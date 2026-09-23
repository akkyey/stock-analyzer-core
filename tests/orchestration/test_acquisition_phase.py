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
        assert isinstance(all_data_map["1001"], pd.DataFrame)
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
