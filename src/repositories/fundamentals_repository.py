import hashlib
from logging import getLogger
from pathlib import Path
from typing import Any, Optional

import polars as pl

from src.repositories.duck_repository import DuckDBRepository

# 損益・1 株当たり指標 (決算期 period_end で新旧を判定) と、貸借対照表の項目 (bs_period_end で判定)
ANNUAL_PL_FIELDS = (
    "sales",
    "operating_income",
    "net_profit",
    "prev_net_profit",
    "roe",
    "eps",
    "dps",
    "operating_margin",
)
BALANCE_SHEET_FIELDS = ("total_assets", "net_assets", "equity_ratio", "shares_outstanding", "bps")


def seed_fingerprint(seed_path: Path) -> str:
    """シードの内容から作る識別子 (実行済みの印に使う。シードを作り直すと変わる)。"""
    return hashlib.sha1(seed_path.read_bytes()).hexdigest()[:12]


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

    def get_provenance(self) -> dict[str, dict[str, Any]]:
        """銘柄ごとの財務値の出所 {"period_end", "bs_period_end"} と現在の売上 (EDINET 取り込みの判定用)。

        売上は、取り込みで売上が変わったかを判定し、変わった場合に旧い売上との比較の売上高成長率を
        空にするために使う。
        """
        with self.duck_repo.client.get_connection() as conn:
            rows = conn.execute(
                "SELECT code, period_end, bs_period_end, sales FROM fundamentals"
            ).fetchall()
        return {r[0]: {"period_end": r[1], "bs_period_end": r[2], "sales": r[3]} for r in rows}

    def refresh_from_seed(self, seed_path: Path) -> int:
        """同梱シードの値で、決算期の古い財務値を更新する (DB の値の方が新しい項目は変更しない)。

        シードを EDINET から作り直したときに、既存の DB に反映するための処理。EDINET 取り込みと同じ規則で、
        銘柄ごとに、損益・1 株当たり指標は period_end、貸借対照表の項目は bs_period_end を比べ、シードの期が
        DB より新しいか、DB の期が不明 (旧シード由来) の項目と、同じ期でシードの提出日時が DB 以降の項目だけを
        更新する (同じ書類を、改良したパーサーで読み直した値を届けるため。DB の方が新しい訂正報告書のものは
        変更しない)。シードの値が空の項目は、
        DB の値を消さない。ただし、売上を更新した銘柄の売上高成長率は、旧い売上との比較の値なので、
        シードの値 (空を含む) に置き換える。出所の書類が無い行 (シードの period_end が空) は更新しない。

        Returns:
            更新した銘柄数 (出所の列が無い旧形式のシードは 0)
        """
        if not seed_path.exists():
            return 0
        seed = pl.read_parquet(str(seed_path))
        provenance = ("period_end", "submitted_at", "bs_period_end", "bs_submitted_at")
        if "code" not in seed.columns or any(c not in seed.columns for c in provenance):
            return 0
        seed = seed.with_columns(pl.col("code").cast(pl.Utf8))
        with self.duck_repo.client.get_connection() as conn:
            table_cols = {r[0] for r in conn.execute("DESCRIBE fundamentals").fetchall()}
            stored = {
                r[0]: r[1:]
                for r in conn.execute(
                    "SELECT code, period_end, submitted_at::VARCHAR, bs_period_end, bs_submitted_at::VARCHAR "
                    "FROM fundamentals"
                ).fetchall()
            }
        records = [
            {"code": row["code"], **rec}
            for row in seed.iter_rows(named=True)
            if row["code"] in stored
            if (rec := self._seed_update(row, stored[row["code"]], table_cols))
        ]

        # 項目の組み合わせごとにまとめて UPSERT する (含まれない項目は更新されない)
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for r in records:
            groups.setdefault(tuple(sorted(r)), []).append(r)
        for rows in groups.values():
            self.upsert(rows)
        return len(records)

    @staticmethod
    def _seed_update(row: dict[str, Any], stored: tuple, table_cols: set) -> dict[str, Any]:
        """シードの 1 行から、DB へ書き込む項目を決める (損益と貸借対照表を、別々の決算期で判定する)。

        stored は (損益の決算期, 損益の提出日時, 貸借対照表の決算期, 貸借対照表の提出日時)。
        """

        def newer(seed_period: Any, seed_submitted: Any, db_period: Any, db_submitted: Any) -> bool:
            """シードの値が、DB の値より新しいか、同じ書類 (または、より新しい訂正) のもの。

            同じ決算期でも、提出日時が DB 以降なら反映する (同じ書類を、改良したパーサーで読み直した値を、
            すでに同じ期の値を持つ DB に届けるため)。DB の方が新しい訂正報告書のものなら反映しない。
            """
            if not seed_period:
                return False
            if not db_period or str(seed_period) > str(db_period):
                return True
            if str(seed_period) < str(db_period) or not seed_submitted:
                return False
            return not db_submitted or str(seed_submitted)[:16] >= str(db_submitted)[:16]

        def values(fields: tuple) -> dict[str, Any]:
            return {f: row[f] for f in fields if row.get(f) is not None and f in table_cols}

        rec: dict[str, Any] = {}
        if newer(row["period_end"], row["submitted_at"], stored[0], stored[1]):
            rec.update(values(ANNUAL_PL_FIELDS))
            rec.update(period_end=row["period_end"], submitted_at=row["submitted_at"])
            if row.get("sales") is not None and "sales_growth" in table_cols:
                rec["sales_growth"] = row.get("sales_growth")
        if newer(row["bs_period_end"], row["bs_submitted_at"], stored[2], stored[3]):
            rec.update(values(BALANCE_SHEET_FIELDS))
            rec.update(bs_period_end=row["bs_period_end"], bs_submitted_at=row["bs_submitted_at"])
        return rec

    def restore_missing_from_seed(self, seed_path: Path, columns: tuple) -> int:
        """DB で欠損している項目だけを、シードの値で補う (値がある項目は変更しない)。

        Returns:
            1 項目以上を補った銘柄数
        """
        if not seed_path.exists():
            return 0
        raw = pl.read_parquet(str(seed_path))
        with self.duck_repo.client.get_connection() as conn:
            table_cols = {r[0] for r in conn.execute("DESCRIBE fundamentals").fetchall()}
            present = [c for c in columns if c in raw.columns and c in table_cols]
            if "code" not in raw.columns or not present:
                return 0
            raw = raw.with_columns(pl.col("code").cast(pl.Utf8))
            seed = raw.select(["code", *present])
            sets = ", ".join(f"{c} = coalesce(f.{c}, s.{c})" for c in present)
            missing = " OR ".join(f"(f.{c} IS NULL AND s.{c} IS NOT NULL)" for c in present)
            basis = (
                raw.select(
                    "code", pl.col("updated_at").cast(pl.Utf8).str.slice(0, 16).alias("basis")
                )
                if "updated_at" in raw.columns
                else None
            )
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
