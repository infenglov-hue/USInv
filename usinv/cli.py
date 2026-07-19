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
    PitInputBatch,
    PitStoreBuilder,
    build_coverage_report,
    coverage_input,
    eligible_ciks_from_filings,
    fsds_quarter_range,
    standardize_pit_snapshot,
)
from usinv.data.edgar.companyfacts import parse_companyfacts_document
from usinv.data.edgar.live_edge import ingest_periodic_filing
from usinv.data.edgar.submissions import (
    detect_new_periodic_filings,
    parse_submission_history,
    parse_submissions_document,
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
    live = subcommands.add_parser(
        "edgar-live-sync",
        help="archive and normalize newly accepted 10-K/10-Q filings for one CIK",
    )
    live.add_argument("--cik", required=True, help="CIK to synchronize")
    live.add_argument("--as-of", type=_aware_datetime, required=True, help="PIT acceptance cutoff")
    live.add_argument("--seen-accession", action="append", default=[])
    live.add_argument("--cache-dir", type=Path, help="override the EDGAR cache directory")
    live.add_argument("--archive-dir", type=Path, help="override as-filed archive root")
    live.add_argument("--output-dir", type=Path, help="override normalized live-edge root")
    live.add_argument(
        "--include-history",
        action="store_true",
        help="fetch SEC-declared older submissions pages for retrospective work",
    )
    live.add_argument("--refresh", action="store_true", help="revalidate SEC resources")
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
    pit = subcommands.add_parser(
        "pit-build", help="build immutable first-filed and latest fact snapshots"
    )
    pit.add_argument(
        "--start",
        type=_fsds_quarter,
        default=FsdsQuarter(2009, 1),
        help="first archived quarter, inclusive (default: 2009q1)",
    )
    pit.add_argument("--end", type=_fsds_quarter, required=True, help="last quarter, inclusive")
    pit.add_argument("--archive-dir", type=Path, help="override the versioned FSDS archive")
    pit.add_argument("--parquet-dir", type=Path, help="override normalized FSDS Parquet root")
    pit.add_argument("--store-dir", type=Path, help="override immutable PIT snapshot root")
    pit.add_argument(
        "--archive-as-of",
        type=_aware_datetime,
        help="select only FSDS archive versions observed by this instant",
    )
    coverage = subcommands.add_parser(
        "fundamentals-coverage",
        help="measure versioned concept coverage on a verified PIT snapshot",
    )
    coverage.add_argument("--facts-pit", type=Path, required=True)
    coverage.add_argument("--pre", type=Path, action="append", required=True)
    coverage.add_argument("--filings", type=Path, action="append", required=True)
    coverage.add_argument("--sample-quarter", type=_fsds_quarter, required=True)
    coverage.add_argument("--output", type=Path, help="write the JSON report to this path")
    coverage.add_argument(
        "--enforce",
        action="store_true",
        help="return a failing status unless core and secondary thresholds pass",
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
    if args.command == "edgar-live-sync":
        config = load_config()
        data_root = Path(config.settings.paths.data_dir) / "sec"
        archive_root = args.archive_dir or data_root / "filing-edge"
        output_root = args.output_dir or data_root / "live-edge"
        try:
            client = EdgarClient.from_config(config, cache_dir=args.cache_dir)
            submissions_document = client.submissions(args.cik, refresh=args.refresh)
            feed = parse_submissions_document(submissions_document)
            filings = list(feed.filings)
            if args.include_history:
                for filename in feed.history_files:
                    document = client.submission_history(filename, refresh=args.refresh)
                    filings.extend(
                        parse_submission_history(
                            document.payload,
                            cik=feed.cik,
                            source_url=document.url,
                            source_sha256=document.content_sha256,
                        )
                    )
            selected = detect_new_periodic_filings(
                filings,
                seen_accessions=args.seen_accession,
                as_of=args.as_of,
            )
            if not selected:
                print(f"edgar_live_sync_ok cik={feed.cik} new_filings=0")
                return 0
            companyfacts = parse_companyfacts_document(
                client.companyfacts(args.cik, refresh=args.refresh),
                filings=filings,
            )
            for filing in selected:
                result = ingest_periodic_filing(
                    client,
                    filing,
                    companyfacts,
                    archive_root=archive_root,
                    output_root=output_root,
                    refresh=args.refresh,
                )
                print(
                    " ".join(
                        (
                            "edgar_live_filing_ok",
                            f"cik={feed.cik}",
                            f"accession={filing.accession}",
                            f"snapshot={result.snapshot.snapshot_id}",
                            f"facts_raw={result.snapshot.facts_raw_rows}",
                            f"parser_issues={len(result.snapshot.issues)}",
                            f"companyfacts_overlap={result.companyfacts_overlap}",
                            f"companyfacts_mismatches={result.companyfacts_mismatches}",
                        )
                    )
                )
        except EdgarError as exc:
            print(f"edgar_live_sync_failed: {exc}", file=sys.stderr)
            return 2
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
    if args.command == "pit-build":
        config = load_config()
        try:
            ingestor = FsdsIngestor.from_config(
                config,
                archive_dir=args.archive_dir,
                output_dir=args.parquet_dir,
            )
            ingested = ingestor.ingest_range(
                args.start,
                args.end,
                archive_as_of=args.archive_as_of,
            )
            builder = PitStoreBuilder.from_config(config, output_dir=args.store_dir)
            result = builder.build([PitInputBatch.from_fsds_result(item) for item in ingested])
            table_counts = {table.name: table.row_count for table in result.tables}
        except EdgarError as exc:
            print(f"pit_build_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "pit_build_ok",
                    f"state={'snapshot_hit' if result.from_cache else 'created'}",
                    f"snapshot_id={result.snapshot_id}",
                    f"inputs={len(result.inputs)}",
                    f"facts_pit={table_counts['facts_pit']}",
                    f"facts_latest={table_counts['facts_latest']}",
                )
            )
        )
        return 0
    if args.command == "fundamentals-coverage":
        try:
            standardized = standardize_pit_snapshot(
                args.facts_pit,
                presentation_paths=args.pre,
            )
            eligible = eligible_ciks_from_filings(args.filings)
            report = build_coverage_report(
                standardized,
                eligible_ciks=eligible,
                sample_quarter=args.sample_quarter.label,
                inputs=(
                    coverage_input(args.facts_pit, role="facts_pit"),
                    *(coverage_input(path, role="raw_pre") for path in args.pre),
                    *(coverage_input(path, role="filings") for path in args.filings),
                ),
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(report.to_json(), encoding="utf-8")
        except EdgarError as exc:
            print(f"fundamentals_coverage_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "fundamentals_coverage_ok",
                    f"sample={report.sample_quarter}",
                    f"chain={report.chain_version}",
                    f"eligible_issuers={report.eligible_issuers}",
                    f"standardized_facts={len(standardized)}",
                    f"core_rate={report.core_rate:.6f}",
                    f"secondary_rate={report.secondary_rate:.6f}",
                    f"strict_gate={'pass' if report.passed else 'fail'}",
                    "enforcement=deferred_phase_1_5",
                )
            )
        )
        return 3 if args.enforce and not report.passed else 0
    raise AssertionError(f"unhandled command: {args.command}")
