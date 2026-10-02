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

    def restore_missing_from_seed(self, seed_path: Path, columns: tuple) -> int:
        """DB で欠損している項目だけを、シードの値で補う (値がある項目は変更しない)。

        Returns:
            1 項目以上を補った銘柄数
        """
        if not seed_path.exists():
            return 0
        seed = pl.read_parquet(str(seed_path))
        present = [c for c in columns if c in seed.columns]
        if "code" not in seed.columns or not present:
            return 0
        seed = seed.select(["code", *present]).with_columns(pl.col("code").cast(pl.Utf8))
        sets = ", ".join(f"{c} = coalesce(f.{c}, s.{c})" for c in present)
        missing = " OR ".join(f"(f.{c} IS NULL AND s.{c} IS NOT NULL)" for c in present)
        basis = (
            pl.read_parquet(str(seed_path), columns=["code", "updated_at"])
            .with_columns(
                pl.col("code").cast(pl.Utf8),
                pl.col("updated_at").cast(pl.Utf8).str.slice(0, 16).alias("basis"),
            )
            .select(["code", "basis"])
            if "updated_at" in pl.read_parquet_schema(str(seed_path))
            else None
        )
        with self.duck_repo.client.get_connection() as conn:
            conn.register("_seed_restore", seed.to_arrow())
            rows = conn.execute(
                f"UPDATE fundamentals AS f SET {sets} FROM _seed_restore AS s "
                f"WHERE f.code = s.code AND ({missing}) RETURNING f.code"
            ).fetchall()
            conn.unregister("_seed_restore")
            # シード由来の値 (出所の書類が無い) には、シードの作成日時を基準日として記録する
            # (株式分割の補正で、分割前の値かどうかを判定するため)
            if basis is not None:
                conn.register("_seed_basis", basis.to_arrow())
                conn.execute(
                    "UPDATE fundamentals AS f SET "
                    "submitted_at = coalesce(f.submitted_at, s.basis), "
                    "bs_submitted_at = coalesce(f.bs_submitted_at, s.basis) "
                    "FROM _seed_basis AS s WHERE f.code = s.code "
                    "AND (f.submitted_at IS NULL OR f.bs_submitted_at IS NULL)"
                )
                conn.unregister("_seed_basis")
        return len(rows)

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
