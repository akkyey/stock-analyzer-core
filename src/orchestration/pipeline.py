"""オーケストレーションパイプライン (v7.0.0)

Acquisition -> Evaluation -> Integration の三段階を統括する。
"""

import time
from typing import List, Optional

import polars as pl

from src.orchestration.context import OrchestratorContext
from src.orchestration.phases.acquisition import AcquisitionPhase
from src.orchestration.phases.base import BasePhase
from src.orchestration.phases.evaluation import EvaluationPhase
from src.orchestration.phases.integration import IntegrationPhase


class OrchestrationPipeline:
    """三段階パイプラインの実行エンジン"""

    def __init__(self, context: OrchestratorContext):
        self.context = context
        self.logger = context.logger
        self.phases: List[BasePhase] = [
            AcquisitionPhase(context),
            EvaluationPhase(context),
            IntegrationPhase(context),
        ]

    def run(self) -> Optional[pl.DataFrame]:
        """パイプラインを順次実行する。"""
        self.logger.info("🚀 Starting Tri-Phase Orchestration Pipeline...")
        start_time = time.time()

        df: Optional[pl.DataFrame] = None

        try:
            for phase in self.phases:
                phase_name = phase.__class__.__name__
                self.logger.info(f"--- [Phase: {phase_name}] ---")

                t_p_start = time.time()
                df = phase.execute(df)
                elapsed = time.time() - t_p_start

                self.logger.info(f"✅ {phase_name} completed in {elapsed:.2f}s")

            total_elapsed = time.time() - start_time
            self.logger.info(
                f"🎉 Pipeline completed successfully in {total_elapsed:.2f}s"
            )
            return df

        except Exception as e:
            self.logger.error(
                f"💥 Pipeline failed at {phase.__class__.__name__}: {e}", exc_info=True
            )
            raise e
