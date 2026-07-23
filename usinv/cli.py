"""Command-line entry point for safe, explicit USInv operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from usinv.config import load_config
from usinv.data.edgar import (
    ApplicabilityError,
    EdgarClient,
    EdgarConfigurationError,
    EdgarError,
    FsdsArchiveClient,
    FsdsIngestor,
    FsdsQuarter,
    PitInputBatch,
    PitStoreBuilder,
    acquire_cover_evidence,
    acquire_filing_sic_snapshot,
    augment_discovery_plan_with_exact_names,
    build_cover_security_master,
    build_coverage_report,
    build_filing_discovery_plan,
    cover_plan_changed_ciks,
    coverage_input,
    discover_historical_ticker_candidates,
    eligible_ciks_from_filings,
    fsds_company_name_observations,
    fsds_quarter_range,
    match_unmapped_listing_names,
    match_unmapped_listing_stems,
    materialize_cover_evidence_merge,
    materialize_cover_evidence_shard,
    materialize_filing_discovery_plan,
    materialize_security_master,
    merge_cover_evidence_shards,
    parse_sec_ticker_associations,
    patch_cover_evidence_merge,
    read_cover_evidence_shard,
    read_cover_evidence_snapshot,
    read_filing_discovery_plan,
    read_filing_sic_snapshot,
    read_live_edge_snapshot,
    rebase_cover_evidence_plan,
    reconcile_cover_evidence_merge,
    standardize_pit_snapshot,
)
from usinv.data.edgar.companyfacts import parse_companyfacts_document
from usinv.data.edgar.live_edge import ingest_periodic_filing
from usinv.data.edgar.submissions import (
    detect_new_periodic_filings,
    parse_submission_history,
    parse_submissions_document,
)
from usinv.data.listings import (
    AlphaVantageListingClient,
    ListingDataError,
    materialize_alpha_listing_snapshot,
    read_alpha_listing_snapshot,
)
from usinv.data.prices import (
    AlpacaPriceProvider,
    PriceDataError,
    TiingoSpotCheckClient,
    acquire_price_universe,
    build_price_universe_plan,
    read_price_universe_snapshot,
    rebase_price_universe_snapshot,
)
from usinv.data.tiingo_lifecycle import read_tiingo_lifecycle_zip
from usinv.phase_2_3 import Phase23BuildError, build_phase_2_3
from usinv.universe import (
    UniverseError,
    UniverseGateError,
    enforce_phase_2_3_gate,
    materialize_universe_snapshot,
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


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 date") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usinv")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("config-check", help="validate packaged configuration")
    smoke = subcommands.add_parser("edgar-smoke", help="fetch one submissions/companyfacts pair")
    smoke.add_argument("--cik", default="0000320193", help="CIK to fetch (default: Apple)")
    smoke.add_argument("--cache-dir", type=Path, help="override the EDGAR cache directory")
    smoke.add_argument("--refresh", action="store_true", help="bypass a fresh local cache")
    alpaca = subcommands.add_parser(
        "alpaca-smoke",
        help="fetch separate historical SIP raw/all bars and probe corporate actions",
    )
    alpaca.add_argument("--symbol", default="AAPL")
    alpaca.add_argument(
        "--start",
        type=_aware_datetime,
        default=datetime(2020, 8, 27, 4, tzinfo=UTC),
    )
    alpaca.add_argument(
        "--end",
        type=_aware_datetime,
        default=datetime(2020, 9, 3, 3, 59, 59, tzinfo=UTC),
    )
    alpaca.add_argument(
        "--action-start",
        type=_iso_date,
        default=date(2020, 8, 1),
    )
    alpaca.add_argument(
        "--action-end",
        type=_iso_date,
        default=date(2020, 9, 30),
    )
    tiingo = subcommands.add_parser(
        "tiingo-smoke",
        help="fetch one symbol-capped Tiingo EOD window and report declared actions",
    )
    tiingo.add_argument("--symbol", default="AAPL")
    tiingo.add_argument("--start", type=_iso_date, default=date(2020, 8, 28))
    tiingo.add_argument("--end", type=_iso_date, default=date(2020, 9, 1))
    listings = subcommands.add_parser(
        "alpha-listing-sync",
        help="archive one dated active+delisted Alpha Vantage listing snapshot",
    )
    listings.add_argument("--as-of", type=_iso_date, required=True)
    listings.add_argument("--output-dir", type=Path, help="override the private data root")
    discovery = subcommands.add_parser(
        "sec-filing-discovery",
        help="build a discovery-only SEC filing plan from one private listing snapshot",
    )
    discovery.add_argument("--listing-snapshot", type=Path, required=True)
    discovery.add_argument("--output-dir", type=Path, help="override the private data root")
    discovery.add_argument("--refresh", action="store_true")
    name_discovery = subcommands.add_parser(
        "sec-name-discovery",
        help="add conservative FSDS exact-name CIK candidates to an immutable plan",
    )
    name_discovery.add_argument("--listing-snapshot", type=Path, required=True)
    name_discovery.add_argument("--discovery-plan", type=Path, required=True)
    name_discovery.add_argument("--fsds-start", type=_fsds_quarter, required=True)
    name_discovery.add_argument("--fsds-end", type=_fsds_quarter, required=True)
    name_discovery.add_argument("--as-of", type=_aware_datetime, required=True)
    name_discovery.add_argument("--archive-as-of", type=_aware_datetime)
    name_discovery.add_argument("--archive-dir", type=Path)
    name_discovery.add_argument("--parquet-dir", type=Path)
    name_discovery.add_argument("--output-dir", type=Path)
    name_discovery.add_argument(
        "--legal-stem",
        action="store_true",
        help="add suffix-insensitive candidates that still require filing corroboration",
    )
    ticker_history = subcommands.add_parser(
        "sec-ticker-history-discovery",
        help="add dominant SEC full-text historical-ticker candidates to a plan",
    )
    ticker_history.add_argument("--discovery-plan", type=Path, required=True)
    ticker_history.add_argument(
        "--listing-snapshot",
        type=Path,
        help="verified Alpha snapshot used for exact EFTS entity-name matching",
    )
    ticker_history.add_argument("--as-of", type=_aware_datetime, required=True)
    ticker_history.add_argument("--cache-dir", type=Path, required=True)
    ticker_history.add_argument("--output-dir", type=Path, required=True)
    ticker_history.add_argument("--target-ticker", action="append", default=[])
    ticker_history.add_argument("--refresh", action="store_true")
    cover = subcommands.add_parser(
        "sec-cover-bootstrap",
        help="archive filing-time cover evidence for one immutable SEC discovery plan",
    )
    cover.add_argument("--discovery-plan", type=Path, required=True)
    cover.add_argument("--as-of", type=_aware_datetime, required=True)
    cover.add_argument("--cache-dir", type=Path, help="override the EDGAR cache directory")
    cover.add_argument(
        "--avoid-duplicate-binary-cache",
        action="store_true",
        help="archive filing resources once without also retaining response-cache copies",
    )
    cover.add_argument(
        "--reuse-verified-cache",
        action="store_true",
        help="reuse hash-verified cached SEC JSON without TTL revalidation; fetch cache misses",
    )
    cover.add_argument("--archive-dir", type=Path, help="override the as-filed archive root")
    cover.add_argument("--output-dir", type=Path, help="override the private data root")
    cover.add_argument("--max-ciks", type=int, help="process a bounded resumable CIK shard")
    cover.add_argument("--start-after-cik", type=int, help="resume after this numeric CIK")
    cover.add_argument("--max-filings-per-cik", type=int, default=4)
    cover.add_argument(
        "--require-evidence",
        action="store_true",
        help="fail unless this shard yields at least one matched cover filing",
    )
    cover.add_argument("--refresh", action="store_true")
    cover_merge = subcommands.add_parser(
        "sec-cover-merge",
        help="merge an exact non-overlapping cover-evidence shard partition",
    )
    cover_merge.add_argument("--discovery-plan", type=Path, required=True)
    cover_merge.add_argument("--evidence-root", type=Path, required=True)
    cover_merge.add_argument("--output-dir", type=Path, help="override the private data root")
    cover_upgrade = subcommands.add_parser(
        "sec-cover-upgrade-40f",
        help="upgrade complete v3 cover evidence with cached PIT-bounded 40-F observations",
    )
    cover_upgrade.add_argument("--cover-evidence", type=Path, required=True)
    cover_upgrade.add_argument("--discovery-plan", type=Path, required=True)
    cover_upgrade.add_argument("--cache-dir", type=Path, required=True)
    cover_upgrade.add_argument("--archive-dir", type=Path, required=True)
    cover_upgrade.add_argument("--output-dir", type=Path, required=True)
    cover_upgrade.add_argument("--max-filings-per-cik", type=int, default=4)
    cover_upgrade.add_argument("--target-cik", type=int, action="append", default=[])
    cover_upgrade.add_argument("--refresh", action="store_true")
    cover_rebase = subcommands.add_parser(
        "sec-cover-rebase-plan",
        help="carry unchanged complete cover evidence into a conservative discovery extension",
    )
    cover_rebase.add_argument("--cover-evidence", type=Path, required=True)
    cover_rebase.add_argument("--old-discovery-plan", type=Path, required=True)
    cover_rebase.add_argument("--new-discovery-plan", type=Path, required=True)
    cover_rebase.add_argument("--cache-dir", type=Path, required=True)
    cover_rebase.add_argument("--archive-dir", type=Path, required=True)
    cover_rebase.add_argument("--output-dir", type=Path, required=True)
    cover_rebase.add_argument("--max-filings-per-cik", type=int, default=4)
    cover_rebase.add_argument("--refresh", action="store_true")
    cover_reconcile = subcommands.add_parser(
        "sec-cover-reconcile",
        help="reconcile filing wording drift in one immutable complete cover package",
    )
    cover_reconcile.add_argument("--cover-evidence", type=Path, required=True)
    cover_reconcile.add_argument("--output-dir", type=Path, help="override the private data root")
    universe_prices = subcommands.add_parser(
        "universe-price-sync",
        help="fetch exact raw/all Alpaca batches for a complete filing-backed security master",
    )
    universe_prices.add_argument("--discovery-plan", type=Path, required=True)
    universe_prices.add_argument("--cover-evidence", type=Path, required=True)
    universe_prices.add_argument("--signal-at", type=_aware_datetime, required=True)
    universe_prices.add_argument("--window-sessions", type=int, default=21)
    universe_prices.add_argument("--batch-size", type=int, default=100)
    universe_prices.add_argument("--output-dir", type=Path, help="override price evidence root")
    price_rebase = subcommands.add_parser(
        "universe-price-rebase",
        help="reuse immutable price batches when two cover snapshots share one exact master",
    )
    price_rebase.add_argument("--discovery-plan", type=Path, required=True)
    price_rebase.add_argument("--source-price-universe", type=Path, required=True)
    price_rebase.add_argument("--old-cover-evidence", type=Path, required=True)
    price_rebase.add_argument("--new-cover-evidence", type=Path, required=True)
    price_rebase.add_argument("--signal-at", type=_aware_datetime, required=True)
    price_rebase.add_argument("--window-sessions", type=int, default=21)
    price_rebase.add_argument("--batch-size", type=int, default=100)
    price_rebase.add_argument("--output-dir", type=Path, required=True)
    phase_2_3 = subcommands.add_parser(
        "phase-2-3-build",
        help="build and enforce the real D032 universe from exact immutable inputs",
    )
    phase_2_3.add_argument("--listing-snapshot", type=Path, required=True)
    phase_2_3.add_argument("--discovery-plan", type=Path, required=True)
    phase_2_3.add_argument("--cover-evidence", type=Path, required=True)
    phase_2_3.add_argument("--price-universe", type=Path, required=True)
    phase_2_3.add_argument("--tiingo-lifecycle-zip", type=Path, required=True)
    phase_2_3.add_argument("--signal-at", type=_aware_datetime, required=True)
    phase_2_3.add_argument("--fsds-start", type=_fsds_quarter, required=True)
    phase_2_3.add_argument("--fsds-end", type=_fsds_quarter, required=True)
    phase_2_3.add_argument("--archive-dir", type=Path, help="override FSDS archive root")
    phase_2_3.add_argument("--parquet-dir", type=Path, help="override FSDS Parquet root")
    phase_2_3.add_argument("--store-dir", type=Path, help="override PIT snapshot root")
    phase_2_3.add_argument("--output-dir", type=Path, help="override final universe root")
    phase_2_3.add_argument("--archive-as-of", type=_aware_datetime)
    phase_2_3.add_argument(
        "--live-edge-snapshot",
        type=Path,
        action="append",
        default=[],
        help="verified current-quarter filing batch to add to the PIT store",
    )
    phase_2_3.add_argument(
        "--filing-sic-snapshot",
        type=Path,
        action="append",
        default=[],
        help="verified filing-header SIC supplement",
    )
    filing_sic = subcommands.add_parser(
        "edgar-filing-sic-sync",
        help="archive filing-time SIC headers for selected cover-backed CIKs",
    )
    filing_sic.add_argument("--cover-evidence", type=Path, required=True)
    filing_sic.add_argument("--cik", type=int, action="append", required=True)
    filing_sic.add_argument("--cache-dir", type=Path, required=True)
    filing_sic.add_argument("--output-dir", type=Path, required=True)
    filing_sic.add_argument("--refresh", action="store_true")
    live = subcommands.add_parser(
        "edgar-live-sync",
        help="archive and normalize newly accepted 10-K/10-Q filings for one CIK",
    )
    live.add_argument("--cik", required=True, help="CIK to synchronize")
    live.add_argument("--as-of", type=_aware_datetime, required=True, help="PIT acceptance cutoff")
    live.add_argument(
        "--accepted-after",
        type=_aware_datetime,
        help="include only filings accepted strictly after this PIT lower bound",
    )
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
    if args.command == "alpha-listing-sync":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        try:
            snapshot = AlphaVantageListingClient.from_config(config).fetch_snapshot(
                as_of=args.as_of
            )
            artifact = materialize_alpha_listing_snapshot(snapshot, output_root)
        except ListingDataError as exc:
            print(f"alpha_listing_sync_failed: {exc}", file=sys.stderr)
            return 2
        state = "cache" if artifact.from_cache else "created"
        print(
            " ".join(
                (
                    "alpha_listing_sync_ok",
                    f"as_of={snapshot.as_of.isoformat()}",
                    f"state={state}",
                    f"snapshot_id={artifact.snapshot_id}",
                    f"active_rows={artifact.active_rows}",
                    f"delisted_rows={artifact.delisted_rows}",
                )
            )
        )
        return 0
    if args.command == "sec-filing-discovery":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        try:
            listings = read_alpha_listing_snapshot(args.listing_snapshot)
            client = EdgarClient.from_config(config)
            associations = parse_sec_ticker_associations(
                client.company_tickers_exchange(refresh=args.refresh)
            )
            plan = build_filing_discovery_plan(listings, associations)
            artifact = materialize_filing_discovery_plan(plan, output_root)
        except (EdgarError, ListingDataError) as exc:
            print(f"sec_filing_discovery_failed: {exc}", file=sys.stderr)
            return 2
        state = "cache" if artifact.from_cache else "created"
        print(
            " ".join(
                (
                    "sec_filing_discovery_ok",
                    f"listing_as_of={plan.listing_as_of.isoformat()}",
                    f"state={state}",
                    f"snapshot_id={artifact.snapshot_id}",
                    f"rows={artifact.rows}",
                    f"discovered_ciks={artifact.discovered_ciks}",
                    f"identity_gaps={artifact.identity_gaps}",
                    f"association_unusable_rows={artifact.association_unusable_rows}",
                )
            )
        )
        return 0
    if args.command == "sec-name-discovery":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        try:
            listing = read_alpha_listing_snapshot(args.listing_snapshot)
            discovery = read_filing_discovery_plan(args.discovery_plan)
            ingested = FsdsIngestor.from_config(
                config,
                archive_dir=args.archive_dir,
                output_dir=args.parquet_dir,
            ).ingest_range(
                args.fsds_start,
                args.fsds_end,
                archive_as_of=args.archive_as_of,
            )
            observations = fsds_company_name_observations(ingested, as_of=args.as_of)
            matches = match_unmapped_listing_names(listing, discovery, observations)
            plan = augment_discovery_plan_with_exact_names(discovery, matches)
            stem_matches = {}
            if args.legal_stem:
                stem_matches = match_unmapped_listing_stems(listing, plan, observations)
                plan = augment_discovery_plan_with_exact_names(plan, stem_matches)
            artifact = materialize_filing_discovery_plan(plan, output_root)
        except (EdgarError, ListingDataError) as exc:
            print(f"sec_name_discovery_failed: {exc}", file=sys.stderr)
            return 2
        unique_matches = sum(match.status == "unique" for match in matches.values())
        ambiguous_matches = sum(match.status == "ambiguous" for match in matches.values())
        stem_unique_matches = sum(
            match.status == "unique" for match in stem_matches.values()
        )
        state = "cache" if artifact.from_cache else "created"
        print(
            " ".join(
                (
                    "sec_name_discovery_ok",
                    f"state={state}",
                    f"snapshot_id={artifact.snapshot_id}",
                    f"observations={len(observations)}",
                    f"unique_matches={unique_matches}",
                    f"ambiguous_matches={ambiguous_matches}",
                    f"stem_unique_matches={stem_unique_matches}",
                    f"identity_gaps={artifact.identity_gaps}",
                )
            )
        )
        return 0
    if args.command == "sec-ticker-history-discovery":
        config = load_config()
        try:
            discovery = read_filing_discovery_plan(args.discovery_plan)
            listing_names_by_pointer = None
            if args.listing_snapshot is not None:
                listing = read_alpha_listing_snapshot(args.listing_snapshot)
                if listing.as_of != discovery.listing_as_of:
                    raise EdgarError(
                        "ticker-history listing and discovery dates do not match"
                    )
                listing_names_by_pointer = {
                    f"alpha-vantage://{row.source_sha256}/{row.row_number}": row.name
                    for row in listing.rows
                    if row.state == "active"
                }
            client = EdgarClient.from_config(
                config,
                cache_dir=args.cache_dir,
                cache_ttl_seconds=10 * 365 * 24 * 60 * 60,
            )
            matches = discover_historical_ticker_candidates(
                client,
                discovery,
                as_of=args.as_of,
                target_tickers=(
                    frozenset(args.target_ticker) if args.target_ticker else None
                ),
                listing_names_by_pointer=listing_names_by_pointer,
                refresh=args.refresh,
            )
            plan = augment_discovery_plan_with_exact_names(discovery, matches)
            artifact = materialize_filing_discovery_plan(plan, args.output_dir)
        except EdgarError as exc:
            print(f"sec_ticker_history_discovery_failed: {exc}", file=sys.stderr)
            return 2
        unique_matches = sum(match.status == "unique" for match in matches.values())
        query_count = (
            len(set(args.target_ticker)) if args.target_ticker else len(matches)
        )
        print(
            " ".join(
                (
                    "sec_ticker_history_discovery_ok",
                    f"snapshot_id={artifact.snapshot_id}",
                    f"queries={query_count}",
                    f"unique_matches={unique_matches}",
                    f"identity_gaps={artifact.identity_gaps}",
                )
            )
        )
        return 0
    if args.command == "sec-cover-upgrade-40f":
        config = load_config()
        try:
            source = read_cover_evidence_snapshot(args.cover_evidence)
            plan = read_filing_discovery_plan(args.discovery_plan)
            if plan.snapshot_id != source.merge.plan_snapshot_id:
                raise EdgarError("40-F upgrade plan does not match source evidence")
            client = EdgarClient.from_config(
                config,
                cache_dir=args.cache_dir,
                cache_ttl_seconds=10 * 365 * 24 * 60 * 60,
            )
            targets = (
                tuple(sorted(set(args.target_cik)))
                if args.target_cik
                else tuple(
                    sorted(
                        {
                            gap.cik
                            for gap in source.merge.acquisition_gaps
                            if gap.kind == "unknown_filer_regime"
                        }
                    )
                )
            )
            if not targets:
                raise EdgarError("source cover evidence has no unresolved filer regimes")
            acquisition = acquire_cover_evidence(
                client,
                plan,
                args.archive_dir,
                as_of=source.merge.as_of,
                maximum_filings_per_cik=args.max_filings_per_cik,
                target_ciks=targets,
                reuse_existing_archives=True,
                parallel_ciks=4,
                refresh=args.refresh,
            )
            bootstrap = build_cover_security_master(
                acquisition.evidence,
                as_of=source.merge.as_of,
            )
            shard = materialize_cover_evidence_shard(
                acquisition,
                bootstrap,
                args.output_dir,
            )
            upgraded = patch_cover_evidence_merge(source, shard)
            snapshot = materialize_cover_evidence_merge(upgraded, args.output_dir)
        except EdgarError as exc:
            print(f"sec_cover_upgrade_40f_failed: {exc}", file=sys.stderr)
            return 2
        forty_f_count = sum(
            row.form.upper().removesuffix("/A") == "40-F"
            for row in shard.fpi_form_observations
        )
        print(
            " ".join(
                (
                    "sec_cover_upgrade_40f_ok",
                    f"source_snapshot={source.snapshot_id}",
                    f"targets={len(targets)}",
                    f"40f_observations={forty_f_count}",
                    f"evidence_ciks={len({row.cik for row in shard.master.securities})}",
                    f"evidence_snapshot={snapshot.snapshot_id}",
                    f"master_snapshot={snapshot.master_snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "sec-cover-rebase-plan":
        config = load_config()
        try:
            source = read_cover_evidence_snapshot(args.cover_evidence)
            old_plan = read_filing_discovery_plan(args.old_discovery_plan)
            new_plan = read_filing_discovery_plan(args.new_discovery_plan)
            targets = cover_plan_changed_ciks(old_plan, new_plan)
            client = EdgarClient.from_config(
                config,
                cache_dir=args.cache_dir,
                cache_ttl_seconds=10 * 365 * 24 * 60 * 60,
            )
            acquisition = acquire_cover_evidence(
                client,
                new_plan,
                args.archive_dir,
                as_of=source.merge.as_of,
                maximum_filings_per_cik=args.max_filings_per_cik,
                target_ciks=targets,
                reuse_existing_archives=True,
                parallel_ciks=4,
                refresh=args.refresh,
            )
            bootstrap = build_cover_security_master(
                acquisition.evidence,
                as_of=source.merge.as_of,
            )
            shard = materialize_cover_evidence_shard(
                acquisition,
                bootstrap,
                args.output_dir,
            )
            rebased = rebase_cover_evidence_plan(
                source,
                old_plan,
                new_plan,
                shard,
            )
            snapshot = materialize_cover_evidence_merge(rebased, args.output_dir)
        except EdgarError as exc:
            print(f"sec_cover_rebase_plan_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "sec_cover_rebase_plan_ok",
                    f"source={source.snapshot_id}",
                    f"targets={len(targets)}",
                    f"evidence_ciks={len({row.cik for row in shard.master.securities})}",
                    f"snapshot={snapshot.snapshot_id}",
                    f"master={snapshot.master_snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "sec-cover-bootstrap":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        archive_root = args.archive_dir or output_root / "sec" / "filing-security"
        try:
            plan = read_filing_discovery_plan(args.discovery_plan)
            client_options = (
                {"cache_binary_resources": False} if args.avoid_duplicate_binary_cache else {}
            )
            if args.reuse_verified_cache:
                client_options["cache_ttl_seconds"] = 10 * 365 * 24 * 60 * 60
            client = EdgarClient.from_config(
                config,
                cache_dir=args.cache_dir,
                **client_options,
            )
            acquisition = acquire_cover_evidence(
                client,
                plan,
                archive_root,
                as_of=args.as_of,
                maximum_filings_per_cik=args.max_filings_per_cik,
                maximum_ciks=args.max_ciks,
                start_after_cik=args.start_after_cik,
                refresh=args.refresh,
            )
            if args.require_evidence and not acquisition.evidence:
                raise EdgarError("cover acquisition produced no matched filing evidence")
            bootstrap = build_cover_security_master(acquisition.evidence, as_of=args.as_of)
            evidence_shard = materialize_cover_evidence_shard(
                acquisition,
                bootstrap,
                output_root,
            )
            snapshot = (
                materialize_security_master(
                    bootstrap.master,
                    output_root / "security-bootstrap" / "master" / plan.snapshot_id,
                )
                if acquisition.complete
                else None
            )
        except EdgarError as exc:
            print(f"sec_cover_bootstrap_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "sec_cover_bootstrap_ok",
                    f"plan={plan.snapshot_id}",
                    f"state={'complete' if acquisition.complete else 'partial'}",
                    f"requested_ciks={len(acquisition.requested_ciks)}",
                    f"last_requested_cik={acquisition.requested_ciks[-1]}",
                    f"deferred_ciks={len(acquisition.deferred_ciks)}",
                    f"archived_filings={acquisition.archived_filings}",
                    f"evidence_filings={len(acquisition.evidence)}",
                    f"share_observations={len(acquisition.share_observations)}",
                    f"fpi_form_observations={len(acquisition.fpi_form_observations)}",
                    f"complete_form_histories={len(acquisition.form_history_proofs)}",
                    f"evidence_ciks={len({row.cik for row in bootstrap.master.securities})}",
                    f"acquisition_gaps={len(acquisition.gaps)}",
                    f"security_master_gaps={len(bootstrap.gaps)}",
                    f"evidence_shard={evidence_shard.snapshot_id}",
                    f"master_snapshot={snapshot.snapshot_id if snapshot else 'deferred'}",
                )
            )
        )
        return 0
    if args.command == "sec-cover-merge":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        try:
            plan = read_filing_discovery_plan(args.discovery_plan)
            shard_paths = tuple(
                sorted(path.parent for path in args.evidence_root.rglob("shard.json"))
            )
            shards = tuple(read_cover_evidence_shard(path) for path in shard_paths)
            merged = merge_cover_evidence_shards(shards, expected_ciks=plan.ciks)
            if merged.plan_snapshot_id != plan.snapshot_id:
                raise EdgarError("cover evidence shards do not belong to the discovery plan")
            snapshot = materialize_cover_evidence_merge(merged, output_root)
        except EdgarError as exc:
            print(f"sec_cover_merge_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "sec_cover_merge_ok",
                    f"plan={plan.snapshot_id}",
                    f"shards={len(shards)}",
                    f"covered_ciks={len(merged.requested_ciks)}",
                    f"securities={len(merged.master.securities)}",
                    f"symbols={len(merged.master.symbols)}",
                    f"mapping_issues={len(merged.master.issues)}",
                    f"acquisition_gaps={len(merged.acquisition_gaps)}",
                    f"bootstrap_gaps={len(merged.bootstrap_gaps)}",
                    f"share_observations={len(merged.share_observations)}",
                    f"fpi_form_observations={len(merged.fpi_form_observations)}",
                    f"complete_form_histories={len(merged.form_history_proofs)}",
                    f"evidence_snapshot={snapshot.snapshot_id}",
                    f"master_snapshot={snapshot.master_snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "sec-cover-reconcile":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir)
        try:
            source = read_cover_evidence_snapshot(args.cover_evidence)
            result = reconcile_cover_evidence_merge(source.merge)
            snapshot = materialize_cover_evidence_merge(result.merge, output_root)
        except EdgarError as exc:
            print(f"sec_cover_reconcile_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "sec_cover_reconcile_ok",
                    f"source_snapshot={source.snapshot_id}",
                    f"collapsed_groups={result.collapsed_groups}",
                    f"rewritten_security_ids={result.rewritten_security_ids}",
                    f"ambiguous_groups={result.ambiguous_groups}",
                    f"securities={len(result.merge.master.securities)}",
                    f"symbols={len(result.merge.master.symbols)}",
                    f"mapping_issues={len(result.merge.master.issues)}",
                    f"evidence_snapshot={snapshot.snapshot_id}",
                    f"master_snapshot={snapshot.master_snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "universe-price-sync":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir) / "prices"
        try:
            discovery = read_filing_discovery_plan(args.discovery_plan)
            cover_snapshot = read_cover_evidence_snapshot(args.cover_evidence)
            plan = build_price_universe_plan(
                discovery,
                cover_snapshot,
                signal_at=args.signal_at,
                window_sessions=args.window_sessions,
                batch_size=args.batch_size,
            )
            snapshot = acquire_price_universe(
                AlpacaPriceProvider.from_config(config),
                plan,
                cover_snapshot,
                output_root,
            )
        except (EdgarError, PriceDataError) as exc:
            print(f"universe_price_sync_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "universe_price_sync_ok",
                    f"plan={plan.snapshot_id}",
                    f"targets={len(plan.targets)}",
                    f"batches={len(plan.batches)}",
                    f"raw_rows={sum(row.raw_rows for row in snapshot.price_snapshots)}",
                    f"mapping_issues={sum(len(row.issues) for row in snapshot.price_snapshots)}",
                    f"snapshot={snapshot.snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "universe-price-rebase":
        try:
            discovery = read_filing_discovery_plan(args.discovery_plan)
            source = read_price_universe_snapshot(args.source_price_universe)
            old_cover = read_cover_evidence_snapshot(args.old_cover_evidence)
            new_cover = read_cover_evidence_snapshot(args.new_cover_evidence)
            plan = build_price_universe_plan(
                discovery,
                new_cover,
                signal_at=args.signal_at,
                window_sessions=args.window_sessions,
                batch_size=args.batch_size,
            )
            snapshot = rebase_price_universe_snapshot(
                source,
                old_cover,
                new_cover,
                plan,
                args.output_dir,
            )
        except (EdgarError, PriceDataError) as exc:
            print(f"universe_price_rebase_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "universe_price_rebase_ok",
                    f"source={source.snapshot_id}",
                    f"cover={new_cover.snapshot_id}",
                    f"master={new_cover.master_snapshot_id}",
                    f"targets={len(plan.targets)}",
                    f"snapshot={snapshot.snapshot_id}",
                )
            )
        )
        return 0
    if args.command == "phase-2-3-build":
        config = load_config()
        output_root = args.output_dir or Path(config.settings.paths.data_dir) / "phase-2-3"
        try:
            listing = read_alpha_listing_snapshot(args.listing_snapshot)
            discovery = read_filing_discovery_plan(args.discovery_plan)
            cover_snapshot = read_cover_evidence_snapshot(args.cover_evidence)
            price_snapshot = read_price_universe_snapshot(args.price_universe)
            lifecycle = read_tiingo_lifecycle_zip(args.tiingo_lifecycle_zip)
            ingestor = FsdsIngestor.from_config(
                config,
                archive_dir=args.archive_dir,
                output_dir=args.parquet_dir,
            )
            ingested = ingestor.ingest_range(
                args.fsds_start,
                args.fsds_end,
                archive_as_of=args.archive_as_of,
            )
            supplements = tuple(
                read_live_edge_snapshot(path) for path in args.live_edge_snapshot
            )
            filing_sic_supplements = tuple(
                read_filing_sic_snapshot(path) for path in args.filing_sic_snapshot
            )
            pit = PitStoreBuilder.from_config(config, output_dir=args.store_dir).build(
                [
                    *(PitInputBatch.from_fsds_result(row) for row in ingested),
                    *(row.pit_input() for row in supplements),
                ]
            )
            result = build_phase_2_3(
                listing,
                discovery,
                cover_snapshot,
                price_snapshot,
                ingested,
                pit,
                signal_at=args.signal_at,
                config=config,
                lifecycle=lifecycle,
                supplements=supplements,
                filing_sic_supplements=filing_sic_supplements,
            )
            universe_artifact = materialize_universe_snapshot(result.universe, output_root)
            gate_dir = output_root / "gate-evidence" / universe_artifact.snapshot_id
            gate_dir.mkdir(parents=True, exist_ok=True)
            (gate_dir / "coverage.json").write_text(
                result.coverage.to_json(),
                encoding="utf-8",
            )
            (gate_dir / "evidence-gaps.json").write_text(
                json.dumps(
                    [
                        {
                            "security_id": row.security_id,
                            "cik": row.cik,
                            "kind": row.kind,
                            "detail": row.detail,
                        }
                        for row in result.evidence.gaps
                    ],
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except (
            ApplicabilityError,
            EdgarError,
            ListingDataError,
            Phase23BuildError,
            PriceDataError,
            UniverseError,
        ) as exc:
            print(f"phase_2_3_build_failed: {exc}", file=sys.stderr)
            return 2
        gate_error: UniverseGateError | None = None
        try:
            enforce_phase_2_3_gate(result.universe, result.coverage)
        except UniverseGateError as exc:
            gate_error = exc
        print(
            " ".join(
                (
                    "phase_2_3_build_ok" if gate_error is None else "phase_2_3_gate_blocked",
                    f"universe_snapshot={universe_artifact.snapshot_id}",
                    f"candidates={len(result.universe.rows)}",
                    f"included={len(result.universe.included)}",
                    f"identity_gaps={len(result.universe.identity_mapping_gaps)}",
                    f"sector_gaps={len(result.universe.sector_mapping_gaps)}",
                    f"evidence_gaps={len(result.evidence.gaps)}",
                    f"core_coverage={result.coverage.core_rate:.6f}",
                    f"secondary_coverage={result.coverage.secondary_rate:.6f}",
                    f"gate={'passed' if gate_error is None else 'blocked'}",
                )
            )
        )
        if gate_error is not None:
            print(f"phase_2_3_gate_reason: {gate_error}", file=sys.stderr)
            return 2
        return 0
    if args.command == "edgar-filing-sic-sync":
        config = load_config()
        try:
            cover = read_cover_evidence_snapshot(args.cover_evidence)
            client = EdgarClient.from_config(config, cache_dir=args.cache_dir)
            snapshot = acquire_filing_sic_snapshot(
                client,
                cover,
                tuple(sorted(set(args.cik))),
                args.output_dir,
                refresh=args.refresh,
            )
        except EdgarError as exc:
            print(f"edgar_filing_sic_sync_failed: {exc}", file=sys.stderr)
            return 2
        print(
            " ".join(
                (
                    "edgar_filing_sic_sync_ok",
                    f"targets={len(snapshot.target_ciks)}",
                    f"records={len(snapshot.records)}",
                    f"gaps={len(snapshot.gaps)}",
                    f"snapshot={snapshot.snapshot_id}",
                    f"path={snapshot.output_dir}",
                )
            )
        )
        return 0
    if args.command == "tiingo-smoke":
        config = load_config()
        try:
            result = TiingoSpotCheckClient.from_config(config).fetch(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
            )
            actions = result.action_observations(security_id=f"smoke:{result.symbol}")
        except PriceDataError as exc:
            print(f"tiingo_smoke_failed: {exc}", file=sys.stderr)
            return 2
        action_types = ",".join(action.action_type for action in actions) or "none"
        print(
            " ".join(
                (
                    "tiingo_smoke_ok",
                    f"symbol={result.symbol}",
                    f"rows={len(result.rows)}",
                    f"source_sha256={result.page.content_sha256}",
                    f"corporate_action_rows={len(actions)}",
                    f"corporate_action_types={action_types}",
                )
            )
        )
        return 0
    if args.command == "alpaca-smoke":
        config = load_config()
        try:
            provider = AlpacaPriceProvider.from_config(config)
            raw, adjusted = provider.fetch_raw_and_all(
                symbols=(args.symbol,),
                start=args.start,
                end=args.end,
            )
            actions = provider.probe_corporate_actions(
                symbols=(args.symbol,),
                start=args.action_start,
                end=args.action_end,
            )
        except PriceDataError as exc:
            print(f"alpaca_smoke_failed: {exc}", file=sys.stderr)
            return 2
        action_rows = sum(actions.category_counts.values())
        print(
            " ".join(
                (
                    "alpaca_smoke_ok",
                    f"symbol={args.symbol.upper()}",
                    f"raw_rows={len(raw.bars)}",
                    f"adjusted_rows={len(adjusted.bars)}",
                    f"raw_sha256={raw.source_sha256}",
                    f"adjusted_sha256={adjusted.source_sha256}",
                    f"corporate_actions={actions.outcome}",
                    f"corporate_action_rows={action_rows}",
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
                accepted_after=args.accepted_after,
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
                    "enforcement=phase_2_3_final_universe",
                )
            )
        )
        return 3 if args.enforce and not report.passed else 0
    raise AssertionError(f"unhandled command: {args.command}")
