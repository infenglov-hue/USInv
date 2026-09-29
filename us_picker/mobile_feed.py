"""Package the offline mobile snapshot into a cloud-friendly feed."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import mkstemp
from urllib.parse import urljoin

from us_picker import read_service
from us_picker.db.connection import ensure_runtime_db_ready, get_session
from us_picker.mobile_snapshot import (
    ROOM_DATABASE_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    export_mobile_snapshot,
    validate_mobile_snapshot,
)

MOBILE_FEED_VERSION = 1
DEFAULT_FEED_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "mobile_feed"
DEFAULT_FEED_MANIFEST_FILENAME = "manifest.json"
DEFAULT_FEED_SNAPSHOT_FILENAME = "mobile_snapshot.db.gz"
DEFAULT_LIVE_TICKERS_FILENAME = "live_tickers.json"
DEFAULT_BACKTEST_AUDIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "output"
    / "backtest_investor_grade_index_aware.json"
)
# L1 (2026-07-10): daily nominal+deflated summary written by the backtest run
# (backtest/engine.persist_daily_summary) so the PWA decision card can show
# REAL returns. Read fail-soft; stale files are dropped rather than shipped.
DEFAULT_DAILY_SUMMARY_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "output"
    / "backtest_daily_summary.json"
)
REAL_RETURNS_MAX_AGE_DAYS = 3
BACKTEST_AUDIT_MAX_AGE_DAYS = 8
ALLOW_STALE_FEED_EXPORT_ENV = "US_PICKER_ALLOW_STALE_FEED_EXPORT"


@dataclass(frozen=True)
class MobileFeedExportResult:
    """Paths and metadata produced by a mobile feed export."""

    manifest_path: Path
    snapshot_path: Path
    live_tickers_path: Path
    manifest: dict[str, object]


def export_mobile_feed(
    feed_dir: str | Path = DEFAULT_FEED_DIRECTORY,
    *,
    base_download_url: str | None = None,
    manifest_filename: str = DEFAULT_FEED_MANIFEST_FILENAME,
    snapshot_filename: str = DEFAULT_FEED_SNAPSHOT_FILENAME,
    live_tickers_filename: str = DEFAULT_LIVE_TICKERS_FILENAME,
) -> MobileFeedExportResult:
    """Create a published mobile feed directory with manifest + gzip snapshot."""
    runtime_engine = ensure_runtime_db_ready(read_service.get_engine())

    feed_dir = Path(feed_dir).resolve()
    feed_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = feed_dir / manifest_filename
    snapshot_path = feed_dir / snapshot_filename
    live_tickers_path = feed_dir / live_tickers_filename

    temp_fd, temp_name = mkstemp(prefix="mobile_feed_", suffix=".db")
    os.close(temp_fd)
    temp_snapshot_path = Path(temp_name)
    try:
        export_mobile_snapshot(temp_snapshot_path)
        metadata = validate_mobile_snapshot(temp_snapshot_path)

        _guard_against_stale_snapshot_regression(snapshot_path, metadata)

        with temp_snapshot_path.open("rb") as source, gzip.open(snapshot_path, "wb") as target:
            shutil.copyfileobj(source, target)
    finally:
        if temp_snapshot_path.exists():
            try:
                temp_snapshot_path.unlink()
            except PermissionError:
                pass

    snapshot_sha256 = _sha256_for_file(snapshot_path)
    from us_picker.live_prices import write_live_ticker_universe

    runtime_session = get_session(runtime_engine)
    try:
        live_tickers = write_live_ticker_universe(
            live_tickers_path,
            session=runtime_session,
        )
    finally:
        runtime_session.close()
    manifest = {
        "snapshot_version": ROOM_DATABASE_VERSION,
        "exported_at": metadata.get("exported_at"),
        "snapshot": {
            "filename": snapshot_filename,
            "url": _build_download_url(
                base_download_url=base_download_url,
                snapshot_filename=snapshot_filename,
            ),
            "sha256": snapshot_sha256,
            "size_bytes": snapshot_path.stat().st_size,
            "compression": "gzip",
        },
        "live_tickers": {
            "filename": live_tickers_filename,
            "url": _build_download_url(
                base_download_url=base_download_url,
                snapshot_filename=live_tickers_filename,
            ),
            "ticker_count": live_tickers["ticker_count"],
            "latest_price_date": live_tickers["latest_price_date"],
        },
    }
    backtest_audit = _load_backtest_audit()
    if backtest_audit is not None:
        manifest["backtest_audit"] = backtest_audit

    real_returns = _load_real_returns()
    if real_returns is not None:
        manifest["real_returns"] = real_returns

    # Rotation calendar for the PWA: lets the UI say "next rotation: <date>"
    # and "hold picks for N weeks" without duplicating the cadence logic.
    from datetime import date as _date

    from us_picker.portfolio.rotation import (
        load_rotation_config,
        next_rotation_date,
        rotation_cycle_start,
    )

    rotation_weeks, rotation_anchor = load_rotation_config()
    today = _date.today()
    manifest["rotation"] = {
        "rotation_weeks": rotation_weeks,
        "cycle_start": rotation_cycle_start(today, rotation_weeks, rotation_anchor).isoformat(),
        "next_rotation_date": next_rotation_date(today, rotation_weeks, rotation_anchor).isoformat(),
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return MobileFeedExportResult(
        manifest_path=manifest_path,
        snapshot_path=snapshot_path,
        live_tickers_path=live_tickers_path,
        manifest=manifest,
    )


def _guard_against_stale_snapshot_regression(
    existing_snapshot_path: Path,
    new_metadata: dict[str, object],
) -> None:
    """Refuse to overwrite a feed snapshot with older model or price dates."""
    if os.environ.get(ALLOW_STALE_FEED_EXPORT_ENV, "").strip() == "1":
        return
    if not existing_snapshot_path.exists():
        return

    existing_metadata = _read_gzip_snapshot_metadata(existing_snapshot_path)
    if not existing_metadata:
        return

    checks = [
        ("snapshot_date", "model snapshot date"),
        ("latest_price_date", "latest price date"),
    ]
    for field, label in checks:
        existing_date = _parse_iso_date(existing_metadata.get(field))
        new_date = _parse_iso_date(new_metadata.get(field))
        if existing_date is not None and new_date is not None and new_date < existing_date:
            raise RuntimeError(
                "Refusing to overwrite mobile feed with stale data: "
                f"new {label} {new_date.isoformat()} is older than existing "
                f"{existing_date.isoformat()}. Set {ALLOW_STALE_FEED_EXPORT_ENV}=1 "
                "only for an intentional rollback."
            )


def _read_gzip_snapshot_metadata(snapshot_path: Path) -> dict[str, object] | None:
    temp_fd, temp_name = mkstemp(prefix="mobile_feed_existing_", suffix=".db")
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with gzip.open(snapshot_path, "rb") as source, temp_path.open("wb") as target:
            shutil.copyfileobj(source, target)
        with sqlite3.connect(temp_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT snapshot_date, latest_price_date, exported_at "
                "FROM snapshot_metadata WHERE id = 1"
            ).fetchone()
            return dict(row) if row is not None else None
    except (OSError, gzip.BadGzipFile, sqlite3.DatabaseError):
        return None
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except PermissionError:
                pass


def _parse_iso_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _build_download_url(base_download_url: str | None, snapshot_filename: str) -> str:
    """Build a feed download URL; relative paths remain relative when no base URL is given."""
    if not base_download_url:
        return snapshot_filename
    base = base_download_url.rstrip("/") + "/"
    return urljoin(base, snapshot_filename)


def _sha256_for_file(path: Path) -> str:
    """Compute a hex sha256 for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_backtest_audit(
    path: Path = DEFAULT_BACKTEST_AUDIT_PATH,
    *,
    max_age_days: int = BACKTEST_AUDIT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Load a current investor-grade report for the PWA manifest."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except (TypeError, ValueError):
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if (now - generated_at).days > max_age_days:
        return None
    return payload


def _load_real_returns(
    path: Path = DEFAULT_DAILY_SUMMARY_PATH,
    max_age_days: int = REAL_RETURNS_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Compact real-returns block for the manifest; None when absent or stale.

    Staleness guard matters: the artifact comes from the same run's backtest
    step, but a partially failed pipeline could leave last week's file on
    disk — old purchasing-power numbers must not ship as if current.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("deflated"), dict):
        return None
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except (TypeError, ValueError):
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if (now - generated_at).days > max_age_days:
        return None
    return {
        "generated_at": payload.get("generated_at"),
        "window": {
            "start": payload.get("start_date"),
            "end": payload.get("end_date"),
        },
        "nominal": payload.get("nominal"),
        "deflated": payload.get("deflated"),
    }
