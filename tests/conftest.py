import pytest
import os
import duckdb

# テスト実行前に DUCKDB_MEMORY_LIMIT を設定
os.environ["DUCKDB_MEMORY_LIMIT"] = "1GB"

@pytest.fixture(scope="session", autouse=True)
def duckdb_test_setup():
    """
    テストセッション開始時の DuckDB セットアップ。
    DuckDBClient を :memory: モードで強制初期化する。
    """
    from src.database.duck_client import DuckDBClient
    
    # シングルトンの初期化（パスを :memory: に固定）
    client = DuckDBClient(db_path=":memory:")
    
    # スキーマの構築
    from src.repositories.duck_repository import DuckDBRepository
    repo = DuckDBRepository()
    repo.ensure_schema()
    
    yield client

@pytest.fixture(scope="session")
def db_conn(duckdb_test_setup):
    """
    セッション全体で同一の DuckDB 接続オブジェクトを共有するためのフィクスチャ。
    DuckDB の :memory: 仕様（接続ごとに空間が分かれる）を回避する。
    """
    conn = duckdb_test_setup.get_connection()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_database(db_conn):
    """
    各テスト実行前にデータをクリーンアップする（テーブル構造は維持）。
    """
    tables = [
        "stocks", 
        "daily_metrics", 
        "analysis_results", 
        "fundamentals", 
        "rank_history",
        "sentinel_alerts"
    ]
    for table in tables:
        try:
            db_conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    yield
