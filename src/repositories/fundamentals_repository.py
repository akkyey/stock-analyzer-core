from logging import getLogger
from typing import Any, Optional

import polars as pl

from src.repositories.duck_repository import DuckDBRepository


class FundamentalsRepository:
    """業績データのリポジトリ (v6.1.0 DuckDB 一本化)"""

    def __init__(self, duck_repo: Optional[DuckDBRepository] = None) -> None:
        self.logger = getLogger(__name__)
        self.duck_repo = duck_repo or DuckDBRepository()

    def upsert(self, data_list: list[dict[str, Any]]) -> None:
        """業績データの登録・更新を行う。"""
        if not data_list:
            return

        df = pl.from_dicts(data_list)
        self.duck_repo.save_fundamentals(df)
        self.logger.info(f"Upserted {len(data_list)} fundamentals records to DuckDB.")

    def get_all_pl(self) -> pl.DataFrame:
        """全件を Polars DataFrame として取得する（銘柄情報と結合）。"""
        query = """
            SELECT 
                s.code,
                s.name,
                s.sector,
                f.* EXCLUDE(code)
            FROM stocks s
            LEFT JOIN fundamentals f ON s.code = f.code
        """
        try:
            with self.duck_repo.client.get_connection() as conn:
                return conn.execute(query).pl()
        except Exception as e:
            self.logger.error(f"Error fetching all fundamentals: {e}")
            return pl.DataFrame()

    def get_count(self) -> int:
        """登録されている財務データ件数を取得する。"""
        query = "SELECT count(*) FROM fundamentals"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query).fetchone()
            return int(res[0]) if res else 0

    def exists(self, code: str) -> bool:
        """指定された銘柄の財務データが存在するか確認する。"""
        query = "SELECT 1 FROM fundamentals WHERE code = ? LIMIT 1"
        with self.duck_repo.client.get_connection() as conn:
            return conn.execute(query, [code]).fetchone() is not None
