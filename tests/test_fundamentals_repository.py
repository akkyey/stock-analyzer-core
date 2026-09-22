import pytest
import polars as pl
from src.repositories.fundamentals_repository import FundamentalsRepository
from src.repositories.stock_repository import StockRepository

def test_upsert_and_count(db_conn):
    """財務データの登録と件数取得のテスト"""
    repo = FundamentalsRepository()
    data = [
        {"code": "7203", "roe": 12.5, "pbr": 1.1, "market_cap": 400000},
        {"code": "9984", "roe": 15.0, "pbr": 2.5, "market_cap": 1000000}
    ]
    repo.upsert(data)
    assert repo.get_count() == 2

def test_get_all_pl_join(db_conn):
    """銘柄マスタとの JOIN を含めた全件取得のテスト"""
    stock_repo = StockRepository()
    funda_repo = FundamentalsRepository()

    # 銘柄マスタの準備
    stock_repo.upsert([
        {"code": "7203", "name": "トヨタ", "sector": "輸送用機器"},
        {"code": "9984", "name": "ソフトバンクG", "sector": "情報・通信業"}
    ])

    # 財務データの準備
    funda_repo.upsert([
        {"code": "7203", "roe": 12.5, "pbr": 1.1}
    ])

    df = funda_repo.get_all_pl()
    assert len(df) == 2  # stocks に 2件あるので LEFT JOIN で 2件返るはず
    
    # トヨタのデータ確認
    toyota = df.filter(pl.col("code") == "7203")
    assert toyota["name"][0] == "トヨタ"
    assert toyota["roe"][0] == 12.5

    # 財務データがない銘柄の確認
    sb = df.filter(pl.col("code") == "9984")
    assert sb["name"][0] == "ソフトバンクG"
    assert sb["roe"][0] is None

def test_exists(db_conn):
    """存在確認のテスト"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "1301", "roe": 8.0}])
    
    assert repo.exists("1301") is True
    assert repo.exists("9999") is False
