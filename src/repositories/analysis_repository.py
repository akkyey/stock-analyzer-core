from datetime import timedelta
from logging import getLogger
from typing import Any, Optional

import polars as pl

from src.repositories.duck_repository import DuckDBRepository
from src.utils import get_current_time


class AnalysisRepository:
    """分析結果のリポジトリ (v6.1.0 DuckDB 一本化)"""

    def __init__(self, duck_repo: Optional[DuckDBRepository] = None) -> None:
        """リポジトリを初期化する。"""
        self.logger = getLogger(__name__)
        self.duck_repo = duck_repo or DuckDBRepository()

    def save(self, record: dict[str, Any]) -> None:
        """分析結果を DuckDB に保存する。"""
        if not record:
            return

        # 銘柄マスタへの参照を保証するため、必要ならコードを正規化
        df = pl.from_dicts([record])
        self.duck_repo.save_analysis_results(df)
        self.logger.debug(f"Saved analysis record to DuckDB for {record.get('code')}")

    def get_cache(
        self, code: str, row_hash: str, strategy: str
    ) -> dict[str, Any] | None:
        """AI分析結果のキャッシュを取得する。

        銘柄コード、入力データのハッシュ(row_hash)、および戦略名がすべて一致する最新のレコードを返す。
        """
        query = """
            SELECT * FROM analysis_results 
            WHERE code = ? AND row_hash = ? AND strategy_name = ?
            ORDER BY analyzed_at DESC LIMIT 1
        """
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query, [code, row_hash, strategy]).fetchone()
            if res:
                cols = [desc[0] for desc in conn.description]
                return dict(zip(cols, res, strict=False))
        return None

    def get_smart_cache(
        self, code: str, strategy: str, validity_days: int
    ) -> dict[str, Any] | None:
        """指定期間内の最新かつ有効な AI分析結果を取得する。"""
        threshold_date = get_current_time() - timedelta(days=validity_days)
        query = """
            SELECT * FROM analysis_results 
            WHERE code = ? AND strategy_name = ? AND analyzed_at >= ?
            ORDER BY analyzed_at DESC LIMIT 1
        """
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query, [code, strategy, threshold_date]).fetchone()
            if res:
                cols = [desc[0] for desc in conn.description]
                return dict(zip(cols, res, strict=False))
        return None

    def clear(
        self, strategy_name: str | None = None, date_str: str | None = None
    ) -> int:
        """分析結果をクリアする。"""
        query = "DELETE FROM analysis_results WHERE 1=1"
        params = []
        if strategy_name:
            query += " AND strategy_name = ?"
            params.append(strategy_name)
        if date_str:
            query += " AND CAST(analyzed_at AS DATE) = ?"
            params.append(date_str)

        try:
            with self.duck_repo.client.get_connection() as conn:
                res = conn.execute(query + " RETURNING *", params).fetchall()
                count = len(res)
                self.logger.info(f"Cleared {count} analysis results from DuckDB.")
                return count
        except Exception as e:
            self.logger.error(f"Error clearing analysis results: {e}")
            return 0

    def get_top_results(
        self, strategy_name: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """戦略ごとのスコア上位銘柄を取得する。"""
        query = """
            SELECT code, quant_score 
            FROM analysis_results 
            WHERE strategy_name = ? 
            ORDER BY quant_score DESC 
            LIMIT ?
        """
        try:
            with self.duck_repo.client.get_connection() as conn:
                res = conn.execute(query, [strategy_name, limit]).fetchall()
                # code, quant_score の辞書リストに変換
                return [
                    {"code": row[0], "quant_score": row[1], "strategy": strategy_name}
                    for row in res
                ]
        except Exception as e:
            self.logger.error(f"Error fetching top results for {strategy_name}: {e}")
            return []

    def get_count(self) -> int:
        """分析結果の総数を取得する。"""
        try:
            with self.duck_repo.client.get_connection() as conn:
                res = conn.execute("SELECT count(*) FROM analysis_results").fetchone()
                return res[0] if res else 0
        except Exception as e:
            self.logger.error(f"Error fetching analysis count: {e}")
            return 0

    def delete_by_codes(
        self,
        codes: list[str],
        before_date: Any | None = None,
        exclude_mock: bool = False,
    ) -> int:
        """指定された銘柄コードの分析結果を削除する。

        Optional:
        - before_date: この日付より前のレコードのみ削除
        - exclude_mock: True の場合、ai_reason に '[MOCK]' を含むレコードを削除対象から除外する
        """
        if not codes:
            return 0

        query = "DELETE FROM analysis_results WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))"
        params = [codes]

        if before_date:
            query += " AND analyzed_at < ?"
            params.append(before_date)

        if exclude_mock:
            query += " AND ai_reason NOT LIKE '%[MOCK]%'"

        try:
            with self.duck_repo.client.get_connection() as conn:
                res = conn.execute(query + " RETURNING *", params).fetchall()
                count = len(res)
                return count
        except Exception as e:
            self.logger.error(f"Error in delete_by_codes: {e}")
            return 0
