"""Content-addressed immutable archive for raw macro provider payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from usinv.data.macro.vintage import MacroDataError

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SECRET_QUERY_KEYS = frozenset({"api_key", "apikey", "token", "key"})


@dataclass(frozen=True, slots=True)
class MacroArchiveRecord:
    source: str
    series_id: str
    retrieved_at: str
    request_url: str
    payload_sha256: str
    payload_bytes: int
    object_path: str


def redact_request_url(url: str) -> str:
    """Remove credential-like query parameters before evidence is persisted."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _SECRET_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class MacroRawArchive:
    """Store raw responses once under their content hash and verify cache hits."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _component(value: str, label: str) -> str:
        if not value or not _SAFE_COMPONENT.fullmatch(value):
            raise MacroDataError(f"unsafe {label}: {value!r}")
        return value

    def store(
        self,
        *,
        source: str,
        series_id: str,
        retrieved_at: datetime,
        request_url: str,
        payload: bytes,
    ) -> MacroArchiveRecord:
        if retrieved_at.tzinfo is None:
            raise MacroDataError("retrieved_at must be timezone-aware")
        if not payload:
            raise MacroDataError("macro payload cannot be empty")
        safe_source = self._component(source, "source")
        safe_series = self._component(series_id, "series_id")
        digest = hashlib.sha256(payload).hexdigest()
        object_dir = self.root / safe_source / safe_series / digest
        payload_path = object_dir / "payload.bin"
        metadata_path = object_dir / "metadata.json"
        relative_payload = payload_path.relative_to(self.root).as_posix()
        record = MacroArchiveRecord(
            source=safe_source,
            series_id=safe_series,
            retrieved_at=retrieved_at.isoformat(),
            request_url=redact_request_url(request_url),
            payload_sha256=digest,
            payload_bytes=len(payload),
            object_path=relative_payload,
        )

        object_dir.mkdir(parents=True, exist_ok=True)
        if payload_path.exists():
            if payload_path.read_bytes() != payload:
                raise MacroDataError("macro archive object hash collision")
        else:
            temporary = object_dir / f".payload.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(payload)
            temporary.replace(payload_path)

        metadata = json.dumps(asdict(record), sort_keys=True, separators=(",", ":")) + "\n"
        if metadata_path.exists():
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                existing.get("payload_sha256") != digest
                or existing.get("series_id") != safe_series
                or existing.get("source") != safe_source
            ):
                raise MacroDataError("macro archive metadata contradicts the object")
        else:
            temporary = object_dir / f".metadata.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            temporary.write_text(metadata, encoding="utf-8")
            temporary.replace(metadata_path)
        return record

    def verify(self, record: MacroArchiveRecord) -> None:
        path = self.root / record.object_path
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise MacroDataError("macro archive record escapes its root") from exc
        if not path.is_file():
            raise MacroDataError("macro archive object is missing")
        payload = path.read_bytes()
        if len(payload) != record.payload_bytes:
            raise MacroDataError("macro archive byte count mismatch")
        if hashlib.sha256(payload).hexdigest() != record.payload_sha256:
            raise MacroDataError("macro archive hash mismatch")
