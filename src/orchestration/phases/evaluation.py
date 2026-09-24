"""評価・演算フェーズ (Phase 2)

Acquisition フェーズで取得した全銘柄データをベクトル演算し、
最新日への絞り込み、財務修復、第1層 Pre-Filter、および第2層 QuantEvaluator を実行する。
"""

from typing import Optional, cast

import pandas as pd
import polars as pl

from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator
from src.orchestration.phases.base import BasePhase
from src.services.financial_repair import FinancialRepairService


class EvaluationPhase(BasePhase):
    """評価・演算フェーズ (3層クオンツ評価アーキテクチャ)"""

    def execute(self, data_map: Optional[dict] = None) -> Optional[pl.DataFrame]:
        self.log_info("🚀 Starting 3-Tier Evaluation Phase...")

        # 1. データロード＆テクニカル指標算出（全履歴でローリング計算）
        processed_df = self._prepare_input_data(data_map)
        if processed_df is None or processed_df.is_empty():
            self.log_error("Input data is empty. Evaluation aborted.")
            return None

        # 2. [指摘2] 全履歴時系列データから流動性指標を集計 (直近20日平均売買代金, 直近5日ゼロ日数)
        self.log_info("Aggregating liquidity metrics from time-series data...")
        df_liquidity = PreFilter.aggregate_timeseries_metrics(processed_df)

        # 3. [Bug Fix #5] 最新日（各銘柄の最新 entry_date）の1行だけに絞り込み
        # 過去日付行や銘柄重複を物理的に排除し、以降の処理量を約 1/245 に圧縮
        self.log_info(
            f"Filtering down to latest entry_date per stock (from {len(processed_df)} records)..."
        )
        latest_df = (
            processed_df.sort(["code", "entry_date"])
            .group_by("code")
            .last()
        )
        self.log_info(f"Unique stocks for evaluation: {len(latest_df)}")

        # 4. 銘柄マスタ・財務データの結合 (Early Memory Join)
        self.log_info("Applying Early Memory Join (Master + Fundamentals)...")
        enriched_df = self._enrich_master_data(latest_df)

        # 5. 財務データの精密修復（比率スケーリング・黒字判定）
        self.log_info("Executing Deep Financial Repair...")
        repaired_df = FinancialRepairService.repair(enriched_df)

        # 6. DuckDB への永続化 (最新修復データを保存: entry_dateが存在する有効市場レコードのみ)
        try:
            valid_metrics_df = repaired_df.filter(pl.col("entry_date").is_not_null())
            if not valid_metrics_df.is_empty():
                self.context.duck_repo.save_metrics(valid_metrics_df)
        except Exception as e:
            self.log_error(f"❌ Failed to persist metrics to DuckDB: {e}", exc_info=True)
            raise RuntimeError(f"Database persistence failed in evaluation phase: {e}") from e

        # 7. [第1層] Pre-Filter (地雷株・流動性足切り)
        self.log_info("Applying Layer 1: Pre-Filter with liquidity metrics...")
        cfg = getattr(self.context, "config", None)
        evaluable_df, uncalculable_df = PreFilter.apply_filter(
            repaired_df, df_liquidity=df_liquidity, config=cfg
        )

        self.context.uncalculable_df = uncalculable_df
        self.context.excluded_count = len(uncalculable_df)
        self.context.evaluated_count = len(evaluable_df)

        self.log_info(
            f"Pre-Filter completed: {len(evaluable_df)} passed, {len(uncalculable_df)} excluded."
        )

        if evaluable_df.is_empty():
            self.log_warn("All stocks were excluded by Pre-Filter.")
            return evaluable_df

        # 8. [第2層] QuantEvaluator (多変量連続グラデーション配点＆ゲートキーパー)
        self.log_info("Applying Layer 2: QuantEvaluator scoring...")
        evaluable_df = self._apply_quant_evaluator(evaluable_df)

        # 9. スコア降順ソート
        final_df = evaluable_df.sort("quant_score", descending=True)

        self.log_info(
            f"Evaluation phase finished successfully. Top score: "
            f"{final_df['quant_score'][0] if not final_df.is_empty() else 'N/A'}"
        )
        return final_df

    def _apply_quant_evaluator(self, df: pl.DataFrame) -> pl.DataFrame:
        """各銘柄に対して QuantEvaluator を適用し、スコアと判定を付与する"""
        records = df.to_dicts()
        scores: list[float] = []
        verdicts: list[str] = []

        for row in records:
            # 指摘1: ma25_divergence は ma_divergence / ma25_divergence の双方から取得
            ma_div = row.get("ma_divergence")
            if ma_div is None:
                ma_div = row.get("ma25_divergence")

            # 指摘1 & MACD status改善: Bullish / Bearish / Neutral を正確に導出
            macd_status = row.get("macd_status")
            if not macd_status or str(macd_status).strip() in ["", "None", "nan"]:
                m_hist = row.get("macd_hist")
                if m_hist is not None:
                    if m_hist > 0:
                        macd_status = "Bullish"
                    elif m_hist < 0:
                        macd_status = "Bearish"
                    else:
                        macd_status = "Neutral"
                else:
                    macd_status = "Neutral"

            dossier = {
                "fundamentals": {
                    "per": row.get("per"),
                    "pbr": row.get("pbr"),
                    "roe": row.get("roe"),
                    "equity_ratio": row.get("equity_ratio"),
                    "dividend_yield": row.get("dividend_yield"),
                    "operating_margin": row.get("operating_margin"),
                    "operating_income": row.get("operating_income"),
                    "net_profit": row.get("net_profit"),
                },
                "technicals": {
                    "price": row.get("price"),
                    "rsi_14": row.get("rsi_14"),
                    "ma25_divergence": ma_div,
                    "macd_hist": row.get("macd_hist"),
                    "macd_status": macd_status,
                },
            }
            score, verdict = QuantEvaluator.evaluate(dossier)
            scores.append(score)
            verdicts.append(verdict)

        return df.with_columns(
            [
                pl.Series("quant_score", scores, dtype=pl.Float64),
                pl.Series("verdict", verdicts, dtype=pl.String),
            ]
        )

    def _prepare_input_data(
        self, data_map: Optional[dict] = None
    ) -> Optional[pl.DataFrame]:
        """計算対象のデータを準備する"""
        from src.fetcher.polars_processor import PolarsProcessor

        data_map = data_map or getattr(self.context, "temp_data_map", None)
        if not data_map:
            self.log_warn("No in-memory data. Loading from DB...")
            return cast(
                Optional[pl.DataFrame], self.context.duck_repo.load_metrics(days=365)
            )

        self.log_info(f"Calculating technicals for {len(data_map)} stocks...")
        sanitized_map = {}
        for c, df in data_map.items():
            if isinstance(df, pd.DataFrame):
                if "code" not in df.columns:
                    df = df.copy()
                    df["code"] = c
            elif isinstance(df, pl.DataFrame):
                if "code" not in df.columns:
                    df = df.with_columns(pl.lit(c).alias("code"))
            sanitized_map[c] = df

        return PolarsProcessor.calc_batch_technicals_vectorized(
            sanitized_map, latest_only=False
        )

    def _enrich_master_data(self, df: pl.DataFrame) -> pl.DataFrame:
        """業種・ファンダメンタルズ情報を結合する"""
        from src.repositories.fundamentals_repository import FundamentalsRepository

        # 銘柄マスタの結合 (context.stock_repo 優先、フォールバックとして duck_repo.load_stocks)
        stocks_df = pl.DataFrame()
        if hasattr(self.context, "stock_repo") and self.context.stock_repo:
            try:
                stocks_df = self.context.stock_repo.load_all()
            except Exception:
                stocks_df = self.context.duck_repo.load_stocks()
        if stocks_df.is_empty():
            stocks_df = self.context.duck_repo.load_stocks()

        df = df.with_columns(pl.col("code").cast(pl.Utf8))

        if not stocks_df.is_empty():
            stocks_df = stocks_df.with_columns(pl.col("code").cast(pl.Utf8))
            candidate_cols = ["code", "name", "sector", "market"]
            master_cols = [c for c in candidate_cols if c in stocks_df.columns]
            # df側のマスター列重複をドロップ
            df_metrics = df.drop([c for c in master_cols if c in df.columns and c != "code"])
            # 銘柄マスタの全銘柄を保持するため、stocks_df を主として left join
            # （時系列データのない銘柄も price=None として保持され、PreFilter で『市場データ取得不能』として隔離回収される）
            df = stocks_df.select(master_cols).join(df_metrics, on="code", how="left")
            if "sector" in df.columns:
                df = df.with_columns([pl.col("sector").fill_null("Other")])
            if "name" in df.columns:
                df = df.with_columns([pl.col("name").fill_null("Unknown")])

        # 財務データの結合 (context.funda_repo 優先)
        if hasattr(self.context, "funda_repo") and self.context.funda_repo:
            funda_repo = self.context.funda_repo
        else:
            funda_repo = FundamentalsRepository()

        if hasattr(funda_repo, "load_all"):
            df_fundamentals = funda_repo.load_all()
        elif hasattr(funda_repo, "get_all_pl"):
            df_fundamentals = funda_repo.get_all_pl()
        else:
            df_fundamentals = pl.DataFrame()

        if not df_fundamentals.is_empty():
            df_fundamentals = df_fundamentals.with_columns(pl.col("code").cast(pl.Utf8))
            f_cols = [c for c in df_fundamentals.columns if c != "code"]
            df = self._clean_columns(df, f_cols)
            df = df.join(df_fundamentals, on="code", how="left")

        # 結合後に万一混入した _right サフィックス列をパージ (Schema Isolation Guard)
        right_cols = [c for c in df.columns if c.endswith("_right")]
        if right_cols:
            df = df.drop(right_cols)

        return df

