from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

from usinv.data.edgar.filing_sic_snapshot import (
    acquire_filing_sic_snapshot,
    read_filing_sic_snapshot,
)


class _Client:
    def filing_resource(
        self,
        cik: int,
        accession: str,
        name: str,
        *,
        refresh: bool,
    ) -> SimpleNamespace:
        sic = 3571 if cik == 100 else None
        body = (
            b"<SEC-HEADER>\n"
            b"<ACCEPTANCE-DATETIME>20260717150000\n"
            + (
                f"STANDARD INDUSTRIAL CLASSIFICATION: SOFTWARE [{sic}]\n".encode()
                if sic is not None
                else b""
            )
            + b"</SEC-HEADER>"
        )
        return SimpleNamespace(
            body=body,
            url=f"https://sec.example/{cik}/{accession}/{name}",
            content_sha256=hashlib.sha256(body).hexdigest(),
        )


def test_filing_sic_acquisition_materializes_verified_records_and_gaps(
    monkeypatch,
    tmp_path,
) -> None:
    cutoff = datetime(2026, 7, 17, 20, tzinfo=UTC)
    cover = SimpleNamespace(
        snapshot_id="a" * 64,
        output_dir=tmp_path / "cover",
        merge=SimpleNamespace(
            as_of=cutoff,
            requested_ciks=(100, 200),
            archives=(
                SimpleNamespace(cik=100, accession="0000000100-26-000001"),
                SimpleNamespace(cik=200, accession="0000000200-26-000001"),
            ),
        ),
    )
    monkeypatch.setattr(
        "usinv.data.edgar.filing_sic_snapshot.read_cover_evidence_snapshot",
        lambda _path: cover,
    )

    created = acquire_filing_sic_snapshot(
        _Client(),
        cover,
        (100, 200),
        tmp_path,
    )
    cached = read_filing_sic_snapshot(created.output_dir)

    assert not created.from_cache
    assert [(row.cik, row.sic) for row in cached.records] == [(100, 3571)]
    assert cached.gaps == (200,)
    assert cached.cover_snapshot_id == cover.snapshot_id

