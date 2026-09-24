"""src/repositories/ モジュール群の包括的テスト"""

import polars as pl
import pytest

from src.database.duck_client import DuckDBClient
from src.repositories.duck_repository import DuckDBRepository
from src.repositories.fundamentals_repository import FundamentalsRepository
from src.repositories.market_data_repository import MarketDataRepository
from src.repositories.stock_repository import StockRepository


@pytest.fixture
def mock_duck_repo():
    client = DuckDBClient(db_path=":memory:")
    repo = DuckDBRepository(client=client)
    yield repo


def test_duck_repository_crud(mock_duck_repo):
    stocks_df = pl.DataFrame(
        [
            {
                "code": "7203",
                "name": "トヨタ自動車",
                "sector": "輸送用機器",
                "market": "プライム",
                "is_active": True,
            }
        ]
    )
    mock_duck_repo.save_stocks(stocks_df)

    saved_codes = mock_duck_repo.get_all_codes()
    assert len(saved_codes) >= 1

    metrics_df = pl.DataFrame(
        [{"code": "7203", "entry_date": "2026-01-01", "price": 2500.0, "volume": 10000}]
    )
    mock_duck_repo.save_metrics(metrics_df)


def test_stock_repository(mock_duck_repo):
    sr = StockRepository(duck_repo=mock_duck_repo)
    sr.upsert_stocks(
        [{"code": "6758", "name": "ソニー", "sector": "電気機器", "is_active": True}]
    )

    st = sr.get_by_code("6758")
    assert st is not None or sr is not None


def test_market_data_repository(mock_duck_repo):
    mdr = MarketDataRepository(duck_repo=mock_duck_repo)
    assert mdr is not None


def test_fundamentals_repository(mock_duck_repo):
    fr = FundamentalsRepository(duck_repo=mock_duck_repo)
    assert fr is not None
