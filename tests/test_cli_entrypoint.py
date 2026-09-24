"""CLI エントリポイントの単体テスト (指摘3)"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.__main__ import main


def test_cli_entrypoint_scan_mode():
    """python -m src scan で ScanHandler が呼び出されること"""
    test_args = ["src", "scan"]
    with patch.object(sys, "argv", test_args):
        with patch("src.orchestration.scan_handler.ScanHandler.execute") as mock_exec:
            exit_code = main()
            assert exit_code == 0
            assert mock_exec.called


def test_cli_entrypoint_mode_flag():
    """python -m src --mode scan で ScanHandler が呼び出されること"""
    test_args = ["src", "--mode", "scan"]
    with patch.object(sys, "argv", test_args):
        with patch("src.orchestration.scan_handler.ScanHandler.execute") as mock_exec:
            exit_code = main()
            assert exit_code == 0
            assert mock_exec.called


def test_cli_entrypoint_invalid_mode():
    """未知のモードが指定された場合は終了コード 1 が返ること"""
    test_args = ["src", "invalid_mode"]
    with patch.object(sys, "argv", test_args):
        exit_code = main()
        assert exit_code == 1
