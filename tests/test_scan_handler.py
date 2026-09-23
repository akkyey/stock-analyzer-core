from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from src.orchestration.scan_handler import ScanHandler


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.config = {
        "strategies": {"growth": {}},
        "ai": {"target_limit": 10},
        "scoring": {"scan_limit": 100},
    }
    context.logger = MagicMock()
    context.debug_mode = True
    context.perf_stats = {"calc_sec": 0, "db_sec": 0}
    return context


def test_scan_handler_mode_name():
    """ScanHandler のモード名が 'daily' であることのテスト"""
    handler = ScanHandler()
    assert handler.get_mode_name() == "daily"


@patch("src.orchestration.scan_handler.OrchestrationPipeline")
def test_scan_handler_execute_success(mock_pipeline_cls, mock_context):
    """ScanHandler.execute が OrchestrationPipeline を呼び出し正常終了するテスト"""
    mock_pipeline = mock_pipeline_cls.return_value
    mock_pipeline.run.return_value = pl.DataFrame({"code": ["7203"]})

    handler = ScanHandler()
    handler.execute(mock_context)

    mock_pipeline_cls.assert_called_once_with(mock_context)
    mock_pipeline.run.assert_called_once()
    mock_context.print_session_summary.assert_called_once()


@patch("src.orchestration.scan_handler.OrchestrationPipeline")
def test_scan_handler_execute_failure(mock_pipeline_cls, mock_context):
    """ScanHandler.execute でパイプライン実行時エラーが適切にハンドリングされるテスト"""
    mock_pipeline = mock_pipeline_cls.return_value
    mock_pipeline.run.side_effect = RuntimeError("Pipeline crash")

    handler = ScanHandler()
    with pytest.raises(RuntimeError, match="Pipeline crash"):
        handler.execute(mock_context)

    mock_context.logger.error.assert_called()
