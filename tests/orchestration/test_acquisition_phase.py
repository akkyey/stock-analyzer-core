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
