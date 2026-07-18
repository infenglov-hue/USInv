"""Command-line entry point for safe, explicit USInv operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from usinv.config import load_config
from usinv.data.edgar import (
    EdgarClient,
    EdgarConfigurationError,
    EdgarError,
    FsdsArchiveClient,
    FsdsIngestor,
    FsdsQuarter,
    fsds_quarter_range,
)


def _fsds_quarter(value: str) -> FsdsQuarter:
    try:
        return FsdsQuarter.parse(value)
    except EdgarConfigurationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usinv")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("config-check", help="validate packaged configuration")
    smoke = subcommands.add_parser("edgar-smoke", help="fetch one submissions/companyfacts pair")
    smoke.add_argument("--cik", default="0000320193", help="CIK to fetch (default: Apple)")
    smoke.add_argument("--cache-dir", type=Path, help="override the EDGAR cache directory")
    smoke.add_argument("--refresh", action="store_true", help="bypass a fresh local cache")
    fsds = subcommands.add_parser("fsds-sync", help="download and version SEC FSDS quarterly ZIPs")
    fsds.add_argument(
        "--start",
        type=_fsds_quarter,
        default=FsdsQuarter(2009, 1),
        help="first quarter, inclusive (default: 2009q1 headers-only archive)",
    )
    fsds.add_argument("--end", type=_fsds_quarter, required=True, help="last quarter, inclusive")
    fsds.add_argument("--archive-dir", type=Path, help="override the versioned FSDS archive")
    fsds.add_argument(
        "--refresh",
        action="store_true",
        help="revalidate existing quarters and record SEC replacements",
    )
    ingest = subcommands.add_parser(
        "fsds-ingest", help="convert archived FSDS ZIPs to immutable Parquet tables"
    )
    ingest.add_argument(
        "--start",
        type=_fsds_quarter,
        default=FsdsQuarter(2009, 1),
        help="first archived quarter, inclusive (default: 2009q1)",
    )
    ingest.add_argument("--end", type=_fsds_quarter, required=True, help="last quarter, inclusive")
    ingest.add_argument("--archive-dir", type=Path, help="override the versioned FSDS archive")
    ingest.add_argument("--output-dir", type=Path, help="override the Parquet output root")
    ingest.add_argument(
        "--archive-as-of",
        type=_aware_datetime,
        help="use only an archive version observed by this timezone-aware instant",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested USInv command."""
    args = _parser().parse_args(argv)
    if args.command == "config-check":
        config = load_config()
        print(f"config_ok schema={config.settings.schema_version} hash={config.config_hash}")
        print(
            " ".join(
                (
                    f"evidence={config.settings.evidence_mode.value}",
                    f"execution={config.settings.execution_mode.value}",
                    f"holdings={config.portfolio.holdings}",
                    f"overlay={config.regime.default_overlay}",
                )
            )
        )
        return 0
    if args.command == "edgar-smoke":
        config = load_config()
        try:
            client = EdgarClient.from_config(config, cache_dir=args.cache_dir)
            submissions = client.submissions(args.cik, refresh=args.refresh)
            companyfacts = client.companyfacts(args.cik, refresh=args.refresh)
        except EdgarError as exc:
            print(f"edgar_smoke_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "edgar_smoke_ok",
                    f"cik={EdgarClient.normalize_cik(args.cik)}",
                    f"name={submissions.payload['name']!r}",
                    f"submissions_sha256={submissions.content_sha256}",
                    f"companyfacts_sha256={companyfacts.content_sha256}",
                )
            )
        )
        return 0
    if args.command == "fsds-sync":
        config = load_config()
        try:
            client = FsdsArchiveClient.from_config(config, archive_dir=args.archive_dir)
            quarters = fsds_quarter_range(args.start, args.end)
            new_versions = 0
            reprocessed = 0
            for quarter in quarters:
                result = client.sync_quarter(quarter, refresh=args.refresh)
                state = result.record.outcome if result.network_accessed else "archive_hit"
                print(
                    " ".join(
                        (
                            "fsds_quarter_ok",
                            f"quarter={quarter}",
                            f"state={state}",
                            f"sha256={result.record.content_sha256}",
                            f"bytes={result.record.byte_count}",
                        )
                    )
                )
                new_versions += int(result.new_version)
                reprocessed += int(result.reprocessed)
        except EdgarError as exc:
            print(f"fsds_sync_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "fsds_sync_ok",
                    f"quarters={len(quarters)}",
                    f"new_versions={new_versions}",
                    f"reprocessed={reprocessed}",
                )
            )
        )
        return 0
    if args.command == "fsds-ingest":
        config = load_config()
        try:
            ingestor = FsdsIngestor.from_config(
                config,
                archive_dir=args.archive_dir,
                output_dir=args.output_dir,
            )
            quarters = fsds_quarter_range(args.start, args.end)
            for quarter in quarters:
                result = ingestor.ingest_quarter(
                    quarter,
                    archive_as_of=args.archive_as_of,
                )
                table_counts = {table.name: table.row_count for table in result.tables}
                print(
                    " ".join(
                        (
                            "fsds_ingest_quarter_ok",
                            f"quarter={quarter}",
                            f"state={'parquet_hit' if result.from_cache else 'created'}",
                            f"source_sha256={result.source_sha256}",
                            f"filings={table_counts['filings']}",
                            f"facts_raw={table_counts['facts_raw']}",
                        )
                    )
                )
        except EdgarError as exc:
            print(f"fsds_ingest_failed: {exc}", file=sys.stderr)
            return 2
        print(f"fsds_ingest_ok quarters={len(quarters)}")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")
