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
    stub_context.duck_repo.load_stocks.return_value = (
        DataGenerator.get_dummy_stocks_df()
    )

    # Repo, Scorer 等の内部依存を一部パッチ
    with (
        patch("src.calc.engine.ScoringEngine") as MockEngine,
        patch(
            "src.repositories.fundamentals_repository.FundamentalsRepository"
        ) as MockFunda,
    ):
        # ScoringEngine のモック (計算結果を模倣)
        mock_engine_inst = MockEngine.return_value

        # 簡易的な計算結果を返すモックを設定
        def mock_calc_score(df, strategy_name):
            # [v13] 属性維持契約を模倣: 入力 df にスコアとランクを付与して返す
            return df.with_columns(
                [
                    pl.lit(80.0).alias("quant_score"),
                    pl.lit(1).alias("rank"),
                    pl.lit(strategy_name).alias("strategy_name"),
                ]
            )

        mock_engine_inst.calculate_score.side_effect = mock_calc_score

        # FundamentalsRepository (空を返す)
        MockFunda.return_value.get_all_pl.return_value = pl.DataFrame()

        # 実行
        phase = EvaluationPhase(stub_context)
        final_df = phase.execute(data_map=data_map)

        # 検証
        assert final_df is not None
        assert not final_df.is_empty()
        assert "quant_score" in final_df.columns
        assert "sector" in final_df.columns  # マスタ結合の確認
        assert "strategy_name" in final_df.columns
        assert len(final_df) <= 50
        assert hasattr(stub_context, "stock_dossiers")
        assert len(stub_context.stock_dossiers) > 0
        assert stub_context.stock_dossiers[0]["code"] in ["1001", "1002"]


def test_evaluation_phase_no_data(stub_context):
    """異常系: 入力データが空の場合、Noneを返すこと"""
    stub_context.duck_repo.load_metrics.return_value = pl.DataFrame()

    phase = EvaluationPhase(stub_context)
    result = phase.execute(data_map={})

    assert result is None
