from logging import getLogger
from typing import Any, Optional

import polars as pl
from src.repositories.duck_repository import DuckDBRepository

class StockRepository:
    """銘柄マスタのリポジトリ (v6.1.0 DuckDB 一本化)"""

    def __init__(self, duck_repo: Optional[DuckDBRepository] = None) -> None:
        """リポジトリを初期化する。"""
        self.logger = getLogger(__name__)
        self.duck_repo = duck_repo or DuckDBRepository()

    def find_by_code(self, code: str) -> dict[str, Any] | None:
        """銘柄コードで検索する。"""
        return self.get_by_code(code)

    def get_all_active_codes(self) -> list[str]:
        """アクティブな銘柄コードリストを取得する。"""
        query = "SELECT code FROM stocks WHERE is_active = TRUE"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query).fetchall()
            return [row[0] for row in res]

    def upsert_stocks(self, stocks: list[dict[str, Any]]) -> None:
        """銘柄マスタを一括更新する。"""
        if not stocks:
            return

        df = pl.from_dicts(stocks)
        self.duck_repo.save_stocks(df)
        self.logger.info(f"Upserted {len(stocks)} stocks to DuckDB master.")

    def upsert(self, stocks: list[dict[str, Any]]) -> None:
        """upsert_stocks のエイリアス。"""
        self.upsert_stocks(stocks)

    def get_by_code(self, code: str) -> dict[str, Any] | None:
        """銘柄情報を取得する。"""
        query = "SELECT * FROM stocks WHERE code = ?"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query, [code]).fetchone()
            if res:
                cols = [desc[0] for desc in conn.description]
                return dict(zip(cols, res))
        return None

    def exists(self, code: str) -> bool:
        """銘柄が存在するか確認する。"""
        query = "SELECT 1 FROM stocks WHERE code = ? LIMIT 1"
        with self.duck_repo.client.get_connection() as conn:
            return conn.execute(query, [code]).fetchone() is not None

    def get_all_codes(self) -> list[str]:
        """全銘柄コードを取得する。"""
        query = "SELECT code FROM stocks"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query).fetchall()
            return [row[0] for row in res]

    def get_count(self) -> int:
        """登録されている銘柄数を取得する。"""
        query = "SELECT count(*) FROM stocks"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query).fetchone()
            return int(res[0]) if res else 0

    def reset_fail_counts(self, codes: list[str]) -> None:
        """指定された銘柄の失敗カウントを 0 にリセットする。"""
        if not codes:
            return
        query = "UPDATE stocks SET fail_count = 0 WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))"
        with self.duck_repo.client.get_connection() as conn:
            conn.execute(query, [codes])

    def increment_fail_counts(self, codes: list[str]) -> None:
        """指定された銘柄の失敗カウントを 1 加算する。"""
        if not codes:
            return
        query = "UPDATE stocks SET fail_count = fail_count + 1 WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))"
        with self.duck_repo.client.get_connection() as conn:
            conn.execute(query, [codes])

    def suspend_stocks(self, codes: list[str], reason: str) -> None:
        """指定された銘柄を休止状態 (is_active=FALSE) に設定する。"""
        if not codes:
            return
        query = """
            UPDATE stocks 
            SET is_active = FALSE, 
                status = 'suspended', 
                exclusion_reason = ? 
            WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))
        """
        with self.duck_repo.client.get_connection() as conn:
            conn.execute(query, [reason, codes])
