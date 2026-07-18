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
from usinv.data.edgar.fsds import (
    FsdsIngestError,
    FsdsIngestor,
    FsdsIngestResult,
    FsdsSchemaError,
    FsdsTableArtifact,
    read_consolidated_facts_as_of,
)
from usinv.data.edgar.pit_store import (
    PitInputBatch,
    PitStoreBuilder,
    PitStoreConflictError,
    PitStoreError,
    PitStoreResult,
    PitTableArtifact,
    read_pit_facts_as_of,
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
    "FsdsIngestError",
    "FsdsIngestResult",
    "FsdsIngestor",
    "FsdsPayloadError",
    "FsdsQuarter",
    "FsdsSchemaError",
    "FsdsSyncResult",
    "FsdsTableArtifact",
    "PitInputBatch",
    "PitStoreBuilder",
    "PitStoreConflictError",
    "PitStoreError",
    "PitStoreResult",
    "PitTableArtifact",
    "fsds_quarter_range",
    "read_consolidated_facts_as_of",
    "read_pit_facts_as_of",
    "validate_sec_contact",
]
