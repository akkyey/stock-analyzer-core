from logging import getLogger

import polars as pl

logger = getLogger(__name__)


class FinancialRepairService:
    """財務データの修復とクレンジングを行うサービス (v10: Polars Native Edition)。

    yfinance の .financials 通信に頼らず、EDINET から取得した確定値（Fact）を基に
    PER の精密修復、黒字転換判定、自己資本比率の逆算をベクトル演算で高速に実行する。
    """

    @staticmethod
    def repair(df: pl.DataFrame) -> pl.DataFrame:
        """財務項目の欠損補完、異常値の修復、および導出項目の生成を統合実行する。"""
        if df.is_empty():
            return df

        initial_count = len(df)
        cols = df.columns

        # [Step 1] 自己資本比率 (Equity Ratio) の精緻化
        if "equity_ratio" in cols and "debt_equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        pl.col("equity_ratio").is_null()
                        & pl.col("debt_equity_ratio").is_not_null()
                        & (pl.col("debt_equity_ratio") > 0)
                    )
                    .then(100.0 / (1.0 + (pl.col("debt_equity_ratio") / 100.0)))
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )

        # [Step 2] 精密 PER (Deep Repair #3) の推計
        if all(c in cols for c in ["per", "price", "net_profit", "shares_outstanding"]):
            df = df.with_columns(
                [
                    pl.when(
                        (pl.col("per").is_null() | (pl.col("per") <= 0))
                        & pl.col("net_profit").is_not_null()
                        & (pl.col("net_profit") > 0)
                        & pl.col("shares_outstanding").is_not_null()
                        & (pl.col("shares_outstanding") > 0)
                    )
                    .then(
                        pl.col("price")
                        / (pl.col("net_profit") / pl.col("shares_outstanding"))
                    )
                    .otherwise(pl.col("per"))
                    .alias("per")
                ]
            )

        # [Step 3] 黒字転換 / 利益ステータス判定 (#5)
        if all(c in cols for c in ["net_profit", "prev_net_profit"]):
            df = df.with_columns(
                [
                    pl.when(
                        (pl.col("prev_net_profit") < 0) & (pl.col("net_profit") > 0)
                    )
                    .then(pl.lit("turnaround_black"))
                    .when((pl.col("prev_net_profit") > 0) & (pl.col("net_profit") < 0))
                    .then(pl.lit("turnaround_white"))
                    .when(
                        (pl.col("prev_net_profit") < 0)
                        & (pl.col("net_profit") < 0)
                        & (pl.col("net_profit") > pl.col("prev_net_profit"))
                    )
                    .then(pl.lit("loss_shrinking"))
                    .otherwise(pl.lit("normal"))
                    .alias("turnaround_status")
                ]
            )
            df = df.with_columns(
                [
                    pl.when(pl.col("turnaround_status") == "turnaround_black")
                    .then(1)
                    .otherwise(0)
                    .alias("is_turnaround")
                ]
            )

        # [Step 4] 利益成長率の生値算出
        if all(c in cols for c in ["net_profit", "prev_net_profit"]):
            df = df.with_columns(
                [
                    pl.when(pl.col("prev_net_profit").abs() > 0)
                    .then(
                        (pl.col("net_profit") - pl.col("prev_net_profit"))
                        / pl.col("prev_net_profit").abs()
                        * 100.0
                    )
                    .otherwise(None)
                    .alias("profit_growth_raw")
                ]
            )

        # [Step 5] 極端な境界値のクリッピング
        if "per" in cols:
            df = df.with_columns(
                [
                    pl.when(pl.col("per") > 1000.0)
                    .then(1000.0)
                    .otherwise(pl.col("per"))
                    .alias("per")
                ]
            )
        if "equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.col("equity_ratio")
                    .clip(lower_bound=0.0, upper_bound=100.0)
                    .alias("equity_ratio")
                ]
            )

        # [Step 6] 比率項目の自動スケーリング (#4)
        # [v27.3] 閾値（デフォルト10.0）未満（実数表記）の場合、百分率へ 100倍スケーリングする
        from src.config_singleton import ConfigSingleton

        threshold = ConfigSingleton.get(
            "financial_repair.ratio_scaling_threshold", 10.0
        )

        ratio_cols = [
            c
            for c in [
                "current_ratio",
                "quick_ratio",
                "equity_ratio",
                "debt_equity_ratio",
            ]
            if c in cols
        ]
        if ratio_cols:
            df = df.with_columns(
                [
                    pl.when(pl.col(c).abs() < threshold)
                    .then(pl.col(c) * 100.0)
                    .otherwise(pl.col(c))
                    .alias(c)
                    for c in ratio_cols
                ]
            )

        logger.info(
            f"✨ Deep financial repair completed. (Records: {initial_count}, Scaling Threshold: {threshold})"
        )
        return df

    # エイリアス定義
    apply_deep_repair = repair
