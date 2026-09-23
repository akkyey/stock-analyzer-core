"""財務データフェッチャー (v30: yfinance & EDINET ハイブリッド両立版)

株価・テクニカル・即時指標は yfinance を利用し、
公式な決算財務諸表（売上、利益、自己資本比率、CF等）は EDINET を利用する。
両データソースを排除せず、相互補完するハイブリッドアーキテクチャ。
"""

import logging
from typing import Any

import yfinance as yf

from .base import FetcherBase


class FundamentalsFetcher(FetcherBase):
    """yfinance と EDINET の両立・ハイブリッド財務データ取得を担当するクラス。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger(__name__)

    def fetch_fundamentals_hybrid(
        self, code: str, edinet_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """yfinance の即時指標と EDINET の公式財務諸表を統合・両立して返す。"""
        symbol = f"{code}.T" if "." not in code else code
        res: dict[str, Any] = {
            "code": code,
            "fetch_status": self.STATUS_SUCCESS,
            "source": "hybrid_yf_edinet",
        }

        # 1. yfinance からの指標取得 (PER, PBR, 配当利回り, 時価総額)
        try:
            t = yf.Ticker(symbol)
            fi = getattr(t, "fast_info", None)
            if fi and getattr(fi, "market_cap", None):
                res["market_cap_yf"] = int(fi.market_cap)

            info = getattr(t, "info", None) or {}
            res["per_yf"] = info.get("trailingPE") or info.get("forwardPE")
            res["pbr_yf"] = info.get("priceToBook")
            res["dividend_yield_yf"] = info.get("dividendYield")
            res["roe_yf"] = info.get("returnOnEquity")
            if "market_cap_yf" not in res:
                res["market_cap_yf"] = info.get("marketCap")
        except Exception as e:
            self.logger.debug(f"yfinance fetch skipped for {code}: {e}")

        # 2. EDINET からの公式財務データ統合
        if edinet_data:
            res["edinet_sales"] = edinet_data.get("sales")
            res["edinet_operating_margin"] = edinet_data.get("operating_margin")
            res["edinet_operating_cf"] = edinet_data.get("operating_cf")
            res["edinet_equity_ratio"] = edinet_data.get("equity_ratio")
            res["edinet_roe"] = edinet_data.get("roe")

        return res
