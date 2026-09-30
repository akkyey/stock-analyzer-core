"""src/orchestration/ モジュール群の包括的テスト"""

from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from src.orchestration.context import OrchestratorContext
from src.orchestration.phases.integration import IntegrationPhase
from src.orchestration.pipeline import OrchestrationPipeline
from src.orchestration.scan_handler import ScanHandler


def test_orchestrator_context_init():
    ctx = OrchestratorContext(debug_mode=True)
    assert ctx.debug_mode is True
    assert ctx.config is not None


def test_scan_handler_basic():
    ctx = OrchestratorContext(debug_mode=True)
    sh = ScanHandler()
    assert sh is not None


def test_orchestration_pipeline_basic():
    ctx = OrchestratorContext(debug_mode=True)
    op = OrchestrationPipeline(ctx)
    assert op is not None


def test_integration_phase_data_contract_guard():
    """IntegrationPhase の _verify_data_contract が null および NaN を確実に検知して遮断すること"""
    ctx = OrchestratorContext(debug_mode=True)
    phase = IntegrationPhase(ctx)

    # 1. 正常な DataFrame
    valid_df = pl.DataFrame(
        {
            "code": ["7203"],
            "price": [2500.0],
            "verdict": ["Grade A"],
        }
    )
    # 例外なく通過すること
    phase._verify_data_contract(valid_df)

    # 2. price に null が含まれる場合
    null_df = pl.DataFrame(
        {
            "code": ["7203", "6758"],
            "price": [2500.0, None],
            "verdict": ["Grade A", "Grade B"],
        }
    )
    with pytest.raises(AssertionError, match="Data Contract Violation"):
        phase._verify_data_contract(null_df)

    # 3. price に float("nan") が含まれる場合
    nan_df = pl.DataFrame(
        {
            "code": ["7203", "6758"],
            "price": [2500.0, float("nan")],
            "verdict": ["Grade A", "Grade B"],
        }
    )
    with pytest.raises(AssertionError, match="Data Contract Violation"):
        phase._verify_data_contract(nan_df)


