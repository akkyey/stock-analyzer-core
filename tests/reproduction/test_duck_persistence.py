import os

import polars as pl
import pytest

from src.database.duck_client import DuckDBClient
from src.repositories.duck_repository import DuckDBRepository


@pytest.fixture
def repo():
    # テスト用の一時 DB を使用
    db_path = "test_persistence.duckdb"
    if os.path.exists(db_path):
        os.remove(db_path)
    client = DuckDBClient(db_path=db_path)
    repo = DuckDBRepository(client=client)
    yield repo
    if os.path.exists(db_path):
        os.remove(db_path)


def test_upsert_hardening_alias_resolution(repo):
    """[Hardening Test] エイリアス強制により Binder Error が解消されているか検証。"""

    # 1. stocks テーブルへの UPSERT (code を衝突させる)
    df_stocks = pl.DataFrame(
        {
            "code": ["7203", "9984"],
            "name": ["Toyota", "Softbank"],
            "market": ["Prime", "Prime"],
        }
    )

    # 初回 INSERT
    repo.save_stocks(df_stocks)

    # UPDATE 発生 (name を変更)
    df_stocks_update = pl.DataFrame({"code": ["7203"], "name": ["Toyota Motor"]})
    repo.save_stocks(df_stocks_update)

    # 検証
    with repo.client.get_connection() as conn:
        res = conn.execute("SELECT name FROM stocks WHERE code = '7203'").fetchone()
        assert res[0] == "Toyota Motor"


def test_daily_metrics_multi_key_upsert(repo):
    """[Hardening Test] 複合キー(code, entry_date)の UPSERT 堅牢性検証。"""

    df_metrics = pl.DataFrame(
        {
            "code": ["7203", "7203"],
            "entry_date": ["2026-01-01", "2026-01-02"],
            "price": [2000.0, 2100.0],
            "rsi_14": [45.0, 55.0],
        }
    )

    repo.save_metrics(df_metrics)

    # 同一日のデータを更新
    df_metrics_update = pl.DataFrame(
        {"code": ["7203"], "entry_date": ["2026-01-01"], "price": [2050.0]}
    )
    repo.save_metrics(df_metrics_update)

    with repo.client.get_connection() as conn:
        res = conn.execute(
            "SELECT price, rsi_14 FROM daily_metrics WHERE code = '7203' AND entry_date = '2026-01-01'"
        ).fetchone()
        assert res[0] == 2050.0
        assert res[1] == 45.0  # 更新対象外のカラムが保持されていること
