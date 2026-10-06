"""XbrlParser の検証 (EDINET の実際のコンテキスト構造を模した XBRL で確認)"""

from src.fetcher.xbrl_parser import KIND_ANNUAL, KIND_INTERIM, XbrlParser

FILING_TAG = "NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc"


def _xbrl(facts, contexts):
    """facts: [(要素名, コンテキスト, 値)]、contexts: {id: 期末日}"""
    ctx_xml = "".join(
        f'<xbrli:context id="{cid}"><xbrli:period><xbrli:instant>{end}</xbrli:instant>'
        f"</xbrli:period></xbrli:context>"
        for cid, end in contexts.items()
    )
    fact_xml = "".join(
        f'<jpcrp_cor:{name} contextRef="{ctx}">{"" if v is None else v}</jpcrp_cor:{name}>'
        for name, ctx, v in facts
    )
    return (
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:jpcrp_cor="http://example/jpcrp">'
        f"{ctx_xml}{fact_xml}</xbrli:xbrl>"
    ).encode()


ANNUAL_CTX = {
    "CurrentYearInstant": "2026-03-31",
    "CurrentYearDuration": "2026-03-31",
    "Prior1YearDuration": "2025-03-31",
}


def test_annual_consolidated_ignores_non_consolidated_and_segments():
    # 単体・セグメントの値を連結より先に並べても、連結 (メンバー無し) の値を採用する
    content = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", 300),
            ("OperatingIncome", "CurrentYearDuration_XSegmentReportableSegmentsMember", 5),
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", 1000),
            ("OperatingIncome", "CurrentYearDuration", 80),
            ("ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults", "Prior1YearDuration", 40),
            ("ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults", "CurrentYearDuration", 50),
            ("NetAssetsSummaryOfBusinessResults", "CurrentYearInstant", 400),
            ("TotalAssetsSummaryOfBusinessResults", "CurrentYearInstant", 1000),
            ("EquityToAssetRatioSummaryOfBusinessResults", "CurrentYearInstant", "0.415"),
            ("RateOfReturnOnEquitySummaryOfBusinessResults", "CurrentYearDuration", "0.138"),
            ("BasicEarningsLossPerShareSummaryOfBusinessResults", "CurrentYearDuration", "25.5"),
            ("NetAssetsPerShareSummaryOfBusinessResults", "CurrentYearInstant", "200"),
            # 配当・株式数は提出会社 (単体) の値として報告される
            ("DividendPaidPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "13"),
            ("TotalNumberOfIssuedSharesSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", "2000"),
        ],
        ANNUAL_CTX,
    )
    r = XbrlParser().parse_content(content)
    assert r["kind"] == KIND_ANNUAL
    assert r["consolidated"] is True
    assert r["period_end"] == "2026-03-31"
    assert r["sales"] == 1000
    assert r["operating_income"] == 80
    assert r["net_profit"] == 50 and r["prev_net_profit"] == 40
    assert r["equity_ratio"] == 41.5  # 小数 → %
    assert abs(r["roe"] - 13.8) < 1e-9
    assert r["eps"] == 25.5 and r["bps"] == 200
    assert r["dps"] == 13 and r["shares_outstanding"] == 2000


def test_ifrs_tags_are_read_and_bps_tag_quirk():
    # IFRS の "EquityToAssetRatioIFRS..." は BPS、自己資本比率は "RatioOfOwnersEquityToGrossAssets..."
    content = _xbrl(
        [
            ("NetSales", "CurrentYearDuration_NonConsolidatedMember", 273),  # 単体の損益計算書
            ("RevenueIFRSSummaryOfBusinessResults", "CurrentYearDuration", 2708),
            ("OperatingProfitLossIFRS", "CurrentYearDuration", -518),
            ("ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults", "CurrentYearDuration", -564),
            ("TotalAssetsIFRSSummaryOfBusinessResults", "CurrentYearInstant", 3107),
            ("EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults", "CurrentYearInstant", 794),
            ("RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults", "CurrentYearInstant", "0.256"),
            ("EquityToAssetRatioIFRSSummaryOfBusinessResults", "CurrentYearInstant", "692.80"),
            ("RateOfReturnOnEquityIFRSSummaryOfBusinessResults", "CurrentYearDuration", "-0.555"),
            ("BasicEarningsLossPerShareIFRSSummaryOfBusinessResults", "CurrentYearDuration", "-492.55"),
        ],
        ANNUAL_CTX,
    )
    r = XbrlParser().parse_content(content)
    assert r["sales"] == 2708
    assert r["operating_income"] == -518
    assert r["net_assets"] == 794
    assert abs(r["equity_ratio"] - 25.6) < 1e-9
    assert r["bps"] == 692.80
    assert abs(r["roe"] + 55.5) < 1e-9
    assert r["eps"] == -492.55


def test_interim_report_returns_balance_sheet_only():
    # 半期報告書: 半期分の損益や前期の値は返さず、期末時点の貸借対照表の項目だけを返す
    content = _xbrl(
        [
            ("TotalAssetsSummaryOfBusinessResults", "Prior1YearInstant", 162),
            ("TotalAssetsSummaryOfBusinessResults", "InterimInstant", 166),
            ("NetAssetsSummaryOfBusinessResults", "InterimInstant", 69),
            ("EquityToAssetRatioSummaryOfBusinessResults", "InterimInstant", "0.415"),
            ("NetSalesSummaryOfBusinessResults", "InterimDuration", 120),
            ("BasicEarningsLossPerShareSummaryOfBusinessResults", "InterimDuration", "88.34"),
            ("BasicEarningsLossPerShareSummaryOfBusinessResults", "Prior1YearDuration", "-957.83"),
        ],
        {"InterimInstant": "2026-09-30", "InterimDuration": "2026-09-30"},
    )
    r = XbrlParser().parse_content(content)
    assert r["kind"] == KIND_INTERIM
    assert r["period_end"] == "2026-09-30"
    assert r["total_assets"] == 166 and r["net_assets"] == 69
    assert abs(r["equity_ratio"] - 41.5) < 1e-9
    for pl_field in ("sales", "eps", "net_profit", "bps", "dps"):
        assert pl_field not in r


def test_non_consolidated_only_company_uses_non_consolidated_member():
    content = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", 500),
            ("NetIncomeLossSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", 30),
            ("NetAssetsSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", 300),
            ("TotalAssetsSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", 600),
        ],
        ANNUAL_CTX,
    )
    r = XbrlParser().parse_content(content)
    assert r["consolidated"] is False
    assert r["sales"] == 500 and r["net_profit"] == 30 and r["net_assets"] == 300


def test_nil_values_and_missing_items_are_omitted():
    content = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", 100),
            ("DividendPaidPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", None),
        ],
        ANNUAL_CTX,
    )
    r = XbrlParser().parse_content(content)
    assert r["sales"] == 100
    # 取れなかった項目はキー自体が無い (取り込みで既存値を消さない)
    assert "dps" not in r and "eps" not in r


def test_document_without_target_period_is_empty():
    content = _xbrl(
        [("NetSalesSummaryOfBusinessResults", "CurrentYTDDuration", 100)],
        {"CurrentYTDDuration": "2023-12-31"},
    )
    assert XbrlParser().parse_content(content) == {}


def test_shares_outstanding_prefers_filing_date_count_after_a_split():
    """期末後・提出前に株式分割があると、有価証券報告書の 1 株当たりの値は分割後の基準で算定し直される。
    株数も、期末時点 (要約表) ではなく、提出日現在の値を使い、基準を揃える (3443 は 3 分割で、期末
    17,474,210 株、提出日 52,422,630 株)"""
    content = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", 1000),
            ("NetAssetsSummaryOfBusinessResults", "CurrentYearInstant", 99510000000),
            ("NetAssetsPerShareSummaryOfBusinessResults", "CurrentYearInstant", "568.5"),
            ("TotalNumberOfIssuedSharesSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", 17474210),
            (FILING_TAG, "FilingDateInstant", 52422630),
            (FILING_TAG, "FilingDateInstant_OrdinaryShareMember", 52422630),
        ],
        ANNUAL_CTX,
    )
    assert XbrlParser().parse_content(content)["shares_outstanding"] == 52422630


def test_shares_outstanding_falls_back_to_summary_without_filing_date_total():
    """提出日現在の合計が無い (株式の種類ごとの値しか無い) 書類は、要約表の値を使う。0 や空も採用しない"""
    only_class = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", 1000),
            ("NetAssetsSummaryOfBusinessResults", "CurrentYearInstant", 400),
            ("TotalNumberOfIssuedSharesSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", 2000),
            (FILING_TAG, "FilingDateInstant_OrdinaryShareMember", 3000),
        ],
        ANNUAL_CTX,
    )
    assert XbrlParser().parse_content(only_class)["shares_outstanding"] == 2000
    zero = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", 1000),
            ("NetAssetsSummaryOfBusinessResults", "CurrentYearInstant", 400),
            ("TotalNumberOfIssuedSharesSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", 2000),
            (FILING_TAG, "FilingDateInstant", 0),
        ],
        ANNUAL_CTX,
    )
    assert XbrlParser().parse_content(zero)["shares_outstanding"] == 2000


def test_interim_report_also_uses_filing_date_shares():
    content = _xbrl(
        [
            ("NetSalesSummaryOfBusinessResults", "InterimDuration", 500),
            ("NetAssetsSummaryOfBusinessResults", "InterimInstant", 400),
            ("TotalNumberOfIssuedSharesSummaryOfBusinessResults", "InterimInstant_NonConsolidatedMember", 1359000),
            (FILING_TAG, "FilingDateInstant", 2726000),
        ],
        {"InterimInstant": "2026-06-30", "InterimDuration": "2026-06-30", "Prior1InterimDuration": "2025-06-30"},
    )
    r = XbrlParser().parse_content(content)
    assert r["kind"] == KIND_INTERIM and r["shares_outstanding"] == 2726000


def test_parser_version_is_bumped_so_old_results_are_reprocessed():
    from src.fetcher.xbrl_parser import PARSER_VERSION

    assert PARSER_VERSION >= 3
