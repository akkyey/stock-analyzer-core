"""src/orchestration/ モジュール群の包括的テスト"""

from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.context import OrchestratorContext
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

