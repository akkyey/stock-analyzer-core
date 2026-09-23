"""統合パイプライン・オーケストレーター

三段階のフェーズ (Acquisition, Evaluation, Integration) を連結し、
システム全体のフローを制御する。
"""

from typing import Optional

import polars as pl

from src.orchestration.context import OrchestratorContext
from src.orchestration.phases.acquisition import AcquisitionPhase
from src.orchestration.phases.evaluation import EvaluationPhase
from src.orchestration.phases.integration import IntegrationPhase


class PipelineOrchestrator:
    """TRI-PHASE パイプライン・オーケストレーター"""

    def __init__(self, context: Optional[OrchestratorContext] = None):
        self.context = context or OrchestratorContext()
        self.log_info = self.context.logger.info
        self.log_error = self.context.logger.error

    def run(self):
        """パイプラインの全フェーズを順次実行する。"""
        self.log_info("★★★ Modernized Tri-Phase Pipeline Started ★★★")

        try:
            # 1. Acquisition Phase (データ取得・蓄積)
            acquisition = AcquisitionPhase(self.context)
            fetched_data = acquisition.execute()

            # 2. Evaluation Phase (評価・演算)
            evaluation = EvaluationPhase(self.context)
            # [v8.0.0] プッシュ型データ転送：取得データを直接渡す
            final_ranks = evaluation.execute(data_map=fetched_data)

            # 3. Integration Phase (統合・報告)
            if final_ranks is not None:
                integration = IntegrationPhase(self.context)
                integration.execute(final_ranks)
            else:
                self.log_info(
                    "Pipeline finished without findings (no stocks matched criteria)."
                )

            self.log_info("★★★ Pipeline Completed Successfully ★★★")

        except Exception as e:
            self.log_error(f"Pipeline failed with critical error: {e}")
            import traceback

            self.log_error(traceback.format_exc())

    def _run_integration(self, df: pl.DataFrame):
        """統合・報告フェーズ (Phase 3)"""
        self.log_info("Starting Integration/Reporting Phase...")
        # 通知ロジックやレポート生成をここに配置
        pass
