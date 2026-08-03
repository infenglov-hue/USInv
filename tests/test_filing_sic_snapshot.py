from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.filing_sic_snapshot import (
    acquire_filing_sic_snapshot,
    read_filing_sic_snapshot,
    rebase_filing_sic_snapshot,
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


class _BdcClient:
    def filing_resource(
        self,
        cik: int,
        accession: str,
        name: str,
        *,
        refresh: bool,
    ) -> SimpleNamespace:
        body = (
            b"<SEC-HEADER>\n"
            b"<ACCEPTANCE-DATETIME>20260717150000\n"
            + f"CENTRAL INDEX KEY: {cik:010d}\n".encode()
            + b"SEC FILE NUMBER: 814-00663\n"
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


def test_filing_sic_acquisition_uses_pit_bounded_814_fallback(monkeypatch, tmp_path) -> None:
    cutoff = datetime(2026, 7, 17, 20, tzinfo=UTC)
    cover = SimpleNamespace(
        snapshot_id="a" * 64,
        output_dir=tmp_path / "cover",
        merge=SimpleNamespace(
            as_of=cutoff,
            requested_ciks=(1287750,),
            archives=(SimpleNamespace(cik=1287750, accession="0001287750-26-000006"),),
        ),
    )
    monkeypatch.setattr(
        "usinv.data.edgar.filing_sic_snapshot.read_cover_evidence_snapshot",
        lambda _path: cover,
    )

    created = acquire_filing_sic_snapshot(
        _BdcClient(),
        cover,
        (1287750,),
        tmp_path,
    )
    cached = read_filing_sic_snapshot(created.output_dir)

    assert cached.gaps == ()
    assert [(row.cik, row.sic, row.source_kind) for row in cached.records] == [
        (1287750, 6726, "sec_file_number_814")
    ]


def test_filing_sic_rebase_preserves_verified_headers_and_requires_same_archives(
    monkeypatch,
    tmp_path,
) -> None:
    cutoff = datetime(2026, 7, 17, 20, tzinfo=UTC)
    archive = SimpleNamespace(cik=100, accession="0000000100-26-000001")
    old_cover = SimpleNamespace(
        snapshot_id="a" * 64,
        output_dir=tmp_path / "old-cover",
        merge=SimpleNamespace(
            as_of=cutoff,
            requested_ciks=(100,),
            archives=(archive,),
        ),
    )
    new_cover = SimpleNamespace(
        snapshot_id="b" * 64,
        output_dir=tmp_path / "new-cover",
        merge=SimpleNamespace(
            as_of=cutoff,
            requested_ciks=(100,),
            archives=(archive,),
        ),
    )
    covers = {
        old_cover.output_dir: old_cover,
        new_cover.output_dir: new_cover,
    }
    monkeypatch.setattr(
        "usinv.data.edgar.filing_sic_snapshot.read_cover_evidence_snapshot",
        lambda path: covers[path],
    )
    source = acquire_filing_sic_snapshot(_Client(), old_cover, (100,), tmp_path / "source")

    rebased = rebase_filing_sic_snapshot(
        source,
        old_cover,
        new_cover,
        tmp_path / "rebased",
    )

    assert rebased.cover_snapshot_id == new_cover.snapshot_id
    assert rebased.records == source.records
    assert rebased.snapshot_id != source.snapshot_id
    changed_cover = SimpleNamespace(
        snapshot_id="c" * 64,
        output_dir=tmp_path / "changed-cover",
        merge=SimpleNamespace(
            as_of=cutoff,
            requested_ciks=(100,),
            archives=(),
        ),
    )
    covers[changed_cover.output_dir] = changed_cover
    with pytest.raises(EdgarPayloadError, match="exact provenance"):
        rebase_filing_sic_snapshot(
            source,
            old_cover,
            changed_cover,
            tmp_path / "rejected",
        )
