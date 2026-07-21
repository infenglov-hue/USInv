from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarConfigurationError, EdgarPayloadError, EdgarResource
from usinv.data.edgar.filing_xbrl import (
    CoverSecurityClass,
    archive_filing,
    extract_cover_security_classes,
    filing_facts_to_raw,
    parse_filing_xbrl,
    parse_presentation_linkbase,
    security_evidence_from_cover,
)
from usinv.data.edgar.securities import build_security_master
from usinv.data.edgar.submissions import SubmissionFiling

CIK = 320193
ACCN = "0000320193-25-000001"


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


INLINE_XBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025"
      xmlns:dei="http://xbrl.sec.gov/dei/2025"
      xmlns:fixture="http://fixture.example/2025">
  <body>
    <ix:header>
      <ix:resources>
        <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
        <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
        <xbrli:context id="duration">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-29</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <xbrli:context id="class-a">
          <xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
            <xbrli:segment>
              <xbrldi:explicitMember dimension="dei:StatementClassOfStockAxis">
                fixture:ClassAMember
              </xbrldi:explicitMember>
            </xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:instant>2025-03-29</xbrli:instant></xbrli:period>
        </xbrli:context>
      </ix:resources>
    </ix:header>
    <ix:nonFraction name="us-gaap:Revenues" contextRef="duration"
                    unitRef="USD" scale="6" decimals="-6">1,234</ix:nonFraction>
    <ix:nonFraction name="us-gaap:Revenues" contextRef="duration"
                    unitRef="USD" scale="9" decimals="-8">1.2</ix:nonFraction>
    <ix:nonFraction name="fixture:SegmentRevenue" contextRef="class-a"
                    unitRef="USD">42</ix:nonFraction>
    <ix:nonFraction name="fixture:NumberOfVendors" contextRef="class-a"
                    unitRef="pure" format="ixt-sec:numwordsen">two</ix:nonFraction>
    <ix:nonNumeric name="dei:TradingSymbol" contextRef="class-a">AAPL</ix:nonNumeric>
    <ix:nonNumeric name="dei:SecurityExchangeName" contextRef="class-a">
      The Nasdaq Stock Market LLC
    </ix:nonNumeric>
    <ix:nonNumeric name="dei:Security12bTitle" contextRef="class-a">Common Stock</ix:nonNumeric>
    <ix:nonFraction name="dei:EntityCommonStockSharesOutstanding" contextRef="class-a"
                    unitRef="shares" decimals="0">15000000000</ix:nonFraction>
  </body>
</html>
"""


PRESENTATION = b"""<?xml version="1.0"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
  <link:presentationLink xlink:role="http://fixture/role/ConsolidatedStatementsOfOperations">
    <link:loc xlink:label="root" xlink:href="fixture.xsd#fixture_StatementAbstract"/>
    <link:loc xlink:label="revenue" xlink:href="fixture.xsd#fixture_NetSales"/>
    <link:presentationArc xlink:from="root" xlink:to="revenue" order="1"/>
  </link:presentationLink>
</link:linkbase>
"""


LABEL = b"""<?xml version="1.0"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
  <link:labelLink xlink:role="http://www.xbrl.org/2003/role/link">
    <link:loc xlink:label="loc_revenue" xlink:href="fixture.xsd#fixture_NetSales"/>
    <link:label xlink:label="lab_revenue">Net sales</link:label>
    <link:labelArc xlink:from="loc_revenue" xlink:to="lab_revenue"/>
  </link:labelLink>
</link:linkbase>
"""


def test_inline_xbrl_preserves_decimal_context_dimensions_and_acceptance() -> None:
    filing = _filing()
    result = parse_filing_xbrl(
        INLINE_XBRL,
        filing=filing,
        source_document=filing.primary_document,
    )

    revenues = next(item for item in result.facts if item.tag == "Revenues")
    assert revenues.value == Decimal("1234000000")
    assert revenues.ddate == date(2025, 3, 31) and revenues.qtrs == 1
    assert revenues.accepted == filing.accepted
    assert not revenues.dimensions

    dimensional = next(item for item in result.facts if item.tag == "SegmentRevenue")
    assert dimensional.custom and dimensional.dimensions
    words = next(item for item in result.facts if item.tag == "NumberOfVendors")
    assert words.value == Decimal(2)
    raw = filing_facts_to_raw(result)
    assert [item.tag for item in raw] == ["Revenues"]
    assert raw[0].value == Decimal("1234000000")


def test_cover_facts_keep_share_class_dimension_together() -> None:
    result = parse_filing_xbrl(
        INLINE_XBRL,
        filing=_filing(),
        source_document="fixture-20250331.htm",
    )

    classes = extract_cover_security_classes(result)

    assert len(classes) == 1
    assert classes[0].ticker == "AAPL"
    assert classes[0].exchange == "The Nasdaq Stock Market LLC"
    assert classes[0].class_title == "Common Stock"
    assert classes[0].dimensions == (("dei:StatementClassOfStockAxis", "fixture:ClassAMember"),)
    assert classes[0].shares_outstanding == Decimal("15000000000")
    assert classes[0].shares_evidence_pointer is not None

    security, symbol = security_evidence_from_cover(_filing(), classes[0])
    master = build_security_master([security], [symbol])
    assert master.resolve("AAPL", "NASDAQ", date(2025, 5, 1)).status == "unmapped"
    assert master.resolve("AAPL", "NASDAQ", date(2025, 5, 2)).security_id == security.security_id


def test_warrant_title_mentioning_common_stock_remains_non_common() -> None:
    cover = CoverSecurityClass(
        "warrant",
        "ONEW",
        "The Nasdaq Stock Market LLC",
        "Warrants to purchase Common Stock",
        (),
        ("fixture://warrant",),
        None,
        None,
    )

    security, _symbol = security_evidence_from_cover(_filing(), cover)

    assert security.security_type == "other"


def test_one_day_duration_cover_context_is_valid_identity_evidence() -> None:
    body = INLINE_XBRL.replace(
        b"<xbrli:instant>2025-03-29</xbrli:instant>",
        b"<xbrli:startDate>2025-03-29</xbrli:startDate><xbrli:endDate>2025-03-29</xbrli:endDate>",
    )

    result = parse_filing_xbrl(
        body,
        filing=_filing(),
        source_document="one-day-cover.htm",
    )

    classes = extract_cover_security_classes(result)
    assert len(classes) == 1
    assert (classes[0].ticker, classes[0].exchange) == (
        "AAPL",
        "The Nasdaq Stock Market LLC",
    )
    assert classes[0].shares_outstanding == Decimal("15000000000")
    invalid_tags = {item.tag for item in result.issues if item.kind == "invalid_period"}
    assert "SegmentRevenue" in invalid_tags
    assert not invalid_tags.intersection(
        {
            "TradingSymbol",
            "SecurityExchangeName",
            "Security12bTitle",
            "EntityCommonStockSharesOutstanding",
        }
    )


def test_dimensionless_entity_shares_attach_only_to_the_single_common_class() -> None:
    body = INLINE_XBRL.replace(
        b"</ix:resources>",
        b"""<xbrli:context id="entity-shares">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-04-25</xbrli:instant></xbrli:period>
        </xbrli:context></ix:resources>""",
    ).replace(
        b'name="dei:EntityCommonStockSharesOutstanding" contextRef="class-a"',
        b'name="dei:EntityCommonStockSharesOutstanding" contextRef="entity-shares"',
    )

    result = parse_filing_xbrl(
        body,
        filing=_filing(),
        source_document="dimensionless-shares.htm",
    )

    classes = extract_cover_security_classes(result)
    assert len(classes) == 1
    assert classes[0].shares_outstanding == Decimal("15000000000")
    assert classes[0].shares_evidence_pointer is not None
    assert "/entity-shares/EntityCommonStockSharesOutstanding" in (
        classes[0].shares_evidence_pointer
    )


def test_presentation_linkbase_recovers_only_accession_versioned_custom_row() -> None:
    rows = parse_presentation_linkbase(PRESENTATION, accession=ACCN, label_body=LABEL)

    assert len(rows) == 1
    assert rows[0].stmt == "IS" and rows[0].line == 1
    assert rows[0].tag == "NetSales" and rows[0].version == ACCN
    assert rows[0].plabel == "Net sales"


def test_malformed_or_external_entity_xbrl_does_not_resolve_data() -> None:
    body = (
        b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM "file:///secret">]><x>&ext;</x>"""
    )
    with pytest.raises(EdgarPayloadError, match="well-formed"):
        parse_filing_xbrl(body, filing=_filing(), source_document="unsafe.xml")


def test_inline_xbrl_accepts_fixed_html_named_entities_without_a_dtd() -> None:
    body = INLINE_XBRL.replace(b"Common Stock", b"Common&nbsp;Stock", 1)

    parsed = parse_filing_xbrl(body, filing=_filing(), source_document="named-entity.htm")

    assert extract_cover_security_classes(parsed)[0].class_title == "Common Stock"


def test_context_entity_and_duplicate_identity_fail_closed() -> None:
    wrong_entity = parse_filing_xbrl(
        INLINE_XBRL.replace(b"0000320193", b"external-id"),
        filing=_filing(),
        source_document="wrong-entity.htm",
    )
    assert not wrong_entity.facts
    assert {item.kind for item in wrong_entity.issues} == {
        "entity_mismatch",
        "missing_context",
    }

    duplicate_contexts = b"""<xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025">
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <xbrli:context id="duplicate">
        <xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-03-29</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:context id="duplicate">
        <xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-03-30</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:context id="duplicate">
        <xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-03-29</xbrli:instant></xbrli:period>
      </xbrli:context>
      <us-gaap:Assets contextRef="duplicate" unitRef="USD" decimals="0">1</us-gaap:Assets>
    </xbrli:xbrl>"""
    conflicted = parse_filing_xbrl(
        duplicate_contexts,
        filing=_filing(),
        source_document="duplicate-context.xml",
    )
    assert not conflicted.facts
    assert [item.kind for item in conflicted.issues].count("context_conflict") == 1


class FakeArchiveClient:
    def __init__(self, resources: dict[str, bytes]) -> None:
        self.resources = resources
        self.calls: list[str] = []

    def filing_resource(
        self,
        cik: int,
        accession: str,
        filename: str,
        *,
        refresh: bool = False,
    ) -> EdgarResource:
        assert cik == CIK and accession == ACCN and not refresh
        self.calls.append(filename)
        body = self.resources[filename]
        observed = datetime(2026, 7, 19, tzinfo=UTC)
        return EdgarResource(
            url=f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession}/{filename}",
            retrieved_at=observed,
            validated_at=observed,
            content_sha256=hashlib.sha256(body).hexdigest(),
            body=body,
            from_cache=False,
            revalidated=False,
        )


def test_filing_archive_is_content_addressed_and_hash_verified(tmp_path: Path) -> None:
    index = json.dumps(
        {
            "directory": {
                "item": [
                    {"name": "fixture-20250331.htm"},
                    {"name": "fixture-20250331_pre.xml"},
                    {"name": "fixture-20250331_lab.xml"},
                    {"name": "fixture-20250331.xsd"},
                    {"name": "rendered.jpg"},
                ]
            }
        }
    ).encode()
    resources = {
        "index.json": index,
        "fixture-20250331.htm": INLINE_XBRL,
        "fixture-20250331_pre.xml": PRESENTATION,
        "fixture-20250331_lab.xml": LABEL,
        "fixture-20250331.xsd": b"<schema/>",
    }
    client = FakeArchiveClient(resources)

    created = archive_filing(client, _filing(), tmp_path)
    cached = archive_filing(client, _filing(), tmp_path)

    assert not created.from_cache and cached.from_cache
    assert created.snapshot_id == cached.snapshot_id
    assert "rendered.jpg" not in client.calls
    assert "fixture-20250331_pre.xml" not in client.calls
    assert "fixture-20250331_lab.xml" not in client.calls
    assert "fixture-20250331.xsd" not in client.calls
    assert created.output_dir.joinpath("fixture-20250331.htm").is_file()

    created.output_dir.joinpath("fixture-20250331.htm").write_bytes(b"tampered")
    with pytest.raises(EdgarPayloadError, match="hash mismatch"):
        archive_filing(client, _filing(), tmp_path)


def test_archive_client_rejects_unsafe_filename_before_network(tmp_path: Path) -> None:
    from usinv.data.edgar.client import EdgarClient

    client = EdgarClient(contact_email="ops@usinv.dev", cache_dir=tmp_path)
    with pytest.raises(EdgarConfigurationError, match="unsafe"):
        client.filing_resource(CIK, ACCN, "../secret")
