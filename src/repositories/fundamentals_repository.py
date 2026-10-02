from logging import getLogger
from pathlib import Path
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

    GROWTH_COLUMNS = ("sales_growth", "profit_growth", "profit_growth_raw")

    def backfill_growth_from_seed(self, seed_path: Path) -> int:
        """成長率が未登録の既存 DB に、バンドルされたシードの成長率を補う。

        旧スキーマには成長率の列が無く、シード取り込み時に値が捨てられていた。
        既に成長率が 1 件でも登録されている場合は何もしない (EDINET 等で更新済みの値を上書きしない)。
        UPSERT は入力に含まれる列だけを更新するため、他の財務指標は変更されない。

        Returns:
            補った銘柄数 (対象外・シード無しは 0)。
        """
        if not seed_path.exists():
            return 0
        cols = " OR ".join(f"{c} IS NOT NULL" for c in self.GROWTH_COLUMNS)
        with self.duck_repo.client.get_connection() as conn:
            already = conn.execute(
                f"SELECT count(*) FROM fundamentals WHERE {cols}"
            ).fetchall()[0][0]
        if already:
            return 0

        seed = pl.read_parquet(str(seed_path))
        present = [c for c in self.GROWTH_COLUMNS if c in seed.columns]
        if "code" not in seed.columns or not present:
            return 0
        growth = seed.select(["code", *present]).filter(
            pl.any_horizontal([pl.col(c).is_not_null() for c in present])
        )
        if growth.is_empty():
            return 0
        self.duck_repo.save_fundamentals(growth)
        self.logger.info(f"Backfilled growth rates for {growth.height} stocks from seed.")
        return growth.height

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
