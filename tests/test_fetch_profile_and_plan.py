"""Colab 専用取得プロファイルと、銘柄ごとの 1y/2d 取得計画の検証"""

import polars as pl
import pytest

from src.fetcher.fetch_profile import COLAB, STANDARD, resolve_fetch_profile
from src.orchestration.phases.acquisition import AcquisitionPhase
from tests.helpers.stubs import StubOrchestratorContext


class _Ctx:
    def __init__(self, config):
        self.config = config


def test_profile_defaults_to_standard(monkeypatch):
    monkeypatch.delenv("STOCK_ANALYZER_FETCH_PROFILE", raising=False)
    assert resolve_fetch_profile(None) is STANDARD
    assert resolve_fetch_profile(_Ctx({})) is STANDARD
    assert resolve_fetch_profile(_Ctx({"fetch_profile": "unknown"})) is STANDARD


def test_profile_selection_order(monkeypatch):
    monkeypatch.setenv("STOCK_ANALYZER_FETCH_PROFILE", "colab")
    assert resolve_fetch_profile(_Ctx({})) is COLAB
    # context.config が環境変数より優先
    assert resolve_fetch_profile(_Ctx({"fetch_profile": "standard"})) is STANDARD


def test_colab_profile_is_more_conservative():
    assert COLAB.inter_batch_sleep > STANDARD.inter_batch_sleep
    assert COLAB.max_retries > STANDARD.max_retries
    # 標準は従来値 (CLI 挙動を変えない)
    assert (STANDARD.inter_batch_sleep, STANDARD.max_retries) == (0.2, 3)


def _hist(rows):
    return pl.DataFrame({"code": ["x"] * rows, "Date": list(range(rows))})


def _plan(config, db_rows, codes):
    ctx = StubOrchestratorContext()
    ctx.config.update(config)
    phase = AcquisitionPhase(ctx)
    hist = {(c, ): _hist(r) for c, r in db_rows.items()}
    return phase._plan_fetch_batches(codes, hist)


def test_plan_splits_by_history_per_stock():
    n = AcquisitionPhase.MIN_HISTORY_ROWS
    batches = _plan(
        {},
        {"1001": n, "1002": n + 40, "1003": n - 1},  # 1004 は履歴なし
        ["1001", "1002", "1003", "1004"],
    )
    by_period = {}
    for codes, period in batches:
        by_period.setdefault(period, []).extend(codes)
    assert sorted(by_period["2d"]) == ["1001", "1002"]
    assert sorted(by_period["1y"]) == ["1003", "1004"]


def test_plan_first_run_forces_full_history_and_covers_all_codes():
    n = AcquisitionPhase.MIN_HISTORY_ROWS
    codes = [str(1000 + i) for i in range(45)]
    batches = _plan({"is_first_run": True}, {c: n + 10 for c in codes}, codes)
    assert {p for _, p in batches} == {"1y"}
    flat = [c for b, _ in batches for c in b]
    assert sorted(flat) == sorted(codes)
    assert all(len(b) <= AcquisitionPhase.FETCH_BATCH_SIZE for b, _ in batches)


def test_plan_empty_db_cli_is_full_history():
    """CLI (is_first_run 未設定) でも、履歴が無ければ全銘柄 1y"""
    batches = _plan({}, {}, ["1001", "1002"])
    assert [(sorted(b), p) for b, p in batches] == [(["1001", "1002"], "1y")]


def test_upload_file_to_drive_uses_shared_folder_and_subfolder(tmp_path, monkeypatch):
    """CLI 経路: 親フォルダ (引数 > 環境変数) とサブフォルダ指定が反映される"""
    import sys
    import types

    from src.colab_tools import ColabTools

    created = []

    class _Req:
        def __init__(self, result):
            self._r = result

        def execute(self):
            return self._r

    class _Files:
        def list(self, **kw):
            return _Req({"files": []})  # サブフォルダは未存在

        def create(self, body, **kw):
            created.append(body)
            if body.get("mimeType") == "application/vnd.google-apps.folder":
                return _Req({"id": "SUBFOLDER"})
            return _Req({"id": "F1", "webViewLink": "https://example/view"})

    class _Service:
        def files(self):
            return _Files()

    google = types.ModuleType("google")
    oauth2 = types.ModuleType("google.oauth2")
    sa = types.ModuleType("google.oauth2.service_account")
    sa.Credentials = types.SimpleNamespace(from_service_account_file=lambda *a, **k: object())
    oauth2.service_account = sa
    gac = types.ModuleType("googleapiclient")
    disc = types.ModuleType("googleapiclient.discovery")
    disc.build = lambda *a, **k: _Service()
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda *a, **k: object()
    for name, mod in {
        "google": google,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": sa,
        "googleapiclient": gac,
        "googleapiclient.discovery": disc,
        "googleapiclient.http": http,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    report = tmp_path / "daily_report.csv"
    report.write_text("a,b\n1,2\n")
    monkeypatch.setenv("GDRIVE_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("GDRIVE_SHARED_FOLDER_ID", "ENVPARENT")

    url = ColabTools.upload_file_to_drive(
        str(report), drive_folder_name="reports", parent_folder_id="ARGPARENT"
    )
    assert url == "https://example/view"
    assert created[0]["parents"] == ["ARGPARENT"]  # サブフォルダは引数の親の配下
    assert created[1]["parents"] == ["SUBFOLDER"]  # ファイルはサブフォルダの配下

    created.clear()
    ColabTools.upload_file_to_drive(str(report))  # 環境変数の親にフォールバック
    assert created[0]["parents"] == ["ENVPARENT"]
