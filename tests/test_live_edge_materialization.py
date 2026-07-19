from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from usinv.data.edgar.filing_xbrl import (
    FilingArchiveResource,
    FilingArchiveResult,
    parse_filing_xbrl,
)
from usinv.data.edgar.live_edge import materialize_live_edge
from usinv.data.edgar.pit_store import PitStoreBuilder, read_pit_facts_as_of
from usinv.data.edgar.submissions import SubmissionFiling

CIK = 320193
ACCN = "0000320193-25-000001"
INLINE_XBRL = b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025">
  <body><ix:header><ix:resources>
    <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    <xbrli:context id="q1">
      <xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>
        <xbrli:endDate>2025-03-29</xbrli:endDate></xbrli:period>
    </xbrli:context>
  </ix:resources></ix:header>
  <ix:nonFraction name="us-gaap:Revenues" contextRef="q1" unitRef="USD">100</ix:nonFraction>
  </body>
</html>
"""


def _filing() -> SubmissionFiling:
    return SubmissionFiling(
        cik=CIK,
        accession=ACCN,
        form="10-Q",
        filing_date=date(2025, 5, 2),
        accepted=datetime(2025, 5, 2, 20, tzinfo=UTC),
        report_date=date(2025, 3, 31),
        primary_document="fixture-20250331.htm",
        source_url="https://data.sec.gov/submissions/CIK0000320193.json",
        source_sha256="a" * 64,
    )


def test_live_edge_batch_flows_through_same_pit_builder_without_future_read(
    tmp_path: Path,
) -> None:
    filing = _filing()
    parsed = parse_filing_xbrl(
        INLINE_XBRL,
        filing=filing,
        source_document=filing.primary_document,
    )
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    primary = archive_dir / filing.primary_document
    primary.write_bytes(INLINE_XBRL)
    archive_sha = hashlib.sha256(INLINE_XBRL).hexdigest()
    archive = FilingArchiveResult(
        filing.accession,
        archive_sha,
        archive_dir,
        (
            FilingArchiveResource(
                filing.primary_document,
                "https://www.sec.gov/fixture",
                archive_sha,
                len(INLINE_XBRL),
            ),
        ),
        False,
    )

    edge = materialize_live_edge(filing, archive, parsed, (), tmp_path / "edge")
    cached = materialize_live_edge(filing, archive, parsed, (), tmp_path / "edge")
    pit = PitStoreBuilder(output_dir=tmp_path / "pit").build([edge.pit_input()])

    assert not edge.from_cache and cached.from_cache
    assert edge.facts_raw_rows == 1
    before = read_pit_facts_as_of(
        pit.table_path("facts_pit"),
        datetime(2025, 5, 2, 19, 59, tzinfo=UTC),
    )
    after = read_pit_facts_as_of(
        pit.table_path("facts_pit"),
        filing.accepted,
    )
    assert before.num_rows == 0
    assert after.num_rows == 1
    assert after.column("tag").to_pylist() == ["Revenues"]


def test_live_edge_preserves_but_quarantines_decimal128_overflow(tmp_path: Path) -> None:
    filing = _filing()
    body = INLINE_XBRL.replace(
        b'unitRef="USD">100',
        b'unitRef="USD">1000000000000000000000000000000',
    )
    parsed = parse_filing_xbrl(
        body,
        filing=filing,
        source_document=filing.primary_document,
    )
    archive = FilingArchiveResult(
        filing.accession,
        hashlib.sha256(body).hexdigest(),
        tmp_path,
        (),
        False,
    )

    edge = materialize_live_edge(filing, archive, parsed, (), tmp_path / "edge")

    assert edge.filing_fact_rows == 1
    assert edge.facts_raw_rows == 0
    assert [item.kind for item in edge.issues] == ["precision_exceeds_fsds_decimal_contract"]
