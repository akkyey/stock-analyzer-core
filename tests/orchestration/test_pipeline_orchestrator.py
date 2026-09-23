from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.pipeline_orchestrator import PipelineOrchestrator
from tests.helpers.stubs import StubOrchestratorContext


@pytest.fixture
def stub_context():
    return StubOrchestratorContext()


def test_pipeline_orchestrator_run_full_success(stub_context):
    """正常系: 全フェーズが連結されて実行されることを確認"""
    with (
        patch("src.orchestration.pipeline_orchestrator.AcquisitionPhase") as MockAcq,
        patch("src.orchestration.pipeline_orchestrator.EvaluationPhase") as MockEval,
        patch("src.orchestration.pipeline_orchestrator.IntegrationPhase") as MockInt,
    ):
        # モックの挙動設定
        mock_acq_inst = MockAcq.return_value
        dummy_data = {"1001": "dummy_price_data"}
        mock_acq_inst.execute.return_value = dummy_data

        mock_eval_inst = MockEval.return_value
        dummy_ranks = MagicMock()  # pl.DataFrame の代わり
        mock_eval_inst.execute.return_value = dummy_ranks

        mock_int_inst = MockInt.return_value

        # 実行
        orchestrator = PipelineOrchestrator(stub_context)
        orchestrator.run()

        # 呼び出しの検証
        # 1. Acquisition が実行されたか
        mock_acq_inst.execute.assert_called_once()

        # 2. Evaluation が前のデータを受け取って実行されたか
        mock_eval_inst.execute.assert_called_once_with(data_map=dummy_data)

        # 3. Integration がランキング結果を受け取って実行されたか
        mock_int_inst.execute.assert_called_once_with(dummy_ranks)

        # ログの確認
        assert stub_context.logger.info.called


def test_pipeline_orchestrator_run_exception_handling(stub_context):
    """異常系: フェーズ内で例外が発生してもキャッチされること"""
    with patch("src.orchestration.pipeline_orchestrator.AcquisitionPhase") as MockAcq:
        mock_acq_inst = MockAcq.return_value
        mock_acq_inst.execute.side_effect = Exception("Fetch Error")

        orchestrator = PipelineOrchestrator(stub_context)
        # 例外が外に漏れないことを確認
        orchestrator.run()

        # エラーログが記録されていること
        assert stub_context.logger.error.called
