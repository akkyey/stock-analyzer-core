import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils import (
    get_current_time,
    rotate_file_backup,
    safe_float_or_none,
    save_dataframe_to_csv,
)


class StockReporter:
    """株式定量分析レポート（CSV形式）のフォーマットと生成を担当するクラス。
    AI評価はエージェント側で実施するため、本クラスは純粋な定量指標・スコアの集計に特化。

    Attributes:
        output_dir (Path): レポートの出力先ディレクトリ。
    """

    def __init__(self, output_dir: str = "data/output"):
        """StockReporter を初期化する。

        Args:
            output_dir (str, optional): 出力先パス。デフォルトは "data/output"。
        """
        self.logger = logging.getLogger(__name__)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("ℹ️ StockReporter Initialized (Pure Quantitative Engine)")

    def generate_reports(
        self,
        results: list[dict[str, Any]],
        source_map: dict[str, str] | None = None,
        output_context: str = "daily",
        limit: int | None = None,
    ) -> dict[str, Path]:
        """定量分析結果をまとめ、単一の決定版CSVファイル (daily_report.csv) として保存する。

        Args:
            results (List[Dict[str, Any]]): 分析結果リスト。
            source_map (Optional[Dict[str, str]], optional): 戦略マッピング。
            output_context (str, optional): daily, weekly, monthly 等のプレフィックス。
            limit (Optional[int], optional): 出力件数制限（Noneで全件）。

        Returns:
            dict[str, Path]: 生成されたレポートのファイルパス {"summary": path}。
        """
        if not results:
            self.logger.warning("No results to report. Generating empty template.")

        rows = []
        for info in results:
            row = self._format_single_item(info, source_map)
            rows.append(row)

        # スコア降順にソート
        rows.sort(key=lambda x: x["Score"], reverse=True)

        # 順位 (Rank) の付与
        for i, r in enumerate(rows, start=1):
            r["Rank"] = i

        # 件数制限
        output_rows = rows[:limit] if limit is not None else rows

        # 固定ファイル名決定
        env = os.getenv("STOCK_ENV", "production").lower()
        env_suffix = "_test" if env == "test" else ""

        file_base = f"daily_report{env_suffix}.csv"
        if "weekly" in output_context:
            file_base = f"weekly_report{env_suffix}.csv"
        if "monthly" in output_context:
            file_base = f"monthly_report{env_suffix}.csv"

        report_path = self.output_dir / file_base

        # 既存ファイルのバックアップローテーション
        rotate_file_backup(str(report_path))

        # CSV 保存
        self.logger.debug(f"Saving Report to {report_path}")
        save_dataframe_to_csv(pd.DataFrame(output_rows), str(report_path))
        self.logger.info(
            f"✅ Quant report generated: {report_path} ({len(output_rows):,} rows)"
        )

        return {"summary": report_path}

    @staticmethod
    def _stored_value_source(
        value: float, derived: float | None, rel_tol: float, abs_tol: float
    ) -> str:
        """保存済みの値が、株価と財務データから再計算した値と一致すれば "calc"、そうでなければ "stored"。

        算出値 (FinancialRepair が price と EPS 等から作る) と、過去に取得して保存された値は
        同じ列に入るため、再計算との一致でのみ「算出値」と判別する (取得元までは断定しない)。
        """
        if derived is not None and abs(value - derived) <= abs(derived) * rel_tol + abs_tol:
            return "calc"
        return "stored"

    def _resolve_metrics_with_fallback(
        self, latest_row: dict[str, Any], common: dict[str, Any]
    ) -> dict[str, tuple[Any, str]]:
        """yfinanceとEDINETを両立させたデータソース統合と算出を行う。
        出所 (`*_Src`) の定義:
        - "calc": 株価と財務データ (EPS・BPS・DPS・発行済株式数) から再計算でき、値が一致した
        - "stored": DB に保存済みの値 (同梱ベースラインや過去の取得結果。取得元・取得時点は DB 次第で、
          本システムが再計算して確認できたものではない)
        - "-": 元データが無く算出もできない (数値も "-")
        ※ 以前は保存値を一律 "yf" (yfinance 直取得) と表示していたが、実データでは EPS 等が空で
          再計算できない保存値が大半であり、取得元を証明できないため "stored" に改めた。

        Returns:
            dict[str, tuple[Any, str]]: 指標名 -> (値, "calc" | "stored" | "-")
        """
        close = safe_float_or_none(
            common.get("close")
            or latest_row.get("close")
            or common.get("price")
            or latest_row.get("price")
        )
        eps = safe_float_or_none(common.get("eps") or latest_row.get("eps"))
        bps = safe_float_or_none(common.get("bps") or latest_row.get("bps"))
        dps = safe_float_or_none(
            common.get("dps")
            or common.get("dividend_per_share")
            or latest_row.get("dps")
        )
        shares = safe_float_or_none(
            common.get("shares_outstanding")
            or common.get("shares")
            or latest_row.get("shares_outstanding")
        )

        res: dict[str, tuple[Any, str]] = {}

        # 1. PER (yfinance -> calc)。データが無い場合は仮の値を出さず "-" とする
        per_val = safe_float_or_none(common.get("per") or latest_row.get("per"))
        if per_val is not None and per_val > 0:
            derived_per = close / eps if close and eps and eps > 0 else None
            res["per"] = (
                round(per_val, 2),
                self._stored_value_source(per_val, derived_per, 0.01, 0.01),
            )
        elif close is not None and eps is not None and eps > 0:
            res["per"] = (round(close / eps, 2), "calc")
        else:
            res["per"] = ("-", "-")

        # 2. PBR (yfinance -> calc)。データが無い場合は "-"
        pbr_val = safe_float_or_none(common.get("pbr") or latest_row.get("pbr"))
        if pbr_val is not None and pbr_val > 0:
            derived_pbr = close / bps if close and bps and bps > 0 else None
            res["pbr"] = (
                round(pbr_val, 2),
                self._stored_value_source(pbr_val, derived_pbr, 0.01, 0.01),
            )
        elif close is not None and bps is not None and bps > 0:
            res["pbr"] = (round(close / bps, 2), "calc")
        else:
            res["pbr"] = ("-", "-")

        # 3. Div_Yield (%) (yfinance -> calc)。保存値は既にパーセント表記 (例: 0.77 = 0.77%)
        div_val = safe_float_or_none(
            common.get("dividend_yield") or latest_row.get("dividend_yield")
        )
        if div_val is not None:
            derived_div = (
                dps / close * 100.0 if close and dps is not None and close > 0 else None
            )
            res["div_yield"] = (
                round(div_val, 2),
                self._stored_value_source(div_val, derived_div, 0.01, 0.01),
            )
        elif close is not None and dps is not None and close > 0:
            res["div_yield"] = (round((dps / close) * 100.0, 2), "calc")
        else:
            res["div_yield"] = ("-", "-")

        # 4. Market_Cap (yfinance -> calc)
        mc_val = safe_float_or_none(
            common.get("market_cap") or latest_row.get("market_cap")
        )
        if mc_val is not None and mc_val > 0:
            derived_mc = close * shares if close and shares and shares > 0 else None
            res["market_cap"] = (
                int(mc_val),
                self._stored_value_source(mc_val, derived_mc, 0.001, 1.0),
            )
        elif close is not None and shares is not None and shares > 0:
            res["market_cap"] = (int(close * shares), "calc")
        else:
            res["market_cap"] = ("-", "-")

        # 5. ROE (%) (yfinance -> edinet -> calc)。保存値は既にパーセント表記
        roe_val = safe_float_or_none(common.get("roe") or latest_row.get("roe"))
        edinet_roe = safe_float_or_none(common.get("edinet_roe"))
        if roe_val is not None:
            res["roe"] = (round(roe_val, 2), "stored")
        elif edinet_roe is not None:
            roe_pct = edinet_roe * 100.0 if abs(edinet_roe) < 1.0 else edinet_roe
            res["roe"] = (round(roe_pct, 2), "edinet")
        elif eps is not None and bps is not None and bps > 0:
            res["roe"] = (round((eps / bps) * 100.0, 2), "calc")
        else:
            res["roe"] = ("-", "-")

        return res

    def _format_single_item(
        self, code_info: dict[str, Any], source_map: dict[str, str] | None
    ) -> dict[str, Any]:
        """単一銘柄のデータを定量レポート行形式に整形する。"""
        from src.utils import safe_display_value as _s

        latest_row = code_info["latest"]
        common = latest_row.get("master_data") or latest_row
        code = latest_row.get("code") or common.get("code")
        verdict = latest_row.get("verdict") or common.get("verdict") or "-"

        score_val = latest_row.get("quant_score")
        sort_score = round(score_val, 2) if (score_val is not None) else 0.0

        now_str = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
        metrics = self._resolve_metrics_with_fallback(latest_row, common)

        return {
            "Rank": 0,
            "Code": code,
            "Name": common.get("name", ""),
            "Sector": _s(common.get("sector_17", common.get("sector"))),
            "Market": _s(common.get("market")),
            "Market_Cap_Src": metrics["market_cap"][1],
            "Market_Cap": metrics["market_cap"][0],
            "Verdict": verdict,
            "Score": sort_score,
            "Price": _s(latest_row.get("price")),
            "Price_Date": _s(
                str(latest_row["entry_date"])[:10]
                if latest_row.get("entry_date") is not None
                else None
            ),
            "PER_Src": metrics["per"][1],
            "PER": metrics["per"][0],
            "PBR_Src": metrics["pbr"][1],
            "PBR": metrics["pbr"][0],
            "Div_Yield_Src": metrics["div_yield"][1],
            "Div_Yield": metrics["div_yield"][0],
            "ROE_Src": metrics["roe"][1],
            "ROE": metrics["roe"][0],
            "Sales_Growth": _s(common.get("sales_growth")),
            "Profit_Growth": _s(common.get("profit_growth")),
            "Operating_Margin": _s(common.get("operating_margin")),
            "Equity_Ratio": _s(common.get("equity_ratio")),
            "RSI": self._format_rsi(common),
            "Trend": (
                latest_row.get("score_trend")
                if latest_row.get("score_trend") is not None
                else (
                    common.get("trend_score")
                    if common.get("trend_score") is not None
                    else "-"
                )
            ),
            "Report_Timestamp": now_str,
        }

    def _format_rsi(self, common_data: dict[str, Any]) -> str:
        """RSI整形"""
        val = common_data.get("rsi_14")
        if val is not None:
            return f"{val:.1f}"
        return "-"
