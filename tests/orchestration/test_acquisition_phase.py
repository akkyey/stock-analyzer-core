from unittest.mock import MagicMock, patch

import pandas as pd
import polars as pl
import pytest

from src.orchestration.phases.acquisition import AcquisitionPhase
from tests.helpers.stubs import DataGenerator, StubOrchestratorContext


@pytest.fixture
def stub_context():
    return StubOrchestratorContext()


def test_acquisition_phase_execute_success(stub_context):
    """正常系: データの取得とマッピングが成功することを確認"""
    # 依存コンポーネントのパッチ
    with (
        patch("src.repositories.duck_repository.DuckDBRepository") as MockDuckRepo,
        patch(
            "src.repositories.market_data_repository.MarketDataRepository"
        ) as MockMarketRepo,
        patch("src.fetcher.facade.DataFetcher") as MockFetcher,
    ):
        # モックの設定
        mock_duck = MockDuckRepo.return_value
        mock_duck.get_all_codes.return_value = ["1001", "1002"]

        mock_market = MockMarketRepo.return_value
        mock_market.get_all_history_pl.return_value = pl.DataFrame(
            schema={"code": pl.Utf8, "Date": pl.Datetime}
        )

        mock_fetcher = MockFetcher.return_value
        # yfinance データの代わり
        hist_1001 = pd.DataFrame(
            {"Close": [100, 105], "Volume": [1000, 1100]},
            index=pd.to_datetime(["2026-04-01", "2026-04-02"]),
        )
        hist_1002 = pd.DataFrame(
            {"Close": [200, 190], "Volume": [500, 600]},
            index=pd.to_datetime(["2026-04-01", "2026-04-02"]),
        )
        mock_fetcher.fetch_stock_data.return_value = {
            "1001": hist_1001,
            "1002": hist_1002,
        }

        # 実行
        phase = AcquisitionPhase(stub_context)
        all_data_map = phase.execute()

        # 検証
        assert isinstance(all_data_map, dict)
        assert len(all_data_map) == 2
        assert "1001" in all_data_map
        assert "1002" in all_data_map
        assert isinstance(all_data_map["1001"], (pd.DataFrame, pl.DataFrame))
        # カバレッジ確認（log_infoが呼ばれたか等）
        assert stub_context.logger.info.called


def test_acquisition_phase_empty_targets(stub_context):
    """準正常系: ターゲット銘柄が0件の場合"""
    with patch("src.repositories.duck_repository.DuckDBRepository") as MockDuckRepo:
        mock_duck = MockDuckRepo.return_value
        mock_duck.get_all_codes.return_value = []

        phase = AcquisitionPhase(stub_context)
        all_data_map = phase.execute()

        assert isinstance(all_data_map, dict)
        assert len(all_data_map) == 0


def test_acquisition_phase_fetch_error(stub_context):
    """異常系: データ取得中に例外が発生した場合のエラーハンドリング"""
    with (
        patch("src.repositories.duck_repository.DuckDBRepository") as MockDuckRepo,
        patch(
            "src.repositories.market_data_repository.MarketDataRepository"
        ) as MockMarketRepo,
        patch("src.fetcher.facade.DataFetcher") as MockFetcher,
    ):
        mock_duck = MockDuckRepo.return_value
        mock_duck.get_all_codes.return_value = ["9999"]

        mock_market = MockMarketRepo.return_value
        mock_market.get_all_history_pl.return_value = pl.DataFrame(
            schema={"code": pl.Utf8, "Date": pl.Datetime}
        )

        mock_fetcher = MockFetcher.return_value
        # 取得時に例外を投げる
        mock_fetcher.fetch_stock_data.side_effect = Exception("Fetch Failed")

        phase = AcquisitionPhase(stub_context)
        all_data_map = phase.execute()

        # 失敗してもプログラムは停止せず、空または一部のデータマップを返すべき
        assert isinstance(all_data_map, dict)
        # エラーがログに記録されていること
        assert stub_context.logger.error.called


# --- 株式分割の取得フェーズでの扱い (調整・記録・冪等性) -------------------------------


def _split_history(closes, start="2026-01-05"):
    """営業日 (土日を除く) の終値履歴 (Yahoo の戻り値の形: 日付索引 + Close/Volume)。"""
    from datetime import date, timedelta

    days, d = [], date.fromisoformat(start)
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return pd.DataFrame(
        {"Close": closes, "Volume": [1000] * len(closes)}, index=pd.to_datetime(days)
    )


def _run_acquisition(stub_context, hist, events=None, db_history=None, recorded=None):
    """取得フェーズを実行し、(戻り値の履歴, save_splits に渡った分割) を返す。"""
    with (
        patch("src.repositories.duck_repository.DuckDBRepository") as MockDuckRepo,
        patch(
            "src.repositories.market_data_repository.MarketDataRepository"
        ) as MockMarketRepo,
        patch("src.fetcher.facade.DataFetcher") as MockFetcher,
    ):
        mock_duck = MockDuckRepo.return_value
        mock_duck.get_all_codes.return_value = ["1001"]
        mock_duck.load_splits.return_value = (
            recorded
            if recorded is not None
            else pl.DataFrame(
                schema={"code": pl.Utf8, "split_date": pl.Date, "ratio": pl.Float64}
            )
        )
        MockMarketRepo.return_value.get_all_history_pl.return_value = (
            db_history
            if db_history is not None
            else pl.DataFrame(schema={"code": pl.Utf8, "Date": pl.Datetime})
        )
        fetcher = MockFetcher.return_value
        fetcher.fetch_stock_data.return_value = {"1001": hist}
        pending = [events or {}]
        fetcher.market_fetcher.pop_detected_splits.side_effect = lambda: (
            pending.pop(0) if pending else {}
        )

        result = AcquisitionPhase(stub_context).execute()
        saved = [c.args[0] for c in mock_duck.save_splits.call_args_list if c.args]
    return result, [row for rows in saved for row in rows]


def test_acquisition_adjusts_history_with_yahoo_split_event(stub_context):
    from datetime import date

    # 3:1 分割: 26 営業日目に段差 (Yahoo が調整していない履歴)
    hist = _split_history([3000.0] * 25 + [1000.0] * 25)
    jump = hist.index[25].date()
    result, saved = _run_acquisition(
        stub_context, hist, events={"1001": [(jump, 3.0)]}
    )
    closes = result["1001"].sort("Date")["Close"].to_list()
    assert max(closes) == pytest.approx(1000.0)  # 分割前が 1/3 に調整された
    assert saved == [("1001", jump, 3.0)]
    assert isinstance(saved[0][1], date)


def test_acquisition_leaves_already_adjusted_history_untouched(stub_context):
    # Yahoo が調整済み (段差なし) の分割: イベントは記録するが、株価は変えない
    hist = _split_history([1000.0] * 50)
    event_day = hist.index[25].date()
    result, saved = _run_acquisition(
        stub_context, hist, events={"1001": [(event_day, 3.0)]}
    )
    assert result["1001"].sort("Date")["Close"].to_list() == [1000.0] * 50
    assert saved == [("1001", event_day, 3.0)]


def test_acquisition_infers_split_without_event(stub_context):
    # イベントが無い 10:1 (8377 の例): 段差から推定して調整・記録する
    hist = _split_history([8000.0] * 25 + [800.0] * 25)
    result, saved = _run_acquisition(stub_context, hist)
    closes = result["1001"].sort("Date")["Close"].to_list()
    assert max(closes) == pytest.approx(800.0)
    assert len(saved) == 1 and saved[0][0] == "1001" and saved[0][2] == pytest.approx(10.0)


def test_acquisition_does_not_treat_widened_limit_surge_as_split(stub_context):
    # 6072 の例: ストップ高が連続した後の 1 日で約 2 倍。併合とみなさない
    closes = [196.0] * 22 + [198.0, 248.0, 328.0, 648.0] + [700.0] * 24
    hist = _split_history(closes)
    result, saved = _run_acquisition(stub_context, hist)
    assert result["1001"].sort("Date")["Close"].to_list() == closes
    assert saved == []


def test_acquisition_ignores_implausible_split_event(stub_context):
    hist = _split_history([1000.0] * 50)
    result, saved = _run_acquisition(
        stub_context, hist, events={"1001": [(hist.index[25].date(), 2.27e-07)]}
    )
    assert result["1001"].sort("Date")["Close"].to_list() == [1000.0] * 50
    assert saved == []


def test_acquisition_is_idempotent_with_recorded_small_ratio_split(stub_context):
    """4923 の例: 記録済みの 1:1.05 の分割で、実行のたびに履歴が割られない"""
    from datetime import date

    hist = _split_history([1200.0] * 25 + [1210.0] * 25)
    recorded = pl.DataFrame(
        {"code": ["1001"], "split_date": [date(2026, 2, 10)], "ratio": [1.05]}
    )
    first, _ = _run_acquisition(stub_context, hist, recorded=recorded)
    second, _ = _run_acquisition(stub_context, hist, recorded=recorded)
    expected = hist["Close"].to_list()
    assert first["1001"].sort("Date")["Close"].to_list() == expected
    assert second["1001"].sort("Date")["Close"].to_list() == expected


# --- 画面に出る銘柄数の説明 (3,920 → 3,912 → 3,892 の理由) ---------------------------


def test_fundamentals_seed_note_counts_codes_missing_from_seed(stub_context, capsys, tmp_path):
    """財務シードの注記は、件数の差ではなく、取得対象のうちシードに無い銘柄の数を出す"""
    seed = pl.DataFrame({"code": ["1001", "1002", "9999"]})  # 9999 は上場廃止済みでもシードにある
    with (
        patch("src.repositories.fundamentals_repository.FundamentalsRepository") as MockFunda,
        patch("pathlib.Path.exists", return_value=True),
        patch("polars.read_parquet", return_value=seed),
    ):
        MockFunda.return_value.get_count.return_value = 0
        MockFunda.return_value.backfill_growth_from_seed.return_value = 0
        repo = MagicMock()
        repo.get_all_codes.return_value = ["1001", "1002", "1003", "1004"]  # 1003・1004 はシードに無い
        repo.get_meta.return_value = "done"
        AcquisitionPhase(stub_context)._ensure_fundamentals(repo)
    out = capsys.readouterr().out
    assert "財務シードデータから 3 銘柄" in out
    assert "取得対象のうち 2 銘柄" in out


def test_fundamentals_seed_note_omitted_when_all_codes_are_in_seed(stub_context, capsys):
    seed = pl.DataFrame({"code": ["1001", "1002", "9999"]})
    with (
        patch("src.repositories.fundamentals_repository.FundamentalsRepository") as MockFunda,
        patch("pathlib.Path.exists", return_value=True),
        patch("polars.read_parquet", return_value=seed),
    ):
        MockFunda.return_value.get_count.return_value = 0
        MockFunda.return_value.backfill_growth_from_seed.return_value = 0
        repo = MagicMock()
        repo.get_all_codes.return_value = ["1001", "1002"]
        repo.get_meta.return_value = "done"
        AcquisitionPhase(stub_context)._ensure_fundamentals(repo)
    out = capsys.readouterr().out
    assert "財務シードデータから 3 銘柄を初期登録しました。" in out
    assert "未収録" not in out


def test_monthly_master_sync_result_is_shown_on_screen(stub_context, capsys):
    """銘柄マスタの月次同期 (新規上場・上場廃止) の結果は、画面にも出す"""
    from types import SimpleNamespace

    repo = MagicMock()
    repo.get_all_codes.side_effect = [["1001"] * 3920, ["1001"] * 3892]
    fetcher = MagicMock()
    result = SimpleNamespace(status="updated", added=43, delisted=71, relisted=0, detail="")
    stub_context.config["fetcher"] = {"refresh_stock_master": True}
    with patch("src.services.stock_master.refresh_stock_master", return_value=result):
        codes = AcquisitionPhase(stub_context)._ensure_stock_master(repo, fetcher)
    out = capsys.readouterr().out
    assert len(codes) == 3892
    assert "新規上場 43 / 上場廃止 71 / 再上場 0 銘柄" in out
    assert "取得対象 3892 銘柄" in out


# --- Colab 1 回目 (2026-10-05) の実測を受けた画面表示 -----------------------------------


def test_split_summary_line_is_printed_with_adjusted_count(stub_context, capsys):
    """株式分割の記録・調整の件数を、画面にも 1 行出す"""
    from datetime import date

    hist = _split_history([3000.0] * 25 + [1000.0] * 25)  # 3:1 分割の段差が残っている
    jump = hist.index[25].date()
    _run_acquisition(stub_context, hist, events={"1001": [(jump, 3.0)]})
    out = capsys.readouterr().out
    assert "株式分割: 新たに 1 件を記録、株価を調整した銘柄 1 件" in out
    assert isinstance(jump, date)


def test_split_summary_line_is_silent_when_nothing_happened(stub_context, capsys):
    _run_acquisition(stub_context, _split_history([1000.0] * 50))
    assert "株式分割:" not in capsys.readouterr().out


def test_split_tracker_counts_only_stocks_whose_prices_changed():
    from datetime import date

    import polars as pl

    from src.fetcher.incremental import SplitTracker

    tracker = SplitTracker(
        pl.DataFrame({"code": ["A", "B"], "split_date": [date(2026, 2, 10)] * 2, "ratio": [3.0, 3.0]})
    )

    def series(closes):
        from datetime import datetime, timedelta

        return pl.DataFrame(
            {"Date": [datetime(2026, 1, 5) + timedelta(days=i) for i in range(len(closes))], "Close": closes}
        )

    tracker.adjust("A", series([3000.0] * 36 + [1000.0] * 14))  # 段差が残っている → 調整
    tracker.adjust("B", series([1000.0] * 50))  # Yahoo が調整済み → 変えない
    assert tracker.adjusted_codes == {"A"}


def test_edinet_completion_message_separates_new_downloads_from_cache(stub_context, capsys):
    """「反映」は毎回の当て直し件数。新たに取得した件数と、キャッシュで省いた件数を分けて出す"""
    from unittest.mock import MagicMock, patch

    def run(downloads, cache_hits):
        stub_context.config["fetcher"] = {"enable_edinet_turbo": True}
        mgr = MagicMock(downloads=downloads, cache_hits=cache_hits, failed_dates=[])
        with (
            patch("src.fetcher.turbo_acquisition.TurboAcquisitionManager", return_value=mgr),
            patch("src.fetcher.edinet_fetcher.EdinetFetcher"),
            patch("src.services.edinet_bridge.EdinetBridge") as bridge,
        ):
            bridge.return_value.bridge_all.return_value = 269
            repo = MagicMock()
            repo.get_meta.return_value = None
            AcquisitionPhase(stub_context)._sync_edinet(repo)
        return capsys.readouterr().out

    # 初回: 全てダウンロード。キャッシュには触れない
    first = run(downloads=273, cache_hits=0)
    assert "新たに取得 273 件 / 反映した開示書類 269 件" in first and "キャッシュ" not in first
    # 2 回目: 通信は 0 件。取得済みの書類はキャッシュを利用 (Colab 2 回目の実測)
    later = run(downloads=0, cache_hits=271)
    assert "新たに取得 0 件 / 取得済み 271 件はキャッシュを利用 / 反映した開示書類 269 件" in later


# --- 作り直した同梱シードの、既存 DB への反映 (シードごとに 1 回だけ) --------------------------


def _seed_file(tmp_path, content=b"seed-v1"):
    path = tmp_path / "seed.parquet"
    path.write_bytes(content)
    return path


def _refresh(stub_context, tmp_path, meta, enabled=True, updated=5, content=b"seed-v1"):
    from src.repositories.fundamentals_repository import seed_fingerprint

    stub_context.config["fetcher"] = {"refresh_fundamentals_from_seed": enabled}
    repo, funda = MagicMock(), MagicMock()
    funda.refresh_from_seed.return_value = updated
    seed = _seed_file(tmp_path, content)
    repo.get_meta.return_value = meta(seed_fingerprint) if callable(meta) else meta
    AcquisitionPhase(stub_context)._refresh_from_seed(repo, funda, seed)
    return repo, funda, seed


def test_seed_refresh_runs_once_and_shows_count(stub_context, tmp_path, capsys):
    """初めてのシード: 反映して、印 (シードの識別子) を残し、画面に件数を出す"""
    from src.repositories.fundamentals_repository import seed_fingerprint

    repo, funda, seed = _refresh(stub_context, tmp_path, meta=None, updated=1309)
    funda.refresh_from_seed.assert_called_once_with(seed)
    repo.set_meta.assert_called_once_with(AcquisitionPhase.SEED_REFRESH_META_KEY, seed_fingerprint(seed))
    out = capsys.readouterr().out
    assert "1309 銘柄更新しました" in out and "決算期の新しい値は変更していません" in out


def test_seed_refresh_is_skipped_for_the_same_seed(stub_context, tmp_path, capsys):
    """同じ版のシードに対しては、2 回目以降は何もしない"""
    repo, funda, _ = _refresh(stub_context, tmp_path, meta=lambda fp: fp(_seed_file(tmp_path)))
    funda.refresh_from_seed.assert_not_called()
    repo.set_meta.assert_not_called()
    assert capsys.readouterr().out == ""


def test_seed_refresh_runs_again_when_seed_is_rebuilt(stub_context, tmp_path):
    """シードを作り直すと (識別子が変わると)、もう一度反映する"""
    _, funda, _ = _refresh(stub_context, tmp_path, meta="an-older-seed-id")
    funda.refresh_from_seed.assert_called_once()


def test_seed_refresh_disabled_by_setting_leaves_no_mark(stub_context, tmp_path, capsys):
    """設定で省いた場合は、印を残さない (あとで有効にして実行できる)"""
    repo, funda, _ = _refresh(stub_context, tmp_path, meta=None, enabled=False)
    funda.refresh_from_seed.assert_not_called()
    repo.set_meta.assert_not_called()
    assert "更新しました" not in capsys.readouterr().out


def test_seed_refresh_zero_updates_is_silent_but_marked(stub_context, tmp_path, capsys):
    repo, _, _ = _refresh(stub_context, tmp_path, meta=None, updated=0)
    repo.set_meta.assert_called_once()
    assert "更新しました" not in capsys.readouterr().out


def test_seed_refresh_failure_does_not_stop_the_run_or_mark(stub_context, tmp_path):
    stub_context.config["fetcher"] = {}
    repo, funda = MagicMock(), MagicMock()
    repo.get_meta.return_value = None
    funda.refresh_from_seed.side_effect = RuntimeError("boom")
    AcquisitionPhase(stub_context)._refresh_from_seed(repo, funda, _seed_file(tmp_path))
    repo.set_meta.assert_not_called()


def test_new_database_gets_the_seed_mark_without_refresh(stub_context, capsys):
    """新規の DB は、読み込んだシードが最新なので、印だけを残し、「更新しました」とは出さない"""
    seed = pl.DataFrame({"code": ["1001", "1002"]})
    with (
        patch("src.repositories.fundamentals_repository.FundamentalsRepository") as MockFunda,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_bytes", return_value=b"seed-v1"),
        patch("polars.read_parquet", return_value=seed),
    ):
        MockFunda.return_value.get_count.return_value = 0
        MockFunda.return_value.backfill_growth_from_seed.return_value = 0
        repo = MagicMock()
        repo.get_all_codes.return_value = ["1001", "1002"]
        store: dict = {}
        repo.get_meta.side_effect = store.get
        repo.set_meta.side_effect = store.__setitem__
        AcquisitionPhase(stub_context)._ensure_fundamentals(repo)
    assert store[AcquisitionPhase.SEED_REFRESH_META_KEY]  # 読み込んだシードの識別子が記録される
    MockFunda.return_value.refresh_from_seed.assert_not_called()
    assert "銘柄更新しました" not in capsys.readouterr().out


def test_notebook_passes_refresh_checkbox_to_pipeline_config():
    import json

    with open("notebooks/stock_analyzer_colab.ipynb") as f:
        cells = ["".join(c["source"]) for c in json.load(f)["cells"]]
    assert any('refresh_fundamentals = True  # @param {type:"boolean"}' in c for c in cells)  # 既定は ON
    step3 = next(c for c in cells if "OrchestratorContext(" in c)
    assert '["refresh_fundamentals_from_seed"]' in step3 and 'globals().get("refresh_fundamentals", True)' in step3
