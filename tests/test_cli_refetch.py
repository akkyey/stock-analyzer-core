"""CLI の --refetch-edinet-year: 指定した実行だけ EDINET の走査日数が 365 になる。"""

import sys

import pytest

from src import __main__ as cli


class _Handler:
    seen: dict = {}

    def execute(self, context):
        _Handler.seen = dict(context.config.get("fetcher", {}))


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["prog", "scan"], 30),
        (["prog", "scan", "--refetch-edinet-year"], 365),
    ],
)
def test_refetch_option_applies_only_when_given(monkeypatch, tmp_path, argv, expected):
    monkeypatch.setenv("STOCK_ANALYZER_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "ScanHandler", _Handler)
    assert cli.main() == 0
    assert _Handler.seen["edinet_scan_days"] == expected
