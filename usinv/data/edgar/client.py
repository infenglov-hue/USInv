"""Polite, cached and retrying client for public ``data.sec.gov`` JSON APIs."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from usinv import __version__
from usinv.config import AppConfig

BASE_URL: Final = "https://data.sec.gov"
ARCHIVE_BASE_URL: Final = "https://www.sec.gov/Archives/edgar/data"
COMPANY_TICKERS_EXCHANGE_URL: Final = "https://www.sec.gov/files/company_tickers_exchange.json"
EFTS_SEARCH_URL: Final = "https://efts.sec.gov/LATEST/search-index"
ALLOWED_SEC_HOSTS: Final = frozenset({"data.sec.gov", "efts.sec.gov", "www.sec.gov"})
RETRIABLE_STATUS_CODES: Final = frozenset({403, 429, 500, 502, 503, 504})
EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == expected), None)


class EdgarError(RuntimeError):
    """Base error for EDGAR client failures."""


class EdgarConfigurationError(EdgarError):
    """Raised before networking when contact or rate configuration is unsafe."""


class EdgarHttpError(EdgarError):
    """Raised when a request exhausts retries or returns a permanent error."""

    def __init__(self, status: int | None, url: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class EdgarPayloadError(EdgarError):
    """Raised when SEC returns invalid JSON or an unexpected top-level schema."""


class EdgarCacheError(EdgarError):
    """Raised when a cached response is incomplete or fails its content hash."""


def validate_sec_contact(value: str) -> str:
    """Validate the monitored contact address declared to SEC."""
    contact = value.strip()
    lowered = contact.lower()
    if not EMAIL_PATTERN.fullmatch(contact):
        raise EdgarConfigurationError("EDGAR contact must be a valid email address")
    if "noreply" in lowered or lowered.endswith("@example.com"):
        raise EdgarConfigurationError("EDGAR contact must be a monitored email address")
    return contact


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Perform one HTTP GET without applying retries."""


class UrllibTransport:
    """Minimal standard-library transport with explicit content decoding."""

    @staticmethod
    def _decode(body: bytes, headers: Mapping[str, str]) -> bytes:
        encoding = (_header(headers, "Content-Encoding") or "").lower()
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            return zlib.decompress(body)
        return body

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_headers = dict(response.headers.items())
                body = self._decode(response.read(), response_headers)
                return HttpResponse(response.status, response_headers, body)
        except HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            body = self._decode(exc.read(), response_headers)
            return HttpResponse(exc.code, response_headers, body)
        except IncompleteRead as exc:
            # Truncated chunked transfer (observed under parallel CIK fetches);
            # surface as URLError so the client's transient-error retry applies.
            raise URLError(f"incomplete read from {url}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class EdgarDocument:
    """Decoded SEC response with immutable retrieval provenance."""

    url: str
    retrieved_at: datetime
    validated_at: datetime
    content_sha256: str
    payload: Mapping[str, Any]
    from_cache: bool
    revalidated: bool


@dataclass(frozen=True, slots=True)
class EdgarResource:
    """Raw SEC resource with immutable retrieval provenance."""

    url: str
    retrieved_at: datetime
    validated_at: datetime
    content_sha256: str
    body: bytes
    from_cache: bool
    revalidated: bool


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    body: bytes
    retrieved_at: datetime
    validated_at: datetime
    content_sha256: str
    etag: str | None
    last_modified: str | None


class _ResponseCache:
    def __init__(self, directory: Path, now: Callable[[], datetime]) -> None:
        self.directory = directory
        self._now = now
        self._lock = threading.Lock()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self._key(url)
        return self.directory / f"{key}.body.json", self.directory / f"{key}.meta.json"

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    def load(self, url: str) -> _CachedResponse | None:
        body_path, metadata_path = self._paths(url)
        with self._lock:
            if not body_path.exists() and not metadata_path.exists():
                return None
            if not body_path.is_file() or not metadata_path.is_file():
                raise EdgarCacheError(f"incomplete EDGAR cache entry for {url}")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                body = body_path.read_bytes()
                retrieved_at = datetime.fromisoformat(metadata["retrieved_at"])
                validated_at = datetime.fromisoformat(
                    metadata.get("validated_at", metadata["retrieved_at"])
                )
                expected_hash = metadata["content_sha256"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise EdgarCacheError(f"invalid EDGAR cache metadata for {url}") from exc
            if metadata.get("url") != url:
                raise EdgarCacheError(f"EDGAR cache URL mismatch for {url}")
            actual_hash = hashlib.sha256(body).hexdigest()
            if actual_hash != expected_hash:
                raise EdgarCacheError(f"EDGAR cache hash mismatch for {url}")
            if retrieved_at.tzinfo is None or validated_at.tzinfo is None:
                raise EdgarCacheError(f"EDGAR cache timestamps are timezone-naive for {url}")
            return _CachedResponse(
                body=body,
                retrieved_at=retrieved_at.astimezone(UTC),
                validated_at=validated_at.astimezone(UTC),
                content_sha256=actual_hash,
                etag=metadata.get("etag"),
                last_modified=metadata.get("last_modified"),
            )

    def delete(self, url: str) -> None:
        """Remove both halves of a cache entry after payload validation fails."""
        body_path, metadata_path = self._paths(url)
        with self._lock:
            body_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)

    def store(
        self,
        url: str,
        body: bytes,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> _CachedResponse:
        self.directory.mkdir(parents=True, exist_ok=True)
        body_path, metadata_path = self._paths(url)
        observed_now = self._now()
        if observed_now.tzinfo is None:
            raise EdgarCacheError("EDGAR cache clock must be timezone-aware")
        retrieved_at = observed_now.astimezone(UTC)
        content_sha256 = hashlib.sha256(body).hexdigest()
        metadata = {
            "schema_version": 1,
            "url": url,
            "retrieved_at": retrieved_at.isoformat(),
            "validated_at": retrieved_at.isoformat(),
            "content_sha256": content_sha256,
            "etag": etag,
            "last_modified": last_modified,
        }
        with self._lock:
            self._write_atomic(body_path, body)
            self._write_atomic(
                metadata_path,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        return _CachedResponse(
            body=body,
            retrieved_at=retrieved_at,
            validated_at=retrieved_at,
            content_sha256=content_sha256,
            etag=etag,
            last_modified=last_modified,
        )

    def revalidate(self, url: str, cached: _CachedResponse) -> _CachedResponse:
        observed_now = self._now()
        if observed_now.tzinfo is None:
            raise EdgarCacheError("EDGAR cache clock must be timezone-aware")
        validated_at = observed_now.astimezone(UTC)
        _, metadata_path = self._paths(url)
        metadata = {
            "schema_version": 1,
            "url": url,
            "retrieved_at": cached.retrieved_at.isoformat(),
            "validated_at": validated_at.isoformat(),
            "content_sha256": cached.content_sha256,
            "etag": cached.etag,
            "last_modified": cached.last_modified,
        }
        with self._lock:
            self._write_atomic(
                metadata_path,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        return _CachedResponse(
            body=cached.body,
            retrieved_at=cached.retrieved_at,
            validated_at=validated_at,
            content_sha256=cached.content_sha256,
            etag=cached.etag,
            last_modified=cached.last_modified,
        )


class _RequestThrottle:
    def __init__(
        self,
        requests_per_second: float,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._interval = 1.0 / requests_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        # Reserve a slot atomically, then sleep OUTSIDE the lock: sleeping while
        # holding the lock serializes every parallel worker behind one sleeper
        # and collapses the effective request rate to ~1/interval regardless of
        # worker count (observed: 4 workers -> 0.35 req/s vs 8 req/s budget).
        with self._lock:
            now = self._monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._interval
        delay = start - self._monotonic()
        if delay > 0:
            self._sleep(delay)


class EdgarClient:
    """SEC JSON client that enforces identity, rate, retry and cache contracts."""

    def __init__(
        self,
        *,
        contact_email: str,
        cache_dir: str | Path,
        max_requests_per_second: float = 8,
        cache_ttl_seconds: float = 900,
        cache_binary_resources: bool = True,
        timeout_seconds: float = 30,
        max_attempts: int = 4,
        backoff_base_seconds: float = 60,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.contact_email = validate_sec_contact(contact_email)
        if not 0 < max_requests_per_second <= 8:
            raise EdgarConfigurationError("EDGAR rate must be within (0, 8] requests/second")
        if cache_ttl_seconds < 0 or timeout_seconds <= 0 or max_attempts < 1:
            raise EdgarConfigurationError("EDGAR timeout, cache TTL or attempt count is invalid")
        if backoff_base_seconds < 0:
            raise EdgarConfigurationError("EDGAR backoff must be non-negative")

        self.user_agent = f"USInv/{__version__} {self.contact_email}"
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.cache_binary_resources = cache_binary_resources
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._cache = _ResponseCache(Path(cache_dir), self._now)
        self._throttle = _RequestThrottle(max_requests_per_second, monotonic=monotonic, sleep=sleep)

    @staticmethod
    def _validate_contact(value: str) -> str:
        """Backward-compatible wrapper for the shared SEC contact contract."""
        return validate_sec_contact(value)

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        cache_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> EdgarClient:
        env_name = config.settings.edgar.contact_env
        contact = os.environ.get(env_name)
        if contact is None or not contact.strip():
            raise EdgarConfigurationError(
                f"set {env_name} to a monitored contact email before using EDGAR"
            )
        resolved_cache = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(config.settings.paths.cache_dir) / "edgar"
        )
        return cls(
            contact_email=contact,
            cache_dir=resolved_cache,
            max_requests_per_second=config.settings.edgar.max_requests_per_second,
            **kwargs,
        )

    @staticmethod
    def normalize_cik(value: int | str) -> str:
        raw = str(value).strip().upper()
        if raw.startswith("CIK"):
            raw = raw[3:]
        if not raw.isdigit() or not 1 <= len(raw) <= 10 or int(raw) <= 0:
            raise EdgarConfigurationError(f"invalid CIK: {value!r}")
        return raw.zfill(10)

    @staticmethod
    def _url(path: str) -> str:
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or not path.startswith("/")
            or ".." in parsed.path.split("/")
        ):
            raise EdgarConfigurationError(f"unsafe EDGAR path: {path!r}")
        return f"{BASE_URL}{path}"

    def _headers(
        self,
        cached: _CachedResponse | None,
        *,
        accept: str = "application/json",
    ) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
        }
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        return headers

    def _is_fresh(self, cached: _CachedResponse) -> bool:
        observed_now = self._now()
        if observed_now.tzinfo is None:
            raise EdgarCacheError("EDGAR cache clock must be timezone-aware")
        age = observed_now.astimezone(UTC) - cached.validated_at
        if age < timedelta(0):
            raise EdgarCacheError("EDGAR cache timestamp is in the future")
        return age <= self.cache_ttl

    def _retry_delay(self, response: HttpResponse, attempt: int) -> float:
        retry_after = (_header(response.headers, "Retry-After") or "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), 600.0)
        return min(self.backoff_base_seconds * (2**attempt), 600.0)

    @staticmethod
    def _decode_payload(body: bytes, url: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(body, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgarPayloadError(f"SEC returned invalid JSON for {url}") from exc
        if not isinstance(payload, dict):
            raise EdgarPayloadError(f"SEC returned a non-object JSON payload for {url}")
        return payload

    @staticmethod
    def _validate_sec_url(url: str, *, allow_efts_query: bool = False) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_SEC_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or (
                parsed.query
                and not (
                    allow_efts_query
                    and parsed.hostname == "efts.sec.gov"
                    and parsed.path == "/LATEST/search-index"
                )
            )
            or parsed.fragment
            or ".." in parsed.path.split("/")
        ):
            raise EdgarConfigurationError(f"unsafe SEC resource URL: {url!r}")
        return url

    def _request_resource(
        self,
        url: str,
        *,
        refresh: bool,
        accept: str,
        allow_efts_query: bool = False,
    ) -> EdgarResource:
        url = self._validate_sec_url(url, allow_efts_query=allow_efts_query)
        persistent_cache = accept != "*/*" or self.cache_binary_resources
        cached = self._cache.load(url) if persistent_cache else None
        if cached is not None and not refresh and self._is_fresh(cached):
            return EdgarResource(
                url=url,
                retrieved_at=cached.retrieved_at,
                validated_at=cached.validated_at,
                content_sha256=cached.content_sha256,
                body=cached.body,
                from_cache=True,
                revalidated=False,
            )

        last_status: int | None = None
        rate_limit_rounds = 0
        attempt = 0
        while True:
            if (response_was_rate_limited := last_status == 429) and rate_limit_rounds >= 24:
                # Give up only after ~24 penalty windows (>= 4 hours of waits):
                # a multi-day archive run must not die to a temporary 429.
                break
            attempt = 0 if response_was_rate_limited else attempt
            self._throttle.wait()
            try:
                response = self._transport.get(
                    url,
                    self._headers(cached, accept=accept),
                    self.timeout_seconds,
                )
            except OSError as exc:
                if attempt + 1 == self.max_attempts:
                    raise EdgarHttpError(None, url, f"EDGAR network failure for {url}") from exc
                attempt += 1
                self._sleep(min(self.backoff_base_seconds * (2**attempt), 600.0))
                continue

            last_status = response.status
            if response.status == 304 and cached is not None:
                revalidated = self._cache.revalidate(url, cached)
                return EdgarResource(
                    url=url,
                    retrieved_at=revalidated.retrieved_at,
                    validated_at=revalidated.validated_at,
                    content_sha256=revalidated.content_sha256,
                    body=revalidated.body,
                    from_cache=True,
                    revalidated=True,
                )
            if response.status == 200:
                if not persistent_cache:
                    retrieved_at = self._now()
                    if retrieved_at.tzinfo is None:
                        raise EdgarCacheError("EDGAR cache clock must be timezone-aware")
                    return EdgarResource(
                        url=url,
                        retrieved_at=retrieved_at.astimezone(UTC),
                        validated_at=retrieved_at.astimezone(UTC),
                        content_sha256=hashlib.sha256(response.body).hexdigest(),
                        body=response.body,
                        from_cache=False,
                        revalidated=False,
                    )
                stored = self._cache.store(
                    url,
                    response.body,
                    etag=_header(response.headers, "ETag"),
                    last_modified=_header(response.headers, "Last-Modified"),
                )
                return EdgarResource(
                    url=url,
                    retrieved_at=stored.retrieved_at,
                    validated_at=stored.validated_at,
                    content_sha256=stored.content_sha256,
                    body=stored.body,
                    from_cache=False,
                    revalidated=False,
                )
            if response.status == 429:
                attempt += 1
            if response.status == 429 and attempt + 1 == self.max_attempts:
                # Temporary penalty window: wait it out (>=10 min) and restart the
                # attempt budget instead of aborting a multi-day archive shard.
                rate_limit_rounds += 1
                wait = max(self._retry_delay(response, attempt), 600.0)
                self._sleep(wait)
                continue
            if response.status not in RETRIABLE_STATUS_CODES or attempt + 1 == self.max_attempts:
                raise EdgarHttpError(
                    response.status,
                    url,
                    f"EDGAR request failed with HTTP {response.status} for {url}",
                )
            self._sleep(self._retry_delay(response, attempt))

        raise EdgarHttpError(last_status, url, f"EDGAR request exhausted retries for {url}")

    def _request(self, path: str, *, refresh: bool) -> EdgarDocument:
        resource = self._request_resource(
            self._url(path),
            refresh=refresh,
            accept="application/json",
        )
        try:
            payload = self._decode_payload(resource.body, resource.url)
        except EdgarPayloadError:
            self._cache.delete(resource.url)
            raise
        return EdgarDocument(
            url=resource.url,
            retrieved_at=resource.retrieved_at,
            validated_at=resource.validated_at,
            content_sha256=resource.content_sha256,
            payload=payload,
            from_cache=resource.from_cache,
            revalidated=resource.revalidated,
        )

    @staticmethod
    def _require_fields(
        document: EdgarDocument, fields: tuple[str, ...], kind: str
    ) -> EdgarDocument:
        missing = [field for field in fields if field not in document.payload]
        if missing:
            raise EdgarPayloadError(f"{kind} response missing fields: {missing}")
        return document

    def submissions(self, cik: int | str, *, refresh: bool = False) -> EdgarDocument:
        normalized = self.normalize_cik(cik)
        document = self._request(f"/submissions/CIK{normalized}.json", refresh=refresh)
        return self._require_fields(document, ("cik", "name", "filings"), "submissions")

    def companyfacts(self, cik: int | str, *, refresh: bool = False) -> EdgarDocument:
        normalized = self.normalize_cik(cik)
        document = self._request(f"/api/xbrl/companyfacts/CIK{normalized}.json", refresh=refresh)
        return self._require_fields(document, ("cik", "entityName", "facts"), "companyfacts")

    def company_tickers_exchange(self, *, refresh: bool = False) -> EdgarDocument:
        """Fetch the SEC's current discovery-only CIK/ticker/exchange associations."""
        resource = self._request_resource(
            COMPANY_TICKERS_EXCHANGE_URL,
            refresh=refresh,
            accept="application/json",
        )
        try:
            payload = self._decode_payload(resource.body, resource.url)
        except EdgarPayloadError:
            self._cache.delete(resource.url)
            raise
        document = EdgarDocument(
            url=resource.url,
            retrieved_at=resource.retrieved_at,
            validated_at=resource.validated_at,
            content_sha256=resource.content_sha256,
            payload=payload,
            from_cache=resource.from_cache,
            revalidated=resource.revalidated,
        )
        return self._require_fields(document, ("fields", "data"), "company tickers exchange")

    def full_text_search(
        self,
        query: str,
        *,
        start: date,
        end: date,
        forms: tuple[str, ...] = ("10-K", "10-Q", "20-F", "40-F", "8-K"),
        refresh: bool = False,
    ) -> EdgarDocument:
        """Fetch one hash-cached SEC EFTS query for discovery-only candidates."""

        normalized = query.strip().upper()
        if (
            not normalized
            or len(normalized) > 32
            or not re.fullmatch(r"[A-Z0-9./()\- ]+", normalized)
            or start > end
            or not forms
        ):
            raise EdgarConfigurationError("unsafe SEC full-text search query")
        url = f"{EFTS_SEARCH_URL}?{
            urlencode(
                {
                    'q': normalized,
                    'dateRange': 'custom',
                    'startdt': start.isoformat(),
                    'enddt': end.isoformat(),
                    'forms': ','.join(forms),
                }
            )
        }"
        resource = self._request_resource(
            url,
            refresh=refresh,
            accept="application/json",
            allow_efts_query=True,
        )
        try:
            payload = self._decode_payload(resource.body, resource.url)
        except EdgarPayloadError:
            self._cache.delete(resource.url)
            raise
        document = EdgarDocument(
            url=resource.url,
            retrieved_at=resource.retrieved_at,
            validated_at=resource.validated_at,
            content_sha256=resource.content_sha256,
            payload=payload,
            from_cache=resource.from_cache,
            revalidated=resource.revalidated,
        )
        return self._require_fields(document, ("hits", "aggregations"), "full-text search")

    def submission_history(self, filename: str, *, refresh: bool = False) -> EdgarDocument:
        """Fetch one SEC-declared older submissions page by its safe filename."""
        if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", filename):
            raise EdgarConfigurationError("unsafe submissions history filename")
        return self._request(f"/submissions/{filename}", refresh=refresh)

    def filing_resource(
        self,
        cik: int | str,
        accession: str,
        filename: str,
        *,
        refresh: bool = False,
    ) -> EdgarResource:
        """Fetch one safe file from an accession directory using shared etiquette/cache."""
        normalized_cik = self.normalize_cik(cik)
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise EdgarConfigurationError("invalid SEC accession")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", filename):
            raise EdgarConfigurationError("unsafe filing resource filename")
        url = "/".join(
            (
                ARCHIVE_BASE_URL,
                str(int(normalized_cik)),
                accession.replace("-", ""),
                quote(filename, safe="._-"),
            )
        )
        return self._request_resource(url, refresh=refresh, accept="*/*")
