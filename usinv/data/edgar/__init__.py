"""SEC EDGAR adapters."""

from usinv.data.edgar.bulk import (
    FsdsArchiveClient,
    FsdsArchiveError,
    FsdsArchiveRecord,
    FsdsPayloadError,
    FsdsQuarter,
    FsdsSyncResult,
    fsds_quarter_range,
)
from usinv.data.edgar.client import (
    EdgarCacheError,
    EdgarClient,
    EdgarConfigurationError,
    EdgarDocument,
    EdgarError,
    EdgarHttpError,
    EdgarPayloadError,
    validate_sec_contact,
)

__all__ = [
    "EdgarCacheError",
    "EdgarClient",
    "EdgarConfigurationError",
    "EdgarDocument",
    "EdgarError",
    "EdgarHttpError",
    "EdgarPayloadError",
    "FsdsArchiveClient",
    "FsdsArchiveError",
    "FsdsArchiveRecord",
    "FsdsPayloadError",
    "FsdsQuarter",
    "FsdsSyncResult",
    "fsds_quarter_range",
    "validate_sec_contact",
]
