"""評価・演算フェーズ (Phase 2)

Acquisition フェーズで取得した全銘柄データを一括でベクトル演算し、
スコアリングとランキングを実行する。
"""

from typing import Optional, cast

import polars as pl

from src.orchestration.phases.base import BasePhase


class EvaluationPhase(BasePhase):
    """評価・演算フェーズ (v10: Turbo Fidelity / Polars 修復統合済)"""

    def execute(self, data_map: Optional[dict] = None) -> Optional[pl.DataFrame]:
        self.log_info("🚀 Starting Turbo Evaluation Phase (v10)...")

        # 1. データロード（市場データ）
        processed_df = self._prepare_input_data(data_map)
        if processed_df is None or processed_df.is_empty():
            self.log_error("Input data is empty. Evaluation aborted.")
            return None

        # 2. Early Memory Join (L3 Data Loss Prevention)
        self.log_info("Applying Early Memory Join (Master + Fundamentals)...")
        processed_df = self._enrich_master_data(processed_df)

        # [v10 Turbo] Financial Repair: 聖典ロジックによる欠損救済と黒字判定
        from src.services.financial_repair import FinancialRepairService

        self.log_info(
            "Executing Deep Financial Repair (Deep Repair #3, Turnaround Detection)..."
        )
        processed_df = FinancialRepairService.apply_deep_repair(processed_df)

        # 3. DuckDB への永続化 (修復済みの全データを保存)
        self.log_info(
            f"Upserting {len(processed_df)} records to DuckDB for persistence..."
        )
        self.context.duck_repo.save_metrics(processed_df)

        # 4. マルチ戦略計算
        self.log_info("Running independent strategy evaluations...")
        strategy_results = self._run_multi_strategy_scoring(processed_df)
        if not strategy_results:
            self.log_error("All strategy evaluations failed.")
            return None

        # 5. 縦方向高速集約（Winner-Takes-All）
        self.log_info("Aggregating winners using window functions...")
        final_scores = self._aggregate_winners(strategy_results)

        # 結合キーの正規化（Date型強制）
        final_scores = final_scores.with_columns([pl.col("entry_date").cast(pl.Date)])

        # [v13] Join-less Protocol: エンジンが属性を維持して返すため、再結合を廃止
        # 重複カラムによる衝突リスクを物理的に排除する。
        final_df = final_scores.sort("quant_score", descending=True)

        # TOP 50 選出
        final_df = final_df.sort("quant_score", descending=True).head(50)

        # [Agentic Pipeline] StockDossier の生成とコンテキストへの蓄積
        from src.orchestration.dossier_builder import StockDossierBuilder

        dossiers = StockDossierBuilder.from_dataframe(final_df, limit=50)
        self.context.stock_dossiers = dossiers

        self.log_info(
            f"Evaluation completed. {len(final_df)} candidates identified, {len(dossiers)} dossiers created."
        )

        # [v12] Final Integrity Guard: 外部へ渡す前にサフィックスの混入がないか最終検閲を行う
        self._verify_integrity(final_df)

        return final_df

    def _prepare_input_data(
        self, data_map: Optional[dict] = None
    ) -> Optional[pl.DataFrame]:
        """計算対象のデータを準備する"""
        from src.fetcher.polars_processor import PolarsProcessor

        data_map = data_map or getattr(self.context, "temp_data_map", None)
        if not data_map:
            self.log_warn("No in-memory data. Loading from DB...")
            # 最新の財務を反映させた状態で DB からロード
            return cast(
                Optional[pl.DataFrame], self.context.duck_repo.load_metrics(days=365)
            )

        self.log_info(f"Calculating technicals for {len(data_map)} stocks...")
        return PolarsProcessor.calc_batch_technicals_vectorized(
            data_map, latest_only=False
        )

    def _run_multi_strategy_scoring(self, df: pl.DataFrame) -> list[pl.DataFrame]:
        """各戦略を独立して実行し、物理的に確定（collect）させる"""
        from src.calc.engine import ScoringEngine

        scoring_engine = ScoringEngine(self.context.config)
        strategies = list(self.context.config.get("strategies", {}).keys()) or [
            "Balanced Strategy"
        ]

        results = []
        for i, strat_name in enumerate(strategies):
            self.log_info(f"[{i + 1}/{len(strategies)}] Evaluating: {strat_name}")
            try:
                strat_res = scoring_engine.calculate_score(df, strategy_name=strat_name)
                # スキーマを正規化
                # [v13] 属性維持: エンジンが返したカラムをそのまま利用
                results.append(strat_res)
            except Exception as e:
                self.log_error(f"Strategy {strat_name} failed: {e}")
        return results

    def _aggregate_winners(self, results: list[pl.DataFrame]) -> pl.DataFrame:
        """縦積み concat と窓関数による最高スコア戦略の選別"""
        all_results = pl.concat(results, how="diagonal_relaxed").lazy()
        final_scores = (
            all_results.with_columns(
                pl.col("quant_score")
                .rank("dense", descending=True)
                .over(["code", "entry_date"])
                .alias("_winner_rank")
            )
            .filter(pl.col("_winner_rank") == 1)
            .group_by(["code", "entry_date"])
            .first()
            .drop("_winner_rank")
            .collect()
        )
        return final_scores

    def _enrich_master_data(self, df: pl.DataFrame) -> pl.DataFrame:
        """業種・ファンダメンタルズ情報を結合する"""
        from src.repositories.fundamentals_repository import FundamentalsRepository

        funda_repo = FundamentalsRepository()

        # 銘柄マスタの結合
        stocks_df = self.context.duck_repo.load_stocks()
        if not stocks_df.is_empty():
            # 既存の sector, name, market 等があれば衝突を避ける
            candidate_cols = ["code", "name", "sector", "market"]
            master_cols = [c for c in candidate_cols if c in stocks_df.columns]
            df = df.drop([c for c in master_cols if c in df.columns and c != "code"])
            df = df.join(stocks_df.select(master_cols), on="code", how="left")
            if "sector" in df.columns:
                df = df.with_columns([pl.col("sector").fill_null("Other")])
            if "name" in df.columns:
                df = df.with_columns([pl.col("name").fill_null("Unknown")])

        # 財務データの結合 (XBRL SSOT 優先)
        df_fundamentals = funda_repo.get_all_pl()
        if not df_fundamentals.is_empty():
            # [v12] Prevent Shadowing: Join 前に重複可能性のある財務カラムを物理削除し衝突を防ぐ
            f_cols = [c for c in df_fundamentals.columns if c != "code"]
            df = self._clean_columns(df, f_cols)
            df = df.join(df_fundamentals, on="code", how="left")

        return df
