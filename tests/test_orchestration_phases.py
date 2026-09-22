"""src/orchestration/ モジュール群の包括的テスト"""

import pytest
from unittest.mock import MagicMock, patch

from src.orchestration.context import OrchestratorContext
from src.orchestration.pipeline_orchestrator import PipelineOrchestrator
from src.orchestration.scan_handler import ScanHandler


def test_orchestrator_context_init():
    ctx = OrchestratorContext(debug_mode=True)
    assert ctx.debug_mode is True
    assert ctx.config is not None


def test_scan_handler_basic():
    ctx = OrchestratorContext(debug_mode=True)
    sh = ScanHandler()
    assert sh is not None


def test_pipeline_orchestrator_basic():
    ctx = OrchestratorContext(debug_mode=True)
    po = PipelineOrchestrator(ctx)
    assert po is not None
