"""EDINET XBRL パーサー

有価証券報告書・半期報告書 (訂正を含む) の XBRL から、評価に使う財務値を取り出す。

値は原則として「主要な経営指標等の推移」(要素名が *SummaryOfBusinessResults) から読む。
この表は会計基準 (日本基準 / IFRS / 米国基準) を問わず全社が開示し、連結値は追加の
メンバーが付かないコンテキスト (例: "CurrentYearDuration")、単体値は
"_NonConsolidatedMember" 付きのコンテキストで報告される。

- コンテキストは完全一致で照合する。部分一致では単体・セグメント・株主資本の内訳
  (例: "CurrentYearDuration_NonConsolidatedMember") の値を拾ってしまう
- 連結の値が 1 つも無い会社 (子会社の無い会社) は単体の値を使う
- 配当と発行済株式数は提出会社 (単体) の値として報告されるため、常に単体を許容する
- 半期報告書は期中の値 (半期分の利益など) しか持たないため、貸借対照表の項目
  (総資産・純資産・自己資本比率・発行済株式数) だけを返す。年間の利益や 1 株当たり
  指標は有価証券報告書の値を使い続ける
- 比率 (ROE・自己資本比率) は XBRL では小数 (0.138 = 13.8%) なので % に換算して返す
"""

import logging
import zipfile
from typing import Any, Dict, Optional

import lxml.etree as et

# 抽出ロジックを変えたら上げる。古い版で処理した書類は取り込み直す
PARSER_VERSION = 2

KIND_ANNUAL = "annual"
KIND_INTERIM = "interim"

NON_CONSOLIDATED = "_NonConsolidatedMember"

# 期間ごとのコンテキスト名
_CONTEXTS = {
    KIND_ANNUAL: {
        "duration": "CurrentYearDuration",
        "instant": "CurrentYearInstant",
        "prior_duration": "Prior1YearDuration",
    },
    KIND_INTERIM: {
        "duration": "InterimDuration",
        "instant": "InterimInstant",
        "prior_duration": "Prior1InterimDuration",
    },
}


class XbrlParser:
    """EDINET XBRL パーサー"""

    # 純利益 (当期は net_profit、前期は prev_net_profit として読む)
    NET_PROFIT_TAGS = (
        "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
        "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
        "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
        "NetIncomeLossSummaryOfBusinessResults",  # 単体のみの会社
    )

    # (取り出す項目, 期間の種類, 候補の要素名 (優先順), 単体を常に許容するか)
    # 注意: IFRS の "EquityToAssetRatioIFRSSummaryOfBusinessResults" は名前に反して
    # 「1 株当たり親会社所有者帰属持分」(BPS)。自己資本比率は "RatioOfOwnersEquityToGrossAssets"
    SUMMARY_FIELDS: list[tuple[str, str, tuple[str, ...], bool]] = [
        (
            "sales",
            "duration",
            (
                "NetSalesSummaryOfBusinessResults",
                "RevenueIFRSSummaryOfBusinessResults",
                "RevenuesUSGAAPSummaryOfBusinessResults",
                "OperatingRevenueSummaryOfBusinessResults",
                "OperatingRevenue1SummaryOfBusinessResults",
                "OperatingRevenue2SummaryOfBusinessResults",
                "BusinessRevenueSummaryOfBusinessResults",
                "GrossOperatingRevenueSummaryOfBusinessResults",
                "OrdinaryIncomeSummaryOfBusinessResults",  # 銀行等の経常収益
                "NetSalesIFRSSummaryOfBusinessResults",
            ),
            False,
        ),
        ("net_profit", "duration", NET_PROFIT_TAGS, False),
        (
            "total_assets",
            "instant",
            (
                "TotalAssetsSummaryOfBusinessResults",
                "TotalAssetsIFRSSummaryOfBusinessResults",
                "TotalAssetsUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "net_assets",
            "instant",
            (
                "NetAssetsSummaryOfBusinessResults",
                "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
                "EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "equity_ratio",
            "instant",
            (
                "EquityToAssetRatioSummaryOfBusinessResults",
                "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults",
                "EquityToAssetRatioUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "roe",
            "duration",
            (
                "RateOfReturnOnEquitySummaryOfBusinessResults",
                "RateOfReturnOnEquityIFRSSummaryOfBusinessResults",
                "RateOfReturnOnEquityUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "eps",
            "duration",
            (
                "BasicEarningsLossPerShareSummaryOfBusinessResults",
                "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
                "BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "bps",
            "instant",
            (
                "NetAssetsPerShareSummaryOfBusinessResults",
                "EquityToAssetRatioIFRSSummaryOfBusinessResults",  # IFRS の BPS (要素名に注意)
                "EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults",
            ),
            False,
        ),
        (
            "dps",
            "duration",
            ("DividendPaidPerShareSummaryOfBusinessResults",),
            True,
        ),
        (
            "shares_outstanding",
            "instant",
            (
                "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
            ),
            True,
        ),
    ]

    # 営業利益は要約表に無いため、財務諸表本体から読む
    OPERATING_INCOME_TAGS = (
        "OperatingIncome",
        "OperatingProfitLossIFRS",
        "OperatingIncomeLossUSGAAP",
    )

    # % 表記に換算する比率項目
    RATIO_FIELDS = ("equity_ratio", "roe")

    # 半期報告書から採用する項目 (期末時点の貸借対照表の値のみ)
    INTERIM_FIELDS = ("total_assets", "net_assets", "equity_ratio", "shares_outstanding")

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def parse_zip(self, zip_path: str) -> Dict[str, Any]:
        """ダウンロードした ZIP 内の XBRL 本体 (PublicDoc) を探してパースする"""
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                xbrl_files = [f for f in z.namelist() if f.endswith(".xbrl")]
                # 監査報告書 (AuditDoc) ではなく本文 (PublicDoc) を対象にする
                public = [f for f in xbrl_files if "PublicDoc" in f] or xbrl_files
                if not public:
                    self.logger.debug(f"ℹ️ No XBRL file found in {zip_path}")
                    return {}
                target = max(public, key=lambda f: z.getinfo(f).file_size)
                with z.open(target) as f:
                    return self.parse_content(f.read())
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            self.logger.warning(f"⚠️ Corrupted or unreadable zip file ({zip_path}): {e}")
            return {}
        except Exception as e:
            self.logger.error(f"❌ Unexpected error reading zip ({zip_path}): {e}")
            return {}

    @staticmethod
    def _to_float(text: Optional[str]) -> Optional[float]:
        if text is None or not text.strip():
            return None
        try:
            return float(text.strip())
        except ValueError:
            return None

    def parse_content(self, content: bytes) -> Dict[str, Any]:
        """XBRL インスタンスから財務値を抽出する。

        Returns:
            {"kind": "annual" | "interim", "period_end": "YYYY-MM-DD", "consolidated": bool,
             <項目>: 値, ...}。対象の期間が見つからない書類は {}。
        """
        try:
            tree = et.fromstring(content)
        except Exception as e:
            self.logger.warning(f"⚠️ XML parse failed: {e}")
            return {}

        # (要素名, コンテキスト) -> 値。同じ組の重複は先勝ち (値は同一)
        facts: dict[tuple[str, str], Optional[float]] = {}
        period_ends: dict[str, str] = {}
        for el in tree.iter():
            if not isinstance(el.tag, str):
                continue
            local = et.QName(el).localname
            if local == "context":
                ctx_id = el.get("id", "")
                end = el.find(".//{*}instant")
                if end is None:
                    end = el.find(".//{*}endDate")
                if end is not None and end.text:
                    period_ends[ctx_id] = end.text.strip()
                continue
            ctx = el.get("contextRef")
            if ctx is None:
                continue
            facts.setdefault((local, ctx), self._to_float(el.text))

        contexts_used = {ctx for _, ctx in facts}
        # 当期の期間コンテキストがある種類 (有価証券報告書を優先)。どちらも無い書類は対象外
        kind = next(
            (
                k
                for k, c in _CONTEXTS.items()
                if any(ctx.startswith(c["duration"]) for ctx in contexts_used)
            ),
            None,
        )
        if kind is None:
            return {}
        ctxs = _CONTEXTS[kind]

        # 要約表に連結 (メンバー無し) の値が 1 つでもあれば連結、無ければ単体の会社
        consolidated = any(
            name.endswith("SummaryOfBusinessResults") and ctx in (ctxs["duration"], ctxs["instant"])
            and value is not None
            for (name, ctx), value in facts.items()
        )
        scope = "" if consolidated else NON_CONSOLIDATED

        def lookup(names: tuple[str, ...], base_ctx: str, allow_non_consolidated: bool) -> Optional[float]:
            candidates = [base_ctx + scope]
            if allow_non_consolidated and scope == "":
                candidates.append(base_ctx + NON_CONSOLIDATED)
            for ctx in candidates:
                for name in names:
                    value = facts.get((name, ctx))
                    if value is not None:
                        return value
            return None

        extracted: Dict[str, Any] = {}
        for field, period, names, allow_nc in self.SUMMARY_FIELDS:
            value = lookup(names, ctxs[period], allow_nc)
            if value is not None:
                extracted[field] = value

        operating_income = lookup(self.OPERATING_INCOME_TAGS, ctxs["duration"], False)
        if operating_income is not None:
            extracted["operating_income"] = operating_income

        if kind == KIND_ANNUAL:
            prev = lookup(self.NET_PROFIT_TAGS, ctxs["prior_duration"], False)
            if prev is not None:
                extracted["prev_net_profit"] = prev

        for field in self.RATIO_FIELDS:
            if field in extracted:
                extracted[field] = extracted[field] * 100.0

        if kind == KIND_INTERIM:
            extracted = {k: v for k, v in extracted.items() if k in self.INTERIM_FIELDS}

        if not extracted:
            return {}
        return {
            "kind": kind,
            "period_end": period_ends.get(ctxs["instant"]),
            "consolidated": consolidated,
            **extracted,
        }
