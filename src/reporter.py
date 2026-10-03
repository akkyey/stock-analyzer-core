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
        def pick(*values: Any) -> float | None:
            # 「a or b or …」と同じく、最初の真値 (どれも偽なら最後の値) を数値にする
            return safe_float_or_none(next((v for v in values[:-1] if v), values[-1]))

        close = pick(
            common.get("close"), latest_row.get("close"), common.get("price"), latest_row.get("price")
        )
        eps = pick(common.get("eps"), latest_row.get("eps"))
        bps = pick(common.get("bps"), latest_row.get("bps"))
        dps = pick(common.get("dps"), common.get("dividend_per_share"), latest_row.get("dps"))
        shares = pick(
            common.get("shares_outstanding"), common.get("shares"), latest_row.get("shares_outstanding")
        )

        def per_share_ratio(denominator: float | None) -> tuple[float | None, float | None]:
            # (保存値の照合用の再計算値, 保存値が無い場合の算出値)。照合用は株価 0 を除く
            if denominator is None or denominator <= 0:
                return None, None
            loose = close / denominator if close is not None else None
            return (loose if close else None), loose

        def resolve(stored: float | None, accept: bool, derived: tuple, fmt: Any, tol: tuple) -> tuple:
            strict, loose = derived
            if stored is not None and accept:
                return fmt(stored), self._stored_value_source(stored, strict, *tol)
            if loose is not None:
                return fmt(loose), "calc"
            return "-", "-"

        def round2(v: float) -> float:
            return round(v, 2)

        per_val = pick(common.get("per"), latest_row.get("per"))
        pbr_val = pick(common.get("pbr"), latest_row.get("pbr"))
        div_val = pick(common.get("dividend_yield"), latest_row.get("dividend_yield"))
        mc_val = pick(common.get("market_cap"), latest_row.get("market_cap"))

        # 配当利回り (%) と時価総額の再計算値 (株価が正の場合のみ)
        div_calc = dps / close * 100.0 if close and dps is not None and close > 0 else None
        mc_strict = close * shares if close and shares and shares > 0 else None
        mc_loose = (
            close * shares if close is not None and shares is not None and shares > 0 else None
        )

        # 1〜4. 保存値があり妥当ならそれ (出所は再計算との一致で判定)、無ければ再計算、どちらも無ければ "-"
        res: dict[str, tuple[Any, str]] = {
            "per": resolve(
                per_val, per_val is not None and per_val > 0, per_share_ratio(eps), round2, (0.01, 0.01)
            ),
            "pbr": resolve(
                pbr_val, pbr_val is not None and pbr_val > 0, per_share_ratio(bps), round2, (0.01, 0.01)
            ),
            # 保存値は既にパーセント表記 (例: 0.77 = 0.77%)。0 (無配) も有効な値
            "div_yield": resolve(div_val, True, (div_calc, div_calc), round2, (0.01, 0.01)),
            "market_cap": resolve(
                mc_val, mc_val is not None and mc_val > 0, (mc_strict, mc_loose), int, (0.001, 1.0)
            ),
        }

        # 5. ROE (%) (保存値 → EDINET → calc)。保存値は既にパーセント表記
        res["roe"] = self._resolve_roe(
            pick(common.get("roe"), latest_row.get("roe")),
            safe_float_or_none(common.get("edinet_roe")),
            eps,
            bps,
        )
        return res

    @staticmethod
    def _resolve_roe(
        roe_val: float | None, edinet_roe: float | None, eps: float | None, bps: float | None
    ) -> tuple[Any, str]:
        if roe_val is not None:
            return round(roe_val, 2), "stored"
        if edinet_roe is not None:
            roe_pct = edinet_roe * 100.0 if abs(edinet_roe) < 1.0 else edinet_roe
            return round(roe_pct, 2), "edinet"
        if eps is not None and bps is not None and bps > 0:
            return round((eps / bps) * 100.0, 2), "calc"
        return "-", "-"

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
