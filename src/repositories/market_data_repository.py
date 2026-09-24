from logging import getLogger
from typing import Any, Optional

import pandas as pd
import polars as pl

from src.repositories.duck_repository import DuckDBRepository


class MarketDataRepository:
    """市況データのリポジトリ (v6.1.0 DuckDB 一本化)"""

    def __init__(self, duck_repo: Optional[DuckDBRepository] = None) -> None:
        """リポジトリを初期化する。"""
        self.logger = getLogger(__name__)
        self.duck_repo = duck_repo or DuckDBRepository()

    def upsert(self, data_list: list[dict[str, Any]]) -> None:
        """市況データの一括登録を DuckDB に対して行う。"""
        if not data_list:
            return

        df = pl.from_dicts(data_list)
        self.duck_repo.save_metrics(df)
        self.logger.info(f"Upserted {len(data_list)} market records to DuckDB.")

    def get_status(self, date_str: str) -> set[str]:
        """指定した日付に収集済みの銘柄コードリストを取得する。"""
        query = "SELECT code FROM daily_metrics WHERE entry_date = ? AND fetch_status = 'success'"
        with self.duck_repo.client.get_connection() as conn:
            res = conn.execute(query, [date_str]).fetchall()
            return {row[0] for row in res}

    def get_id(self, code: str, entry_date: str) -> str | None:
        """market_data_id (DuckDB では便宜上の一意識別) を取得する。"""
        # DuckDB では ID 自体より (code, entry_date) が主キー相当
        return f"{code}_{entry_date}"

    def get_batch(self, codes: list[str]) -> pd.DataFrame:
        """指定した銘柄リストの最新市況データを一括取得する (pandas互換)。"""
        pl_df = self.get_batch_pl(codes)
        return pl_df.to_pandas() if not pl_df.is_empty() else pd.DataFrame()

    def get_batch_pl(self, codes: list[str]) -> pl.DataFrame:
        """指定した銘柄リストの最新市況データを一括取得する (Polars)。"""
        if not codes:
            return pl.DataFrame()

        # DuckDB の先進的なクエリ: 各コードの最新日付を JOIN で一気に取得
        query = """
            WITH latest AS (
                SELECT code, MAX(entry_date) as max_date 
                FROM daily_metrics 
                WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))
                GROUP BY code
            )
            SELECT 
                m.* EXCLUDE(per, pbr, roe, dividend_yield, equity_ratio, market_cap, sales, operating_cf),
                s.name,
                s.sector,
                f.* EXCLUDE(code, updated_at),
                f.updated_at as f_updated_at
            FROM daily_metrics m
            JOIN latest l ON m.code = l.code AND m.entry_date = l.max_date
            JOIN stocks s ON m.code = s.code
            LEFT JOIN fundamentals f ON m.code = f.code
        """
        try:
            with self.duck_repo.client.get_connection() as conn:
                return conn.execute(query, [codes]).pl()
        except Exception as e:
            self.logger.error(f"Error fetching batch market data: {e}")
            return pl.DataFrame()

    def get_all_history_pl(self, months: int = 3) -> pl.DataFrame:
        """指定された月数分の全銘柄の履歴データを取得する。"""
        from datetime import datetime, timedelta

        cutoff_date = (datetime.now() - timedelta(days=months * 31)).strftime(
            "%Y-%m-%d"
        )

        query = """
            SELECT 
                code,
                entry_date as Date,
                price as Close,
                volume as Volume,
                trading_value
            FROM daily_metrics 
            WHERE entry_date >= ?
            ORDER BY code, entry_date
        """
        try:
            with self.duck_repo.client.get_connection() as conn:
                df = conn.execute(query, [cutoff_date]).pl()

            if df.is_empty():
                return df

            # OHLC 形式のダミー列を追加（バックテスト/検証用互換性）
            return df.with_columns(
                [
                    pl.lit(0.0).alias("Open"),
                    pl.lit(0.0).alias("High"),
                    pl.lit(0.0).alias("Low"),
                    pl.col("Close").alias("Adj Close"),
                    pl.col("Date")
                    .cast(pl.String)
                    .str.to_datetime("%Y-%m-%d", strict=False),
                ]
            ).select(
                ["code", "Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "trading_value"]
            )
        except Exception as e:
            self.logger.error(f"Error fetching historical records: {e}")
            return pl.DataFrame()

    def get_calendar_dates(self, start_date: str, end_date: str) -> list[Any]:
        """指定期間の営業日リストを DB から取得する。"""
        query = (
            "SELECT date FROM market_calendar WHERE date BETWEEN ? AND ? ORDER BY date"
        )
        try:
            with self.duck_repo.client.get_connection() as conn:
                res = conn.execute(query, [start_date, end_date]).fetchall()
                return [row[0] for row in res]
        except Exception as e:
            self.logger.warning(f"Error fetching calendar from DB: {e}")
            return []

    def upsert_calendar_dates(self, dates: list[Any]) -> None:
        """営業日リストを DB へ永続化する。"""
        if not dates:
            return

        # DuckDB への INSERT OR IGNORE 相当の処理
        query = "INSERT OR IGNORE INTO market_calendar (date) VALUES (?)"
        try:
            with self.duck_repo.client.get_connection() as conn:
                data = [[d] for d in dates]
                conn.executemany(query, data)
            self.logger.info(f"Upserted {len(dates)} calendar dates to DB.")
        except Exception as e:
            self.logger.error(f"Error upserting calendar dates: {e}")
