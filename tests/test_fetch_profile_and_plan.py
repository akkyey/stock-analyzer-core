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
    # 事前インターバルは実測 (429 なし) により標準と同値。リトライ予算のみ拡大
    assert COLAB.inter_batch_sleep == STANDARD.inter_batch_sleep
    assert COLAB.max_retries > STANDARD.max_retries
    assert COLAB.empty_backoff_base >= STANDARD.empty_backoff_base
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


def _fake_download_factory(batch_empty: bool, canary_ok: bool, calls: list):
    import pandas as pd

    def fake_download(tickers, **kwargs):
        calls.append(list(tickers))
        if tickers == ["7203.T"]:
            return pd.DataFrame({"Close": [1.0]}) if canary_ok else pd.DataFrame()
        if batch_empty:
            return pd.DataFrame()
        cols = pd.MultiIndex.from_product([tickers, ["Close"]])
        return pd.DataFrame([[1.0] * len(tickers)], columns=cols)

    return fake_download


def _run_fetch(monkeypatch, batch_empty, canary_ok):
    from src.fetcher import market_fetcher as mf

    calls: list = []
    sleeps: list = []
    monkeypatch.setattr(mf.yf, "download", _fake_download_factory(batch_empty, canary_ok, calls))
    monkeypatch.setattr(mf.time, "sleep", lambda s: sleeps.append(s))
    f = mf.MarketFetcher({})
    res = f.fetch_market_data(["9999", "9998"], period="1y", context=_Ctx({"fetch_profile": "standard"}))
    return res, calls, sleeps


def test_empty_batch_without_rate_limit_skips_backoff(monkeypatch):
    """データの無い銘柄だけのバッチ (上場廃止・PRO Market 等) は、試し取得が成功すれば待機・再試行しない"""
    res, calls, sleeps = _run_fetch(monkeypatch, batch_empty=True, canary_ok=True)
    assert res == {}
    assert calls == [["9999.T", "9998.T"], ["7203.T"]]  # バッチ 1 回 + 試し取得 1 回のみ
    assert max(sleeps) < 1.0  # 5 秒以上のバックオフ待機が発生しない


def test_empty_batch_under_rate_limit_keeps_backoff(monkeypatch):
    """試し取得も失敗する (レート制限) 場合は、従来どおりバックオフして再試行する"""
    res, calls, sleeps = _run_fetch(monkeypatch, batch_empty=True, canary_ok=False)
    batch_calls = [c for c in calls if c != ["7203.T"]]
    assert len(batch_calls) == STANDARD.max_retries
    assert any(s >= STANDARD.empty_backoff_base for s in sleeps)


def test_non_empty_batch_does_not_probe(monkeypatch):
    res, calls, _ = _run_fetch(monkeypatch, batch_empty=False, canary_ok=True)
    assert set(res) == {"9999", "9998"}
    assert ["7203.T"] not in calls


def test_rows_without_close_are_dropped(monkeypatch):
    """Yahoo が直近日を終値 NaN で返しても、その行は採用しない (DB の正しい値を上書きしない)"""
    import numpy as np
    import pandas as pd

    from src.fetcher import market_fetcher as mf

    idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
    cols = pd.MultiIndex.from_product([["3549.T", "7203.T"], ["Close", "Volume"]])
    raw = pd.DataFrame(
        [[3711.0, 100.0, 2925.0, 200.0], [np.nan, 0.0, np.nan, 0.0]], index=idx, columns=cols
    )
    f = mf.MarketFetcher({})
    out = f._extract_dfs_from_batch(raw, ["3549.T", "7203.T"])
    assert list(out["3549"].index) == [pd.Timestamp("2026-10-01")]
    assert out["3549"]["Close"].notna().all()

    # 全行が NaN の銘柄は「取得できなかった」扱い
    raw2 = pd.DataFrame([[np.nan, 0.0, 1.0, 1.0]], index=idx[:1], columns=cols)
    out2 = f._extract_dfs_from_batch(raw2, ["3549.T", "7203.T"])
    assert "3549" not in out2 and "7203" in out2


def test_plan_uses_last_db_date_to_cover_gaps():
    """実行間隔が空いた銘柄は、欠落日を含む期間で取得する (固定 2d では欠落が残る)"""
    from datetime import date, datetime, timedelta

    n = AcquisitionPhase.MIN_HISTORY_ROWS
    end = date(2026, 9, 18)  # 金曜

    def hist(last):
        days = [last - timedelta(days=i) for i in range(n + 5)]
        return pl.DataFrame({"code": ["x"] * len(days), "Date": [datetime(d.year, d.month, d.day) for d in days]})

    ctx = StubOrchestratorContext()
    phase = AcquisitionPhase(ctx)
    batches = phase._plan_fetch_batches(
        ["1001", "1002"],
        {("1001",): hist(date(2026, 10, 1)), ("1002",): hist(end)},
        today=date(2026, 10, 2),
    )
    by_period = {c: p for codes, p in batches for c in codes}
    assert by_period == {"1001": "2d", "1002": "21d"}


def test_edinet_scan_days_covers_time_since_last_scan(monkeypatch):
    from datetime import datetime as dt

    from src.utils import JST

    ctx = StubOrchestratorContext()
    phase = AcquisitionPhase(ctx)

    class Repo:
        def __init__(self, last):
            self.meta = {AcquisitionPhase.EDINET_SCAN_META_KEY: last} if last else {}

        def get_meta(self, k):
            return self.meta.get(k)

        def set_meta(self, k, v):
            self.meta[k] = v

    monkeypatch.setattr(
        "src.orchestration.phases.acquisition.get_current_time",
        lambda: dt(2026, 10, 3, 10, 0, tzinfo=JST),
    )
    assert phase._edinet_scan_days(Repo(None), 30) == 30
    assert phase._edinet_scan_days(Repo("2026-09-30"), 30) == 30
    assert phase._edinet_scan_days(Repo("2026-07-01"), 30) == 95
    assert phase._edinet_scan_days(Repo("2024-01-01"), 30) == AcquisitionPhase.MAX_EDINET_SCAN_DAYS

    repo = Repo("2026-09-01")
    phase._record_edinet_scan(repo, [])
    assert repo.meta[AcquisitionPhase.EDINET_SCAN_META_KEY] == "2026-10-03"
    # 一覧を取得できなかった日があれば、その前日までしか進めない
    repo = Repo("2026-09-01")
    phase._record_edinet_scan(repo, ["2026-09-20", "2026-09-25"])
    assert repo.meta[AcquisitionPhase.EDINET_SCAN_META_KEY] == "2026-09-01"
    repo = Repo("2026-09-01")
    phase._record_edinet_scan(repo, ["2026-08-20"])
    assert repo.meta[AcquisitionPhase.EDINET_SCAN_META_KEY] == "2026-08-19"


def test_plan_forced_full_history_for_split_backfill():
    """株式分割の記録が無い DB の初回は、履歴が十分でも全銘柄 1y で取り直す"""
    n = AcquisitionPhase.MIN_HISTORY_ROWS
    batches = _plan({"_force_full_history": True}, {"1001": n + 40}, ["1001"])
    assert [p for _, p in batches] == ["1y"]
