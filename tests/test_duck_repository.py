import pytest
import polars as pl
from src.repositories.duck_repository import DuckDBRepository

def test_save_and_load_stocks(db_conn):
    """銘柄情報の保存と読み込みのテスト (DuckDBRepository 直接)"""
    repo = DuckDBRepository()
    df = pl.DataFrame({
        "code": ["7203", "9984"],
        "name": ["トヨタ", "SBG"]
    })
    repo.save_stocks(df)
    
    loaded = repo.load_stocks()
    assert len(loaded) >= 2
    # コードでソートして確認するか、含まれているかを確認
    names = loaded["name"].to_list()
    assert "トヨタ" in names
    assert "SBG" in names

def test_save_and_load_metrics(db_conn):
    """日次指標の保存と読み込みのテスト"""
    repo = DuckDBRepository()
    df = pl.DataFrame({
        "code": ["7203"],
        "entry_date": ["2024-01-01"],
        "price": [2500.0]
    })
    repo.save_metrics(df)
    
    # 十分大きな日数を指定して過去データを取得できるようにする
    loaded = repo.load_metrics(["7203"], days=1000)
    assert len(loaded) == 1
    assert loaded["price"][0] == 2500.0

def test_alerts_management(db_conn):
    """アラートの保存とステータス更新のテスト"""
    repo = DuckDBRepository()
    # アラート保存
    repo.save_alert("7203", "Price Spike", "Target price reached")
    
    # 未処理アラート取得
    unprocessed = repo.get_unprocessed_alerts()
    assert len(unprocessed) >= 1
    # 先頭行の 'id' カラムの値をスカラーとして取得
    alert_id = int(unprocessed[0, "id"])
    
    # 処理済みにマーク
    repo.mark_alert_processed(alert_id)
    
    # 再度取得 -> 0件のはず
    assert len(repo.get_unprocessed_alerts()) == 0

def test_save_empty_df(db_conn):
    """空の DataFrame を渡した場合にエラーにならずに戻るかテスト"""
    repo = DuckDBRepository()
    repo.save_stocks(pl.DataFrame())
    repo.save_metrics(pl.DataFrame())
    repo.save_fundamentals(pl.DataFrame())
    repo.save_analysis_results(pl.DataFrame())
    # エラーが発生しなければ OK
