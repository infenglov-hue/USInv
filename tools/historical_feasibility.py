"""Isolated Phase 0.4 historical-data feasibility probe.

This module deliberately lives under ``tools`` rather than ``usinv.data``.
It validates the awkward-security sample and provider contracts, renders an
evidence matrix, and can capture the providers' public demo responses. Raw
responses are never written into a tracked source directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESEARCH_DIR = REPO_ROOT / "research" / "phase_0_4"
REQUIRED_STRATA = frozenset(
    {
        "active",
        "acquired",
        "bankrupt",
        "otc_moved",
        "ticker_recycled",
        "multi_class",
        "reverse_split",
        "pre_2018_delisted",
        "post_2018_delisted",
    }
)
COVERAGE_FIELDS = (
    "raw_ohlcv",
    "open_close_semantics",
    "adjusted_close",
    "adjustment_method",
    "splits",
    "dividends",
    "listing_date",
    "delisting_date",
    "delisting_reason",
    "historical_exchange_security_type",
    "identifier_mapping",
    "ticker_validity_intervals",
)
CONTRACT_STATUSES = frozenset(
    {
        "documented",
        "documented_partial",
        "documented_gap",
        "not_documented",
        "access_required",
        "not_applicable",
    }
)
OBSERVATION_STATUSES = frozenset({"observed", "observed_gap", "blocked", "invalid"})


class FeasibilityError(RuntimeError):
    """Raised when a feasibility artifact or live response violates its contract."""


@dataclass(frozen=True)
class Capture:
    """Metadata for one locally archived raw response."""

    provider: str
    probe: str
    status_code: int
    content_type: str
    byte_count: int
    sha256: str
    raw_path: str
    redacted_url: str
    retrieved_at: str
    valid: bool
    shape: str
    fields: tuple[str, ...]
    row_count: int | None
    notes: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "probe": self.probe,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "raw_path": self.raw_path,
            "redacted_url": self.redacted_url,
            "retrieved_at": self.retrieved_at,
            "valid": self.valid,
            "shape": self.shape,
            "fields": list(self.fields),
            "row_count": self.row_count,
            "notes": self.notes,
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeasibilityError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FeasibilityError(f"top-level JSON value must be an object: {path}")
    return value


def _require_https(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise FeasibilityError(f"{label} must be an https URL")
    return value


def _parse_iso_date(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FeasibilityError(f"{label} must be an ISO date or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise FeasibilityError(f"{label} must be an ISO date") from exc
    return value


def validate_sample_manifest(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and return the deliberately awkward security records."""
    if document.get("schema_version") != 1:
        raise FeasibilityError("sample manifest schema_version must be 1")
    securities = document.get("securities")
    if not isinstance(securities, list) or len(securities) < 30:
        raise FeasibilityError("sample manifest must contain at least 30 securities")

    sample_ids: set[str] = set()
    security_keys: set[str] = set()
    strata: Counter[str] = Counter()
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(securities):
        label = f"securities[{index}]"
        if not isinstance(raw, dict):
            raise FeasibilityError(f"{label} must be an object")
        sample_id = raw.get("sample_id")
        security_key = raw.get("security_key")
        stratum = raw.get("stratum")
        entity_name = raw.get("entity_name")
        if not isinstance(sample_id, str) or re.fullmatch(r"[a-z0-9_]+", sample_id) is None:
            raise FeasibilityError(f"{label}.sample_id must be a lowercase safe slug")
        if sample_id in sample_ids:
            raise FeasibilityError(f"duplicate sample_id: {sample_id}")
        if not isinstance(security_key, str) or ":" not in security_key:
            raise FeasibilityError(f"{label}.security_key must be namespaced, not a bare ticker")
        if security_key in security_keys:
            raise FeasibilityError(f"duplicate security_key: {security_key}")
        if stratum not in REQUIRED_STRATA:
            raise FeasibilityError(f"{label}.stratum is not a required stratum")
        if not isinstance(entity_name, str) or not entity_name.strip():
            raise FeasibilityError(f"{label}.entity_name must be non-empty")

        symbols = raw.get("probe_symbols")
        if not isinstance(symbols, dict):
            raise FeasibilityError(f"{label}.probe_symbols must be an object")
        for provider in ("eodhd", "alpha_vantage"):
            provider_symbols = symbols.get(provider)
            if not isinstance(provider_symbols, list) or not provider_symbols:
                raise FeasibilityError(f"{label}.probe_symbols.{provider} must be non-empty")
            if not all(isinstance(symbol, str) and symbol for symbol in provider_symbols):
                raise FeasibilityError(f"{label}.probe_symbols.{provider} has an invalid symbol")

        ciks = raw.get("expected_ciks")
        if not isinstance(ciks, list) or not ciks:
            raise FeasibilityError(f"{label}.expected_ciks must be non-empty")
        if not all(isinstance(cik, str) and len(cik) == 10 and cik.isdigit() for cik in ciks):
            raise FeasibilityError(f"{label}.expected_ciks must contain zero-padded 10-digit CIKs")
        _parse_iso_date(raw.get("event_date"), label=f"{label}.event_date")
        _require_https(raw.get("evidence_url"), label=f"{label}.evidence_url")
        if not isinstance(raw.get("case_notes"), str) or not raw["case_notes"].strip():
            raise FeasibilityError(f"{label}.case_notes must be non-empty")

        sample_ids.add(sample_id)
        security_keys.add(security_key)
        strata[stratum] += 1
        validated.append(raw)

    missing = REQUIRED_STRATA - set(strata)
    if missing:
        raise FeasibilityError(f"sample manifest is missing strata: {', '.join(sorted(missing))}")
    return validated


def validate_provider_contracts(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and return provider capability declarations."""
    if document.get("schema_version") != 1:
        raise FeasibilityError("provider contracts schema_version must be 1")
    if tuple(document.get("coverage_fields", ())) != COVERAGE_FIELDS:
        raise FeasibilityError("provider contracts coverage_fields do not match the probe schema")
    providers = document.get("providers")
    if not isinstance(providers, list) or not providers:
        raise FeasibilityError("provider contracts must contain providers")

    names: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(providers):
        label = f"providers[{index}]"
        if not isinstance(raw, dict):
            raise FeasibilityError(f"{label} must be an object")
        name = raw.get("provider")
        if not isinstance(name, str) or not name or name in names:
            raise FeasibilityError(f"{label}.provider must be unique and non-empty")
        _require_https(raw.get("contract_url"), label=f"{label}.contract_url")
        _parse_iso_date(raw.get("checked_on"), label=f"{label}.checked_on")
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(COVERAGE_FIELDS):
            raise FeasibilityError(f"{label}.capabilities must define every coverage field")
        for field in COVERAGE_FIELDS:
            capability = capabilities[field]
            if not isinstance(capability, dict):
                raise FeasibilityError(f"{label}.capabilities.{field} must be an object")
            if capability.get("status") not in CONTRACT_STATUSES:
                raise FeasibilityError(f"{label}.capabilities.{field} has an invalid status")
            _require_https(
                capability.get("evidence_url"),
                label=f"{label}.capabilities.{field}.evidence_url",
            )
            if not isinstance(capability.get("limitation"), str):
                raise FeasibilityError(f"{label}.capabilities.{field}.limitation is required")
        names.add(name)
        validated.append(raw)
    return validated


def validate_observations(
    document: Mapping[str, Any], provider_names: set[str]
) -> list[dict[str, Any]]:
    """Validate metadata-only observations; raw provider payloads stay outside Git."""
    if document.get("schema_version") != 1:
        raise FeasibilityError("observations schema_version must be 1")
    raw_observations = document.get("observations")
    if not isinstance(raw_observations, list):
        raise FeasibilityError("observations must be a list")
    observations = list(raw_observations)
    ids: set[str] = set()
    for index, observation in enumerate(observations):
        label = f"observations[{index}]"
        if not isinstance(observation, dict):
            raise FeasibilityError(f"{label} must be an object")
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id or observation_id in ids:
            raise FeasibilityError(f"{label}.observation_id must be unique")
        if observation.get("provider") not in provider_names:
            raise FeasibilityError(f"{label}.provider is unknown")
        sha256 = observation.get("raw_sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise FeasibilityError(f"{label}.raw_sha256 must be a SHA-256 hex digest")
        try:
            int(sha256, 16)
            datetime.fromisoformat(str(observation.get("retrieved_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise FeasibilityError(f"{label} has invalid hash or timestamp") from exc
        fields = observation.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise FeasibilityError(f"{label}.fields must be non-empty")
        for field, status in fields.items():
            if field not in COVERAGE_FIELDS or status not in OBSERVATION_STATUSES:
                raise FeasibilityError(f"{label}.fields contains an invalid field/status")
        if observation.get("raw_archive") != "local_artifact_only":
            raise FeasibilityError(f"{label}.raw_archive must remain local_artifact_only")
        ids.add(observation_id)

    sample_batches = document.get("sample_batches", [])
    if not isinstance(sample_batches, list):
        raise FeasibilityError("sample_batches must be a list")
    for batch_index, batch in enumerate(sample_batches):
        label = f"sample_batches[{batch_index}]"
        if not isinstance(batch, dict):
            raise FeasibilityError(f"{label} must be an object")
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id or batch_id in ids:
            raise FeasibilityError(f"{label}.batch_id must be unique")
        if batch.get("provider") not in provider_names:
            raise FeasibilityError(f"{label}.provider is unknown")
        manifest_sha256 = batch.get("manifest_sha256")
        capture_sha256s = batch.get("capture_sha256s")
        try:
            if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
                raise ValueError
            int(manifest_sha256, 16)
            if not isinstance(capture_sha256s, list) or not capture_sha256s:
                raise ValueError
            if any(not isinstance(value, str) or len(value) != 64 for value in capture_sha256s):
                raise ValueError
            for value in capture_sha256s:
                int(value, 16)
            datetime.fromisoformat(str(batch.get("retrieved_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise FeasibilityError(f"{label} has an invalid hash or timestamp") from exc
        if batch.get("raw_archive") != "local_artifact_only":
            raise FeasibilityError(f"{label}.raw_archive must remain local_artifact_only")
        if not isinstance(batch.get("request_scope"), str) or not isinstance(
            batch.get("notes"), str
        ):
            raise FeasibilityError(f"{label} requires request_scope and notes")
        field_order = batch.get("field_order")
        if (
            not isinstance(field_order, list)
            or not field_order
            or len(field_order) != len(set(field_order))
            or any(field not in COVERAGE_FIELDS for field in field_order)
        ):
            raise FeasibilityError(f"{label}.field_order is invalid")
        samples = batch.get("samples")
        if not isinstance(samples, dict) or not samples:
            raise FeasibilityError(f"{label}.samples must be non-empty")
        for sample_id, statuses in samples.items():
            if not isinstance(sample_id, str) or not isinstance(statuses, list):
                raise FeasibilityError(f"{label}.samples contains an invalid sample")
            if len(statuses) != len(field_order) or any(
                status not in OBSERVATION_STATUSES for status in statuses
            ):
                raise FeasibilityError(f"{label}.samples.{sample_id} has invalid statuses")
            observation_id = f"{batch_id}--{sample_id}"
            if observation_id in ids:
                raise FeasibilityError(f"{label}.samples.{sample_id} has a duplicate identity")
            observations.append(
                {
                    "observation_id": observation_id,
                    "provider": batch["provider"],
                    "sample_id": sample_id,
                    "retrieved_at": batch["retrieved_at"],
                    "request_scope": batch["request_scope"],
                    "raw_sha256": manifest_sha256,
                    "capture_sha256s": capture_sha256s,
                    "raw_archive": batch["raw_archive"],
                    "fields": dict(zip(field_order, statuses, strict=True)),
                    "notes": batch["notes"],
                }
            )
            ids.add(observation_id)
        ids.add(batch_id)
    return observations


def build_matrix(
    securities: Sequence[dict[str, Any]],
    providers: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build one coverage row per provider/security without promoting contract claims."""
    sample_overrides: dict[tuple[str, str], dict[str, str]] = {}
    for observation in observations:
        sample_id = observation.get("sample_id")
        if isinstance(sample_id, str):
            sample_overrides.setdefault((observation["provider"], sample_id), {}).update(
                observation["fields"]
            )

    rows: list[dict[str, str]] = []
    for provider in providers:
        provider_name = provider["provider"]
        for security in securities:
            if provider_name == "alpha_vantage":
                probe_symbol = security["probe_symbols"]["alpha_vantage"][0]
            elif provider_name == "eodhd":
                probe_symbol = security["probe_symbols"]["eodhd"][0]
            else:
                probe_symbol = security["security_key"]
            row = {
                "provider": provider_name,
                "sample_id": security["sample_id"],
                "stratum": security["stratum"],
                "entity_name": security["entity_name"],
                "probe_symbol": probe_symbol,
            }
            overrides = sample_overrides.get((provider_name, security["sample_id"]), {})
            for field in COVERAGE_FIELDS:
                row[field] = overrides.get(field, provider["capabilities"][field]["status"])
            rows.append(row)
    return rows


def _input_hash(*documents: Mapping[str, Any]) -> str:
    encoded = b"\n".join(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for document in documents
    )
    return hashlib.sha256(encoded).hexdigest()


def render_outputs(
    matrix: Sequence[dict[str, str]],
    securities: Sequence[dict[str, Any]],
    providers: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    *,
    output_dir: Path,
    manifest_hash: str,
) -> None:
    """Render deterministic, metadata-only feasibility artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "coverage_matrix.csv"
    fieldnames = [
        "provider",
        "sample_id",
        "stratum",
        "entity_name",
        "probe_symbol",
        *COVERAGE_FIELDS,
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(matrix)

    summary_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for row in matrix:
        for field in COVERAGE_FIELDS:
            summary_counts[row["provider"]][field][row[field]] += 1
    summary = {
        "schema_version": 1,
        "input_manifest_sha256": manifest_hash,
        "security_count": len(securities),
        "provider_count": len(providers),
        "matrix_row_count": len(matrix),
        "strata": dict(sorted(Counter(item["stratum"] for item in securities).items())),
        "coverage_counts": {
            provider: {
                field: dict(sorted(statuses.items())) for field, statuses in sorted(fields.items())
            }
            for provider, fields in sorted(summary_counts.items())
        },
        "observations": [observation["observation_id"] for observation in observations],
        "gate_status": "blocked_pending_research_archive_rights_and_sample",
    }
    (output_dir / "coverage_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = [
        "# Phase 0.4 feasibility evidence report",
        "",
        f"- Input manifest SHA-256: `{manifest_hash}`",
        (
            f"- Sample: {len(securities)} deliberately awkward securities across "
            f"{len(REQUIRED_STRATA)} strata"
        ),
        f"- Providers assessed: {', '.join(provider['provider'] for provider in providers)}",
        f"- Metadata-only live observations: {len(observations)}",
        (
            "- Gate: **BLOCKED** pending a retention-permitted research price archive "
            "and its full sample probe."
        ),
        "",
        "A contract marked `documented` is not treated as observed sample coverage. Only explicit",
        "sample-scoped observations override a matrix cell. Raw responses remain under ignored",
        "local artifacts and are represented here only by hashes and validation metadata.",
        "",
        "## Strata",
        "",
        "| Stratum | Securities |",
        "|---|---:|",
    ]
    counts = Counter(item["stratum"] for item in securities)
    lines.extend(f"| {stratum} | {counts[stratum]} |" for stratum in sorted(counts))
    lines.extend(
        [
            "",
            "## Provider verdicts",
            "",
            "| Provider | Evidence route | Current feasibility verdict |",
            "|---|---|---|",
        ]
    )
    for provider in providers:
        lines.append(
            f"| {provider['provider']} | {provider['evidence_route']} | {provider['verdict']} |"
        )
    lines.extend(
        [
            "",
            "## Live observations",
            "",
            "| Observation | Provider | Scope | Evidence SHA-256 | Result |",
            "|---|---|---|---|---|",
        ]
    )
    for observation in observations:
        result = ", ".join(f"{field}={status}" for field, status in observation["fields"].items())
        lines.append(
            f"| {observation['observation_id']} | {observation['provider']} | "
            f"{observation.get('sample_id') or 'provider snapshot'} | "
            f"`{observation['raw_sha256']}` | {result} |"
        )
    lines.extend(
        [
            "",
            "## Consequence",
            "",
            "The cheap research route is technically plausible but still has an explicit pre-2018",
            "delisted-action gap. EODHD's public terms also require deletion within one month",
            "after expiry, so the old one-month-then-retain plan is prohibited without a written",
            "override. CRSP is a strong",
            "audit-source candidate for actions, delistings and identity history, but it",
            "requires institutional access and does not by itself document full daily OHLCV",
            "with an opening field. No honest historical",
            "backtest claim is permitted at this stage.",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def _ensure_raw_output_is_safe(output_dir: Path) -> Path:
    resolved = output_dir.resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved
    if not relative.parts or relative.parts[0] not in {"artifacts", "data"}:
        raise FeasibilityError(
            "raw captures inside the repo must be under ignored artifacts/ or data/"
        )
    return resolved


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, "REDACTED" if key in {"api_token", "apikey"} else value) for key, value in query
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(redacted),
            parsed.fragment,
        )
    )


def _request(url: str, *, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "USInv-feasibility/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except urllib.error.URLError as exc:
        raise FeasibilityError(f"network error for {_redact_url(url)}: {exc.reason}") from exc


def _json_shape(content: bytes) -> tuple[Any, str, tuple[str, ...], int | None]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeasibilityError("response is not valid UTF-8 JSON") from exc
    if isinstance(payload, list):
        fields = tuple(sorted(payload[0])) if payload and isinstance(payload[0], dict) else ()
        return payload, "array", fields, len(payload)
    if isinstance(payload, dict):
        return payload, "object", tuple(sorted(payload)), None
    return payload, type(payload).__name__, (), None


def _csv_shape(content: bytes) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FeasibilityError("response is not valid UTF-8 CSV") from exc
    reader = csv.DictReader(StringIO(text))
    rows = list(reader)
    return rows, tuple(reader.fieldnames or ())


def _capture(
    *,
    provider: str,
    probe: str,
    url: str,
    extension: str,
    output_dir: Path,
    timeout: float,
    validator: Any,
) -> Capture:
    retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    status, headers, content = _request(url, timeout=timeout)
    raw_path = output_dir / provider / f"{probe}.{extension}"
    _atomic_write(raw_path, content)
    valid, shape, fields, row_count, notes = validator(status, content)
    return Capture(
        provider=provider,
        probe=probe,
        status_code=status,
        content_type=str(headers.get("Content-Type", "")),
        byte_count=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        raw_path=raw_path.relative_to(output_dir).as_posix(),
        redacted_url=_redact_url(url),
        retrieved_at=retrieved_at,
        valid=valid,
        shape=shape,
        fields=fields,
        row_count=row_count,
        notes=notes,
    )


def _write_capture_manifest(
    output_dir: Path,
    captures: Sequence[Capture],
    *,
    sample_coverage: Sequence[Mapping[str, object]] | None = None,
) -> None:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "raw_payload_policy": "local_artifact_only",
        "captures": [capture.as_dict() for capture in captures],
    }
    if sample_coverage is not None:
        manifest["sample_coverage"] = list(sample_coverage)
    _atomic_write(
        output_dir / "capture_manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _eodhd_array_validator(required_fields: set[str]) -> Any:
    def validate(status: int, content: bytes) -> tuple[bool, str, tuple[str, ...], int | None, str]:
        if status != 200:
            return False, "http_error", (), None, f"HTTP {status}; provider access not available"
        try:
            _payload, shape, fields, row_count = _json_shape(content)
        except FeasibilityError as exc:
            return False, "invalid", (), None, str(exc)
        missing = required_fields - set(fields)
        valid = (
            shape == "array"
            and bool(fields)
            and row_count is not None
            and row_count > 0
            and not missing
        )
        notes = (
            "schema and non-empty response validated"
            if valid
            else f"missing fields: {sorted(missing)}"
        )
        return valid, shape, fields, row_count, notes

    return validate


def _eodhd_mapping_validator(
    status: int, content: bytes
) -> tuple[bool, str, tuple[str, ...], int | None, str]:
    if status != 200:
        return False, "http_error", (), None, f"HTTP {status}; demo entitlement blocked"
    try:
        payload, shape, fields, _row_count = _json_shape(content)
    except FeasibilityError as exc:
        return False, "invalid", (), None, str(exc)
    if shape != "object" or not isinstance(payload.get("data"), list):
        return False, shape, fields, None, "mapping response has no data array"
    rows = payload["data"]
    row_fields = tuple(sorted(rows[0])) if rows and isinstance(rows[0], dict) else ()
    expected_identifiers = {"cik", "cusip", "figi", "isin", "lei"}
    valid = bool(rows) and "symbol" in row_fields and bool(expected_identifiers & set(row_fields))
    notes = "mapping data array and identifiers validated" if valid else "empty/incomplete mapping"
    return valid, "object:data", row_fields, len(rows), notes


def _alpha_validator(*, expected_status: str, cutoff: date) -> Any:
    expected_fields = (
        "symbol",
        "name",
        "exchange",
        "assetType",
        "ipoDate",
        "delistingDate",
        "status",
    )

    def validate(status: int, content: bytes) -> tuple[bool, str, tuple[str, ...], int | None, str]:
        if status != 200:
            return False, "http_error", (), None, f"HTTP {status}"
        try:
            rows, fields = _csv_shape(content)
        except FeasibilityError as exc:
            return False, "invalid", (), None, str(exc)
        if fields != expected_fields:
            return (
                False,
                "non_contract_response",
                fields,
                len(rows),
                "expected CSV schema not returned",
            )
        statuses = {row["status"] for row in rows}
        if statuses - {expected_status}:
            return False, "csv", fields, len(rows), f"unexpected statuses: {sorted(statuses)}"
        if expected_status == "Delisted":
            future = [
                row
                for row in rows
                if row["delistingDate"] and date.fromisoformat(row["delistingDate"]) > cutoff
            ]
            if future:
                return False, "csv", fields, len(rows), "response contains post-cutoff delistings"
        valid = bool(rows)
        notes = "CSV schema, status and date cutoff validated" if valid else "empty CSV response"
        return valid, "csv", fields, len(rows), notes

    return validate


def run_public_demo(output_dir: Path, *, provider: str, timeout: float = 30.0) -> list[Capture]:
    """Capture provider-published public demos without creating an account."""
    output_dir = _ensure_raw_output_is_safe(output_dir)
    captures: list[Capture] = []
    if provider in {"all", "eodhd"}:
        base = "https://eodhd.com/api"
        captures.extend(
            [
                _capture(
                    provider="eodhd",
                    probe="aapl_eod_2020_split_window",
                    url=f"{base}/eod/AAPL.US?api_token=demo&fmt=json&from=2020-08-28&to=2020-09-01",
                    extension="json",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_eodhd_array_validator(
                        {"date", "open", "high", "low", "close", "adjusted_close", "volume"}
                    ),
                ),
                _capture(
                    provider="eodhd",
                    probe="aapl_splits_2020",
                    url=f"{base}/splits/AAPL.US?api_token=demo&fmt=json&from=2020-08-01&to=2020-09-30",
                    extension="json",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_eodhd_array_validator({"date", "split"}),
                ),
                _capture(
                    provider="eodhd",
                    probe="aapl_dividends_2020",
                    url=f"{base}/div/AAPL.US?api_token=demo&fmt=json&from=2020-01-01&to=2020-12-31",
                    extension="json",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_eodhd_array_validator({"date", "value"}),
                ),
                _capture(
                    provider="eodhd",
                    probe="aapl_id_mapping",
                    url=(
                        f"{base}/id-mapping?filter%5Bsymbol%5D=AAPL.US&"
                        "page%5Blimit%5D=10&api_token=demo&fmt=json"
                    ),
                    extension="json",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_eodhd_mapping_validator,
                ),
                _capture(
                    provider="eodhd",
                    probe="delisted_common_stock_list",
                    url=(
                        f"{base}/exchange-symbol-list/US?delisted=1&type=common_stock&"
                        "api_token=demo&fmt=json"
                    ),
                    extension="json",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_eodhd_array_validator(set()),
                ),
            ]
        )
    if provider in {"all", "alpha_vantage"}:
        base = "https://www.alphavantage.co/query?function=LISTING_STATUS&date=2014-07-10"
        captures.extend(
            [
                _capture(
                    provider="alpha_vantage",
                    probe="listing_status_delisted_2014_07_10",
                    url=f"{base}&state=delisted&apikey=demo",
                    extension="csv",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_alpha_validator(
                        expected_status="Delisted", cutoff=date(2014, 7, 10)
                    ),
                ),
                _capture(
                    provider="alpha_vantage",
                    probe="listing_status_active_2014_07_10",
                    url=f"{base}&state=active&apikey=demo",
                    extension="csv",
                    output_dir=output_dir,
                    timeout=timeout,
                    validator=_alpha_validator(expected_status="Active", cutoff=date(2014, 7, 10)),
                ),
            ]
        )

    _write_capture_manifest(output_dir, captures)
    return captures


def _sample_window(security: Mapping[str, object], *, as_of: date) -> tuple[date, date]:
    event_value = security.get("event_date")
    if isinstance(event_value, str):
        anchor = date.fromisoformat(event_value)
        start = anchor - timedelta(days=45)
        end = min(anchor + timedelta(days=45), as_of)
    else:
        end = as_of
        start = end - timedelta(days=45)
    if end < start:
        raise FeasibilityError(
            f"sample {security['sample_id']} event window is after the requested as-of date"
        )
    return start, end


def _capture_field_status(capture: Capture) -> str:
    if capture.valid:
        return "observed"
    if capture.status_code in {401, 403, 429}:
        return "blocked"
    return "observed_gap"


def _capture_schema_status(capture: Capture, *required_names: str) -> str:
    base_status = _capture_field_status(capture)
    if base_status != "observed":
        return base_status
    normalized = {field.lower() for field in capture.fields}
    return (
        "observed"
        if all(required_name.lower() in normalized for required_name in required_names)
        else "observed_gap"
    )


def run_eodhd_sample(
    output_dir: Path,
    *,
    securities: Sequence[dict[str, Any]],
    api_token: str,
    as_of: date,
    timeout: float = 30.0,
) -> list[Capture]:
    """Run the bounded EODHD probe across every awkward sample security."""
    if not api_token or api_token.lower() == "demo":
        raise FeasibilityError("full EODHD sample probe requires EODHD_API_TOKEN, not demo")
    output_dir = _ensure_raw_output_is_safe(output_dir)
    base = "https://eodhd.com/api"
    token = urllib.parse.quote(api_token, safe="")
    captures: list[Capture] = []
    sample_coverage: list[dict[str, object]] = []

    delisted_capture = _capture(
        provider="eodhd",
        probe="sample_delisted_common_stock_list",
        url=(
            f"{base}/exchange-symbol-list/US?delisted=1&type=common_stock&"
            f"api_token={token}&fmt=json"
        ),
        extension="json",
        output_dir=output_dir,
        timeout=timeout,
        validator=_eodhd_array_validator(set()),
    )
    captures.append(delisted_capture)

    for security in securities:
        sample_id = security["sample_id"]
        start, end = _sample_window(security, as_of=as_of)
        candidates = security["probe_symbols"]["eodhd"]
        eod_attempts: list[Capture] = []
        selected: str | None = None
        for candidate_number, symbol in enumerate(candidates, start=1):
            capture = _capture(
                provider="eodhd",
                probe=f"{sample_id}_eod_{candidate_number}",
                url=(
                    f"{base}/eod/{urllib.parse.quote(symbol, safe='.-_')}?api_token={token}&"
                    f"fmt=json&from={start.isoformat()}&to={end.isoformat()}"
                ),
                extension="json",
                output_dir=output_dir,
                timeout=timeout,
                validator=_eodhd_array_validator(
                    {"date", "open", "high", "low", "close", "adjusted_close", "volume"}
                ),
            )
            captures.append(capture)
            eod_attempts.append(capture)
            if capture.valid:
                selected = symbol
                break

        fields: dict[str, str] = {
            "raw_ohlcv": "observed" if selected else "observed_gap",
            "adjusted_close": "observed" if selected else "observed_gap",
            "listing_date": _capture_schema_status(delisted_capture, "listingDate"),
            "delisting_date": _capture_schema_status(delisted_capture, "delistingDate"),
            "historical_exchange_security_type": _capture_schema_status(
                delisted_capture, "Exchange", "Type"
            ),
        }
        linked_probes = [capture.probe for capture in eod_attempts]
        if selected is not None:
            encoded_symbol = urllib.parse.quote(selected, safe=".-_")
            action_start = start - timedelta(days=370)
            action_end = min(end + timedelta(days=370), as_of)
            split_capture = _capture(
                provider="eodhd",
                probe=f"{sample_id}_splits",
                url=(
                    f"{base}/splits/{encoded_symbol}?api_token={token}&fmt=json&"
                    f"from={action_start.isoformat()}&to={action_end.isoformat()}"
                ),
                extension="json",
                output_dir=output_dir,
                timeout=timeout,
                validator=_eodhd_array_validator({"date", "split"}),
            )
            dividend_capture = _capture(
                provider="eodhd",
                probe=f"{sample_id}_dividends",
                url=(
                    f"{base}/div/{encoded_symbol}?api_token={token}&fmt=json&"
                    f"from={action_start.isoformat()}&to={action_end.isoformat()}"
                ),
                extension="json",
                output_dir=output_dir,
                timeout=timeout,
                validator=_eodhd_array_validator({"date", "value"}),
            )
            mapping_capture = _capture(
                provider="eodhd",
                probe=f"{sample_id}_id_mapping",
                url=(
                    f"{base}/id-mapping?filter%5Bsymbol%5D={encoded_symbol}&"
                    f"page%5Blimit%5D=20&api_token={token}&fmt=json"
                ),
                extension="json",
                output_dir=output_dir,
                timeout=timeout,
                validator=_eodhd_mapping_validator,
            )
            captures.extend((split_capture, dividend_capture, mapping_capture))
            linked_probes.extend(
                (split_capture.probe, dividend_capture.probe, mapping_capture.probe)
            )
            fields.update(
                {
                    "splits": _capture_field_status(split_capture),
                    "dividends": _capture_field_status(dividend_capture),
                    "identifier_mapping": _capture_field_status(mapping_capture),
                }
            )
        else:
            fields.update(
                {
                    "splits": "observed_gap",
                    "dividends": "observed_gap",
                    "identifier_mapping": "observed_gap",
                }
            )
        sample_coverage.append(
            {
                "provider": "eodhd",
                "sample_id": sample_id,
                "requested_symbols": candidates,
                "selected_symbol": selected,
                "window": {"from": start.isoformat(), "to": end.isoformat()},
                "fields": fields,
                "capture_probes": linked_probes,
            }
        )

    _write_capture_manifest(output_dir, captures, sample_coverage=sample_coverage)
    return captures


def _alpha_rows(path: Path, *, valid: bool) -> list[dict[str, str]]:
    if not valid:
        return []
    rows, _fields = _csv_shape(path.read_bytes())
    return rows


def _alpha_has_value(value: str | None) -> bool:
    return bool(value and value.strip().lower() not in {"-", "n/a", "none", "null"})


def run_alpha_vantage_sample(
    output_dir: Path,
    *,
    securities: Sequence[dict[str, Any]],
    api_key: str,
    as_of: date,
    timeout: float = 30.0,
    request_interval: float = 15.0,
) -> list[Capture]:
    """Match every sample security against dated active and delisted snapshots."""
    if not api_key or api_key.lower() == "demo":
        raise FeasibilityError(
            "full Alpha Vantage sample probe requires ALPHA_VANTAGE_API_KEY, not demo"
        )
    if as_of <= date(2010, 1, 1):
        raise FeasibilityError("Alpha Vantage historical listing dates must be after 2010-01-01")
    if request_interval < 0:
        raise FeasibilityError("Alpha Vantage request interval cannot be negative")
    output_dir = _ensure_raw_output_is_safe(output_dir)
    token = urllib.parse.quote(api_key, safe="")
    base = f"https://www.alphavantage.co/query?function=LISTING_STATUS&date={as_of.isoformat()}"
    active_capture = _capture(
        provider="alpha_vantage",
        probe=f"sample_active_{as_of.isoformat()}",
        url=f"{base}&state=active&apikey={token}",
        extension="csv",
        output_dir=output_dir,
        timeout=timeout,
        validator=_alpha_validator(expected_status="Active", cutoff=as_of),
    )
    if request_interval:
        time.sleep(request_interval)
    delisted_capture = _capture(
        provider="alpha_vantage",
        probe=f"sample_delisted_{as_of.isoformat()}",
        url=f"{base}&state=delisted&apikey={token}",
        extension="csv",
        output_dir=output_dir,
        timeout=timeout,
        validator=_alpha_validator(expected_status="Delisted", cutoff=as_of),
    )
    captures = [active_capture, delisted_capture]
    active_rows = _alpha_rows(output_dir / captures[0].raw_path, valid=captures[0].valid)
    delisted_rows = _alpha_rows(output_dir / captures[1].raw_path, valid=captures[1].valid)
    active_index = defaultdict(list)
    delisted_index = defaultdict(list)
    for row in active_rows:
        active_index[row["symbol"].upper()].append(row)
    for row in delisted_rows:
        delisted_index[row["symbol"].upper()].append(row)

    sample_coverage: list[dict[str, object]] = []
    for security in securities:
        candidates = [symbol.upper() for symbol in security["probe_symbols"]["alpha_vantage"]]
        matches = [
            {"snapshot": "active", **row}
            for symbol in candidates
            for row in active_index.get(symbol, [])
        ] + [
            {"snapshot": "delisted", **row}
            for symbol in candidates
            for row in delisted_index.get(symbol, [])
        ]
        listing_observed = any(_alpha_has_value(match.get("ipoDate")) for match in matches)
        delisting_observed = any(_alpha_has_value(match.get("delistingDate")) for match in matches)
        exchange_type_observed = any(
            _alpha_has_value(match.get("exchange")) and _alpha_has_value(match.get("assetType"))
            for match in matches
        )
        sample_coverage.append(
            {
                "provider": "alpha_vantage",
                "sample_id": security["sample_id"],
                "requested_symbols": candidates,
                "matched_rows": len(matches),
                "matched_symbols": sorted({match["symbol"] for match in matches}),
                "matched_snapshots": sorted({match["snapshot"] for match in matches}),
                "fields": {
                    "listing_date": "observed" if listing_observed else "observed_gap",
                    "delisting_date": "observed" if delisting_observed else "observed_gap",
                    "historical_exchange_security_type": (
                        "observed" if exchange_type_observed else "observed_gap"
                    ),
                },
                "capture_probes": [capture.probe for capture in captures],
            }
        )
    _write_capture_manifest(output_dir, captures, sample_coverage=sample_coverage)
    return captures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="validate committed feasibility inputs")
    validate.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)

    render = subcommands.add_parser("render", help="render metadata-only coverage artifacts")
    render.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    render.add_argument("--output-dir", type=Path)

    live = subcommands.add_parser("live-demo", help="capture provider-published public demos")
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--provider", choices=("all", "eodhd", "alpha_vantage"), default="all")
    live.add_argument("--timeout", type=float, default=30.0)

    sample = subcommands.add_parser(
        "live-sample", help="run the credentialed probe across all awkward securities"
    )
    sample.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    sample.add_argument("--output-dir", type=Path, required=True)
    sample.add_argument("--provider", choices=("eodhd", "alpha_vantage"), required=True)
    sample.add_argument("--as-of", type=date.fromisoformat, required=True)
    sample.add_argument("--timeout", type=float, default=30.0)
    sample.add_argument("--alpha-request-interval", type=float, default=15.0)
    return parser


def _validated_inputs(research_dir: Path) -> tuple[Any, ...]:
    sample_document = _load_json(research_dir / "awkward_securities.json")
    contract_document = _load_json(research_dir / "provider_contracts.json")
    observation_document = _load_json(research_dir / "observations.json")
    securities = validate_sample_manifest(sample_document)
    providers = validate_provider_contracts(contract_document)
    observations = validate_observations(
        observation_document, {provider["provider"] for provider in providers}
    )
    sample_ids = {security["sample_id"] for security in securities}
    unknown_samples = {
        observation["sample_id"]
        for observation in observations
        if observation.get("sample_id") is not None and observation["sample_id"] not in sample_ids
    }
    if unknown_samples:
        raise FeasibilityError(f"observations reference unknown samples: {sorted(unknown_samples)}")
    return (
        sample_document,
        contract_document,
        observation_document,
        securities,
        providers,
        observations,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the isolated feasibility tool."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "live-demo":
            captures = run_public_demo(
                args.output_dir, provider=args.provider, timeout=args.timeout
            )
            valid = sum(capture.valid for capture in captures)
            print(f"feasibility_live_demo captures={len(captures)} valid={valid}")
            for capture in captures:
                print(
                    f"provider={capture.provider} probe={capture.probe} "
                    f"status={capture.status_code} valid={str(capture.valid).lower()} "
                    f"sha256={capture.sha256}"
                )
            return 0

        if args.command == "live-sample":
            sample_document = _load_json(args.research_dir / "awkward_securities.json")
            securities = validate_sample_manifest(sample_document)
            if args.provider == "eodhd":
                captures = run_eodhd_sample(
                    args.output_dir,
                    securities=securities,
                    api_token=os.environ.get("EODHD_API_TOKEN", ""),
                    as_of=args.as_of,
                    timeout=args.timeout,
                )
            else:
                captures = run_alpha_vantage_sample(
                    args.output_dir,
                    securities=securities,
                    api_key=os.environ.get("ALPHA_VANTAGE_API_KEY", ""),
                    as_of=args.as_of,
                    timeout=args.timeout,
                    request_interval=args.alpha_request_interval,
                )
            valid = sum(capture.valid for capture in captures)
            print(
                f"feasibility_live_sample provider={args.provider} securities={len(securities)} "
                f"captures={len(captures)} valid={valid}"
            )
            return 0

        (
            sample_document,
            contract_document,
            observation_document,
            securities,
            providers,
            observations,
        ) = _validated_inputs(args.research_dir)
        manifest_hash = _input_hash(sample_document, contract_document, observation_document)
        matrix = build_matrix(securities, providers, observations)
        if args.command == "render":
            output_dir = args.output_dir or args.research_dir / "generated"
            render_outputs(
                matrix,
                securities,
                providers,
                observations,
                output_dir=output_dir,
                manifest_hash=manifest_hash,
            )
            print(
                f"feasibility_render_ok securities={len(securities)} providers={len(providers)} "
                f"rows={len(matrix)} hash={manifest_hash}"
            )
        else:
            print(
                f"feasibility_validate_ok securities={len(securities)} providers={len(providers)} "
                f"observations={len(observations)} rows={len(matrix)} hash={manifest_hash}"
            )
        return 0
    except FeasibilityError as exc:
        print(f"feasibility_failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
