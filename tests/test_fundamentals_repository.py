import polars as pl
import pytest

from src.repositories.fundamentals_repository import FundamentalsRepository
from src.repositories.stock_repository import StockRepository


def test_upsert_and_count(db_conn):
    """財務データの登録と件数取得のテスト"""
    repo = FundamentalsRepository()
    data = [
        {"code": "7203", "roe": 12.5, "pbr": 1.1, "market_cap": 400000},
        {"code": "9984", "roe": 15.0, "pbr": 2.5, "market_cap": 1000000},
    ]
    repo.upsert(data)
    assert repo.get_count() == 2


def test_get_all_pl_join(db_conn):
    """銘柄マスタとの JOIN を含めた全件取得のテスト"""
    stock_repo = StockRepository()
    funda_repo = FundamentalsRepository()

    # 銘柄マスタの準備
    stock_repo.upsert(
        [
            {"code": "7203", "name": "トヨタ", "sector": "輸送用機器"},
            {"code": "9984", "name": "ソフトバンクG", "sector": "情報・通信業"},
        ]
    )

    # 財務データの準備
    funda_repo.upsert([{"code": "7203", "roe": 12.5, "pbr": 1.1}])

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


def test_growth_columns_exist_and_are_persisted(db_conn):
    """成長率の列が fundamentals に存在し、保存・取得できる (旧スキーマでは捨てられていた)"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "7203", "roe": 12.5, "sales_growth": 4.2, "profit_growth": -3.1}])
    with repo.duck_repo.client.get_connection() as conn:
        row = conn.execute(
            "SELECT sales_growth, profit_growth FROM fundamentals WHERE code='7203'"
        ).fetchall()[0]
    assert row == (4.2, -3.1)


def test_backfill_growth_from_seed(db_conn, tmp_path):
    """既存 DB (成長率が全て空) にシードの成長率だけを補い、他の指標は変更しない"""
    repo = FundamentalsRepository()
    repo.upsert([
        {"code": "7203", "roe": 12.5, "pbr": 1.1},
        {"code": "9984", "roe": 15.0},
    ])
    seed = tmp_path / "seed.parquet"
    pl.DataFrame({
        "code": ["7203", "9984", "6758"],
        "sales_growth": [4.2, None, None],
        "profit_growth": [None, -3.1, None],
        "roe": [999.0, 999.0, 999.0],  # シードの他の列は反映されないこと
    }).write_parquet(seed)

    assert repo.backfill_growth_from_seed(seed) == 2  # 成長率を持つ 2 銘柄のみ
    with repo.duck_repo.client.get_connection() as conn:
        rows = dict(
            (r[0], r[1:])
            for r in conn.execute(
                "SELECT code, sales_growth, profit_growth, roe FROM fundamentals ORDER BY code"
            ).fetchall()
        )
    assert rows["7203"] == (4.2, None, 12.5)  # roe は元の値のまま
    assert rows["9984"] == (None, -3.1, 15.0)

    # 2 回目は成長率が登録済みのため何もしない (更新済みの値を上書きしない)
    assert repo.backfill_growth_from_seed(seed) == 0

    # シード無しは 0
    assert repo.backfill_growth_from_seed(tmp_path / "none.parquet") == 0
