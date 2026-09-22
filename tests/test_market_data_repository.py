import pytest
import polars as pl
from datetime import datetime, timedelta
from src.repositories.market_data_repository import MarketDataRepository
from src.repositories.stock_repository import StockRepository
from src.repositories.duck_repository import DuckDBRepository

def test_upsert_and_get_status(db_conn):
    """市況データの登録と収集ステータス取得のテスト"""
    repo = MarketDataRepository()
    date_str = datetime.now().strftime("%Y-%m-%d")
    data = [
        {"code": "1301", "entry_date": date_str, "price": 3000.0, "fetch_status": "success"},
        {"code": "1302", "entry_date": date_str, "price": 2000.0, "fetch_status": "failed"}
    ]
    repo.upsert(data)

    status = repo.get_status(date_str)
    assert "1301" in status
    assert "1302" not in status
    assert len(status) == 1

def test_get_batch_pl_with_joins(db_conn):
    """JOIN を含む一括データ取得のテスト（stocks, fundamentals との結合）"""
    market_repo = MarketDataRepository()
    stock_repo = StockRepository()
    duck_repo = DuckDBRepository()

    # 1. 前提データの準備 (銘柄マスタ)
    stock_repo.upsert([
        {"code": "7203", "name": "トヨタ自動車", "sector": "輸送用機器"}
    ])

    # 2. 前提データの準備 (ファンダメンタルズ)
    # 明示的にコネクションを取得して実行し、コミットを確認
    with duck_repo.client.get_connection() as conn:
        conn.execute("DELETE FROM fundamentals WHERE code = '7203'")
        conn.execute("""
            INSERT INTO fundamentals (code, per, pbr, updated_at)
            VALUES ('7203', 10.5, 1.2, now())
        """)

    # 3. 市況データの登録
    date_str = datetime.now().strftime("%Y-%m-%d")
    market_repo.upsert([
        {"code": "7203", "entry_date": date_str, "price": 3500.0, "fetch_status": "success"}
    ])

    # 4. 実行と検証
    res_pl = market_repo.get_batch_pl(["7203"])
    
    assert not res_pl.is_empty()
    row = res_pl.row(0, named=True)
    assert row["code"] == "7203"
    assert row["name"] == "トヨタ自動車"
    # per は daily_metrics (m) にもある可能性があるので、明確に JOIN 結果を確認
    # MarketDataRepository のクエリは f.* EXCLUDE(code, updated_at)
    assert row["per"] == 10.5
    assert row["price"] == 3500.0

def test_get_all_history_pl(db_conn):
    """履歴データ取得のテスト（最新日付を使用してフィルタリングを回避）"""
    repo = MarketDataRepository()
    today = datetime.now()
    d1 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = today.strftime("%Y-%m-%d")

    repo.upsert([
        {"code": "1301", "entry_date": d1, "price": 100.0, "trading_value": 1000},
        {"code": "1301", "entry_date": d2, "price": 110.0, "trading_value": 1100}
    ])

    # 3ヶ月分で取得すれば、昨日・今日のデータは必ず入る
    history = repo.get_all_history_pl(months=3)
    
    # 他のテストの影響を受ける可能性があるので、コードでフィルタリング
    history_1301 = history.filter(pl.col("code") == "1301")
    assert len(history_1301) >= 2
    assert "Open" in history.columns
    assert "Close" in history.columns

def test_get_id(db_conn):
    """ID 生成の便宜的実装のテスト"""
    repo = MarketDataRepository()
    assert repo.get_id("7203", "2024-04-20") == "7203_2024-04-20"

from unittest.mock import patch

def test_market_data_exception_handling(db_conn):
    """例外ハンドリングパスのテスト (カバレッジ向上のため)"""
    repo = MarketDataRepository()
    
    # get_batch_pl でエラーが発生した場合
    with patch("src.database.duck_client.DuckDBClient.get_connection", side_effect=Exception("DB Error")):
        res = repo.get_batch_pl(["7203"])
        assert res.is_empty()
    
    # get_all_history_pl でエラーが発生した場合
    with patch("src.repositories.duck_repository.DuckDBRepository.load_metrics", side_effect=Exception("Load Error")):
        # 空の DataFrame を返すようにフォールバックされることを確認
        res = repo.get_all_history_pl(months=3)
        assert len(res) == 0
