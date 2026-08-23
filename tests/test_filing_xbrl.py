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
    parse_plain_html_cover_table,
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


def test_plain_html_section_12b_table_produces_provenanced_cover_class() -> None:
    body = b"""
    <html><body><table>
      <tr>
        <th>Title of each class</th>
        <th>Trading Symbol(s)</th>
        <th>Name of each exchange on which registered</th>
      </tr>
      <tr>
        <td>Class A common stock, $0.001 par value</td>
        <td>AAPL</td>
        <td>The Nasdaq Stock Market LLC</td>
      </tr>
    </table></body></html>
    """

    result = parse_plain_html_cover_table(
        body,
        filing=_filing(),
        source_document="plain-cover.htm",
    )
    classes = extract_cover_security_classes(result)

    assert len(classes) == 1
    assert (
        classes[0].ticker,
        classes[0].exchange,
        classes[0].class_title,
    ) == (
        "AAPL",
        "The Nasdaq Stock Market LLC",
        "Class A common stock, $0.001 par value",
    )
    assert all(
        "plain-cover.htm/plain-html-cover-row-1" in pointer
        for pointer in classes[0].evidence_pointers
    )


def test_plain_html_cover_requires_explicit_headers_and_rejects_dtd() -> None:
    result = parse_plain_html_cover_table(
        b"<html><body><table><tr><td>AAPL</td><td>NASDAQ</td></tr></table></body></html>",
        filing=_filing(),
        source_document="unlabeled.htm",
    )

    assert not result.facts
    with pytest.raises(EdgarPayloadError, match="prohibited DTD"):
        parse_plain_html_cover_table(
            b"<!DOCTYPE html><html><body></body></html>",
            filing=_filing(),
            source_document="unsafe.htm",
        )


def test_identical_duplicate_cover_facts_do_not_hide_the_security_class() -> None:
    exchange = b"""<ix:nonNumeric name="dei:SecurityExchangeName" contextRef="class-a">
      The Nasdaq Stock Market LLC
    </ix:nonNumeric>"""
    duplicated = INLINE_XBRL.replace(exchange, exchange + exchange)

    classes = extract_cover_security_classes(
        parse_filing_xbrl(
            duplicated,
            filing=_filing(),
            source_document="duplicate-cover.htm",
        )
    )

    assert len(classes) == 1
    assert classes[0].ticker == "AAPL"
    assert len(classes[0].evidence_pointers) == 4


def test_single_class_cover_accepts_exchange_axis_context_split() -> None:
    body = INLINE_XBRL.replace(
        b"</ix:resources>",
        b"""<xbrli:context id="exchange">
          <xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
            <xbrli:segment>
              <xbrldi:explicitMember dimension="dei:EntityListingsExchangeAxis">
                fixture:XNASMember
              </xbrldi:explicitMember>
            </xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:instant>2025-03-29</xbrli:instant></xbrli:period>
        </xbrli:context></ix:resources>""",
    ).replace(
        b'name="dei:SecurityExchangeName" contextRef="class-a"',
        b'name="dei:SecurityExchangeName" contextRef="exchange"',
    )

    classes = extract_cover_security_classes(
        parse_filing_xbrl(
            body,
            filing=_filing(),
            source_document="exchange-axis-cover.htm",
        )
    )

    assert len(classes) == 1
    assert (classes[0].ticker, classes[0].exchange, classes[0].class_title) == (
        "AAPL",
        "The Nasdaq Stock Market LLC",
        "Common Stock",
    )
    assert classes[0].dimensions == (("dei:StatementClassOfStockAxis", "fixture:ClassAMember"),)


def test_exchange_axis_fallback_keeps_multiple_classes_unresolved() -> None:
    body = (
        INLINE_XBRL.replace(
            b"</ix:resources>",
            b"""<xbrli:context id="exchange">
          <xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
            <xbrli:segment>
              <xbrldi:explicitMember dimension="dei:EntityListingsExchangeAxis">
                fixture:XNASMember
              </xbrldi:explicitMember>
            </xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:instant>2025-03-29</xbrli:instant></xbrli:period>
        </xbrli:context></ix:resources>""",
        )
        .replace(
            b'name="dei:SecurityExchangeName" contextRef="class-a"',
            b'name="dei:SecurityExchangeName" contextRef="exchange"',
        )
        .replace(
            b'<ix:nonNumeric name="dei:TradingSymbol" contextRef="class-a">AAPL</ix:nonNumeric>',
            b"""<ix:nonNumeric name="dei:TradingSymbol" contextRef="class-a">AAPL</ix:nonNumeric>
        <ix:nonNumeric name="dei:TradingSymbol" contextRef="class-a">AAPL.B</ix:nonNumeric>""",
        )
    )

    classes = extract_cover_security_classes(
        parse_filing_xbrl(
            body,
            filing=_filing(),
            source_document="ambiguous-exchange-axis-cover.htm",
        )
    )

    assert not classes


def test_missing_exchange_requires_one_allowed_listing_pair() -> None:
    body = INLINE_XBRL.replace(
        b"""<ix:nonNumeric name="dei:SecurityExchangeName" contextRef="class-a">
      The Nasdaq Stock Market LLC
    </ix:nonNumeric>""",
        b"",
    )
    result = parse_filing_xbrl(
        body,
        filing=_filing(),
        source_document="missing-exchange.htm",
    )

    assert not extract_cover_security_classes(result)
    assert not extract_cover_security_classes(
        result,
        allowed_pairs=frozenset({("AAPL", "NASDAQ"), ("AAPL", "NYSE")}),
    )
    classes = extract_cover_security_classes(
        result,
        allowed_pairs=frozenset({("AAPL", "NASDAQ")}),
    )

    assert len(classes) == 1
    assert (classes[0].ticker, classes[0].exchange) == ("AAPL", "NASDAQ")
    assert classes[0].exchange_from_allowed_pair


def test_missing_title_requires_unique_pair_and_explicit_common_shares() -> None:
    body = (
        INLINE_XBRL.replace(
            (
                b'    <ix:nonNumeric name="dei:Security12bTitle" contextRef="class-a">'
                b"Common Stock</ix:nonNumeric>\n"
            ),
            b"",
        )
        .replace(
            b"</ix:resources>",
            b"""<xbrli:context id="entity-shares">
          <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>
          <xbrli:period><xbrli:instant>2025-04-25</xbrli:instant></xbrli:period>
        </xbrli:context></ix:resources>""",
        )
        .replace(
            b'name="dei:EntityCommonStockSharesOutstanding" contextRef="class-a"',
            b'name="dei:EntityCommonStockSharesOutstanding" contextRef="entity-shares"',
        )
    )
    result = parse_filing_xbrl(
        body,
        filing=_filing(),
        source_document="missing-title.htm",
    )

    assert not extract_cover_security_classes(result)
    classes = extract_cover_security_classes(
        result,
        allowed_pairs=frozenset({("AAPL", "NASDAQ")}),
    )

    assert len(classes) == 1
    assert classes[0].class_title == "Common Stock"
    assert classes[0].shares_outstanding == Decimal("15000000000")


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


@pytest.mark.parametrize(
    "class_title",
    (
        "Class A ordinary shares, par value $0.0001 per share",
        "Subordinate Voting Shares",
    ),
)
def test_ordinary_and_subordinate_voting_shares_are_common_stock(
    class_title: str,
) -> None:
    cover = CoverSecurityClass(
        "class-a",
        "ONE",
        "The Nasdaq Stock Market LLC",
        class_title,
        (),
        ("fixture://ordinary-share",),
        None,
        None,
    )

    security, _symbol = security_evidence_from_cover(_filing(), cover)

    assert security.security_type == "common_stock"


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


def test_plain_prospectus_listing_statement_requires_an_exact_expected_pair() -> None:
    body = b"""<html><body><p>
      We have applied to list our common shares on the Nasdaq Global Select
      Market (&quot;Nasdaq&quot;) under the symbol &quot;INIO.&quot;
    </p></body></html>"""

    parsed = parse_plain_html_cover_table(
        body,
        filing=_filing(),
        source_document="prospectus.htm",
        expected_listing_pair=("INIO", "NASDAQ"),
    )
    classes = extract_cover_security_classes(parsed)

    assert len(classes) == 1
    assert (
        classes[0].ticker,
        classes[0].exchange,
        classes[0].class_title,
    ) == ("INIO", "NASDAQ", "Common Shares")


def test_plain_prospectus_listing_accepts_replacement_quote_around_exact_ticker() -> None:
    parsed = parse_plain_html_cover_table(
        (
            '<html><head><meta charset="utf-8"></head><body>'
            "We have applied to list our common stock on the "
            "New York Stock Exchange (the \ufffdNYSE\ufffd) under the symbol "
            "\ufffdCSQR\ufffd.</body></html>"
        ).encode("utf-8"),
        filing=_filing(),
        source_document="prospectus.htm",
        expected_listing_pair=("CSQR", "NYSE"),
    )

    assert extract_cover_security_classes(parsed)[0].ticker == "CSQR"


@pytest.mark.parametrize(
    "statement",
    (
        "We intend to apply to list our common stock on NYSE under the symbol CSQR.",
        "We have applied to list our preferred stock on NYSE under the symbol CSQR.",
        "We have applied to list our common stock on Nasdaq under the symbol WRONG.",
    ),
)
def test_plain_prospectus_listing_statement_fails_closed(statement: str) -> None:
    parsed = parse_plain_html_cover_table(
        f"<html><body>{statement}</body></html>".encode(),
        filing=_filing(),
        source_document="prospectus.htm",
        expected_listing_pair=("CSQR", "NYSE"),
    )

    assert not parsed.facts


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


def test_live_edge_archive_can_include_only_presentation_linkbases(tmp_path: Path) -> None:
    index = json.dumps(
        {
            "directory": {
                "item": [
                    {"name": "fixture-20250331.htm"},
                    {"name": "fixture-20250331_pre.xml"},
                    {"name": "fixture-20250331_lab.xml"},
                    {"name": f"{ACCN}.txt"},
                    {"name": "fixture-20250331.xsd"},
                ]
            }
        }
    ).encode()
    resources = {
        "index.json": index,
        "fixture-20250331.htm": INLINE_XBRL,
        "fixture-20250331_pre.xml": PRESENTATION,
        "fixture-20250331_lab.xml": LABEL,
        f"{ACCN}.txt": b"STANDARD INDUSTRIAL CLASSIFICATION: SOFTWARE [7372]",
        "fixture-20250331.xsd": b"<schema/>",
    }
    client = FakeArchiveClient(resources)

    archive = archive_filing(
        client,
        _filing(),
        tmp_path,
        include_presentation=True,
        include_filing_header=True,
    )

    names = {item.name for item in archive.resources}
    assert "fixture-20250331_pre.xml" in names
    assert "fixture-20250331_lab.xml" in names
    assert f"{ACCN}.txt" in names
    assert "fixture-20250331.xsd" not in names


def test_archive_publish_retries_transient_windows_permission_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import usinv.data.edgar.filing_xbrl as filing_xbrl

    original_replace = Path.replace
    attempts = 0

    def flaky_replace(source: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    source = tmp_path / "temporary"
    target = tmp_path / "published"
    source.mkdir()

    filing_xbrl._replace_directory_with_retry(
        source,
        target,
        initial_delay_seconds=0,
    )

    assert attempts == 2
    assert target.is_dir()


def test_archive_client_rejects_unsafe_filename_before_network(tmp_path: Path) -> None:
    from usinv.data.edgar.client import EdgarClient

    client = EdgarClient(contact_email="ops@usinv.dev", cache_dir=tmp_path)
    with pytest.raises(EdgarConfigurationError, match="unsafe"):
        client.filing_resource(CIK, ACCN, "../secret")
