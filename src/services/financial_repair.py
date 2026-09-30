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
        # 1-1. 総資産と純資産による直接計算 (最優先)
        if "total_assets" in cols and "net_assets" in cols and "equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        pl.col("equity_ratio").is_null()
                        & pl.col("total_assets").is_finite()
                        & (pl.col("total_assets") > 0)
                        & pl.col("net_assets").is_finite()
                    )
                    .then((pl.col("net_assets") / pl.col("total_assets")) * 100.0)
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )

        # 1-2. D/Eレシオからの恒等式逆算補完
        # ※注: ここでのDEレシオは「有利子負債比率」ではなくEDINET原本の「総負債 ÷ 純資産」テーブル値を前提
        # 恒等式: 自己資本比率 = 純資産 / 総資産 = 1 / (1 + 総負債/純資産) = 100 / (1 + DE/100)
        if "equity_ratio" in cols and "debt_equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        pl.col("equity_ratio").is_null()
                        & pl.col("debt_equity_ratio").is_finite()
                        & (pl.col("debt_equity_ratio") > 0)
                    )
                    .then(100.0 / (1.0 + (pl.col("debt_equity_ratio") / 100.0)))
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )

        # [Step 2] 動的 PER / PBR / 利回り / 時価総額 の算出 (当日株価 × 財務確定Fact)
        # 当日株価 (price) が存在する場合、シードやDBの確定Factからリアルタイムに導出する
        if "price" in cols:
            # 2-1. EPS の補完 (net_profit / shares_outstanding)
            if all(c in cols for c in ["net_profit", "shares_outstanding"]):
                if "eps" not in cols:
                    df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("eps"))
                df = df.with_columns(
                    pl.when(
                        pl.col("eps").is_null()
                        & pl.col("net_profit").is_finite()
                        & pl.col("shares_outstanding").is_finite()
                        & (pl.col("shares_outstanding") > 0)
                    )
                    .then(pl.col("net_profit") / pl.col("shares_outstanding"))
                    .otherwise(pl.col("eps"))
                    .alias("eps")
                )

            # 2-2. BPS の補完 (net_assets / shares_outstanding)
            if all(c in cols for c in ["net_assets", "shares_outstanding"]):
                if "bps" not in cols:
                    df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("bps"))
                df = df.with_columns(
                    pl.when(
                        pl.col("bps").is_null()
                        & pl.col("net_assets").is_finite()
                        & pl.col("shares_outstanding").is_finite()
                        & (pl.col("shares_outstanding") > 0)
                    )
                    .then(pl.col("net_assets") / pl.col("shares_outstanding"))
                    .otherwise(pl.col("bps"))
                    .alias("bps")
                )

            # 2-3. PER の動的算出: 当日株価 / EPS (EPS > 0 の黒字企業)
            if "eps" in df.columns:
                if "per" not in cols:
                    df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("per"))
                df = df.with_columns(
                    pl.when(
                        pl.col("eps").is_finite()
                        & (pl.col("eps") > 0)
                        & pl.col("price").is_finite()
                        & (pl.col("price") > 0)
                    )
                    .then(pl.col("price") / pl.col("eps"))
                    .otherwise(pl.col("per"))
                    .alias("per")
                )

            # 2-4. PBR の動的算出: 当日株価 / BPS (BPS > 0)
            if "bps" in df.columns:
                if "pbr" not in cols:
                    df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("pbr"))
                df = df.with_columns(
                    pl.when(
                        pl.col("bps").is_finite()
                        & (pl.col("bps") > 0)
                        & pl.col("price").is_finite()
                        & (pl.col("price") > 0)
                    )
                    .then(pl.col("price") / pl.col("bps"))
                    .otherwise(pl.col("pbr"))
                    .alias("pbr")
                )

            # 2-5. 配当利回り (dividend_yield) の動的算出: (DPS / 当日株価) * 100
            if "dps" in df.columns:
                if "dividend_yield" not in cols:
                    df = df.with_columns(
                        pl.lit(None, dtype=pl.Float64).alias("dividend_yield")
                    )
                df = df.with_columns(
                    pl.when(
                        pl.col("dps").is_finite()
                        & (pl.col("dps") >= 0)
                        & pl.col("price").is_finite()
                        & (pl.col("price") > 0)
                    )
                    .then((pl.col("dps") / pl.col("price")) * 100.0)
                    .otherwise(pl.col("dividend_yield"))
                    .alias("dividend_yield")
                )

            # 2-6. 時価総額 (market_cap) の動的算出: 当日株価 × 発行済株式数
            if "shares_outstanding" in df.columns:
                if "market_cap" not in cols:
                    df = df.with_columns(
                        pl.lit(None, dtype=pl.Float64).alias("market_cap")
                    )
                df = df.with_columns(
                    pl.when(
                        pl.col("shares_outstanding").is_finite()
                        & (pl.col("shares_outstanding") > 0)
                        & pl.col("price").is_finite()
                        & (pl.col("price") > 0)
                    )
                    .then(pl.col("price") * pl.col("shares_outstanding"))
                    .otherwise(pl.col("market_cap"))
                    .alias("market_cap")
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

        # [Step 5] 比率項目の自動スケーリング (#4, 指摘4)
        # 自己資本比率が実数（0.0〜1.0 未満）で表記されている場合のみ百分率（%）へ100倍スケーリングする
        # ※ 1.0 は「1%」として扱うのが安全なため < 1.0 で判定する。
        # ※ current_ratio, debt_equity_ratio などの倍率指標は 1.0 を超えるのが通常なため対象外とする。
        from src.config_singleton import ConfigSingleton

        threshold = ConfigSingleton.get("financial_repair.ratio_scaling_threshold", 1.0)

        if "equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        (pl.col("equity_ratio").abs() > 0)
                        & (pl.col("equity_ratio").abs() < threshold)
                    )
                    .then(pl.col("equity_ratio") * 100.0)
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )

        # [Step 6] 極端な境界値のクリッピング（スケーリング後に適用）
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

        logger.info(
            f"✨ Deep financial repair completed. (Records: {initial_count}, Scaling Threshold: {threshold})"
        )
        return df

    # エイリアス定義
    apply_deep_repair = repair
