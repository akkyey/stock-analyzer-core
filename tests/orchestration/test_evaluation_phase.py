from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from src.orchestration.phases.evaluation import EvaluationPhase
from tests.helpers.stubs import DataGenerator, StubOrchestratorContext


@pytest.fixture
def stub_context():
    config = {
        "strategies": {
            "Balanced Strategy": {
                "base_score": 50.0,
                "points": {"roe": 10.0},
                "thresholds": {"roe": 10.0},
            }
        }
    }
    return StubOrchestratorContext(config)


def test_evaluation_phase_execute_push_flow(stub_context):
    """正常系: プッシュ型で渡されたデータをスコアリングし、マスタが結合されること"""
    # 合成データの準備
    data_map = {
        "1001": DataGenerator.generate_history("1001", 120),
        "1002": DataGenerator.generate_history("1002", 120),
    }

    # マスタデータのモック設定
    stub_context.stock_repo.load_all.return_value = DataGenerator.get_dummy_stocks_df()
    stub_context.funda_repo.load_all.return_value = pl.DataFrame(
        {
            "code": ["1001", "1002"],
            "roe": [15.0, 8.0],
            "per": [10.0, 20.0],
            "pbr": [1.0, 1.5],
            "equity_ratio": [60.0, 40.0],
        }
    )

    # 実行
    phase = EvaluationPhase(stub_context)
    final_df = phase.execute(data_map=data_map)

    # 検証
    assert final_df is not None
    assert not final_df.is_empty()
    assert "quant_score" in final_df.columns
    assert "verdict" in final_df.columns
    assert "sector" in final_df.columns  # マスタ結合の確認
    assert hasattr(stub_context, "uncalculable_df")
    assert stub_context.evaluated_count > 0


def test_evaluation_phase_no_data(stub_context):
    """異常系: 入力データが空の場合、Noneを返すこと"""
    stub_context.duck_repo.load_metrics.return_value = pl.DataFrame()

    phase = EvaluationPhase(stub_context)
    result = phase.execute(data_map={})

    assert result is None
