from unittest.mock import MagicMock

import polars as pl
import pytest

from src.calc.engines.polars_engine import PolarsEngine
from src.orchestration.context import OrchestratorContext
from src.orchestration.phases.evaluation import EvaluationPhase


def test_polars_engine_whitelist_enforcement():
    """PolarsEngine (V13 Contract) が内部計算用の中間カラムをパージしつつ、契約カラムと入力属性を保持するか検証する。"""
    config = {
        "strategies": {
            "test_strat": {
                "base_score": 50,
                "points": {"roe": 10},
                "thresholds": {"roe": 10},
            }
        }
    }
    engine = PolarsEngine(config, strategy_name="test_strat")

    # 意図的に計算中間値や無関係なカラムを混入させる
    df = pl.DataFrame(
        {
            "code": ["1001"],
            "entry_date": ["2026-04-22"],
            "roe": [15.0],
            "sector": ["Tech"],  # ランキング処理に必要（維持されるべき入力属性）
            "score_base": [999.0],  # エンジン内部でパージされるべき中間カラム
            "fund_norm_test": [1.0],  # 内部パージ対象プレフィックス
        }
    )

    result = engine.calculate_scores(df)
    cols = result.columns

    # 内部計算中間値（INTERNAL_PURGE_PREFIXES）がパージされていること
    for prefix in engine.INTERNAL_PURGE_PREFIXES:
        for col in cols:
            assert not (
                col.startswith(prefix) and col not in engine.CONTRACT_COLUMNS
            ), f"Internal column {col} leaked!"

    # 契約カラムおよび入力属性が保持されていること
    for contract_col in engine.CONTRACT_COLUMNS:
        assert contract_col in cols, f"Contract column {contract_col} missing!"
    assert "sector" in cols, "Input attribute 'sector' should be preserved!"


def test_base_phase_integrity_guard_exception():
    """BasePhase._verify_integrity がサフィックスを検知して例外を投げるか検証。"""
    mock_context = MagicMock()
    mock_context.logger = MagicMock()
    phase = EvaluationPhase(mock_context)

    # サフィックスが混入した汚れた DataFrame
    dirty_df = pl.DataFrame(
        {"code": ["1001"], "quant_score_left": [80.0], "quant_score_right": [85.0]}
    )

    with pytest.raises(RuntimeError, match="Schema isolation failure: Suffix detected"):
        phase._verify_integrity(dirty_df)


def test_base_phase_integrity_guard_clean():
    """正常な DataFrame では例外が発生しないことを検証。"""
    mock_context = MagicMock()
    mock_context.logger = MagicMock()
    phase = EvaluationPhase(mock_context)

    clean_df = pl.DataFrame({"code": ["1001"], "quant_score": [85.0]})

    # 例外が発生しないこと
    phase._verify_integrity(clean_df)
