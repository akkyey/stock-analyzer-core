"""DuckDB リポジトリ層

DuckDB への具体的なデータ読み書きを担当する。
既存の Peewee Repository に代わり、Polars/Arrow ベースの高速 I/O を提供する。
"""

from logging import getLogger
from typing import List, Optional
import polars as pl
import duckdb

from src.database.duck_client import DuckDBClient
from src.utils import get_current_time

class DuckDBRepository:
    """DuckDB 向けリポジトリ実装 (v6.1.0 Unified)
    
    - [x] **Task 1: DuckDBRepository の基盤化 (Structural Hardening)**
    - [x] `_upsert_dataframe` 共通テンプレートメソッドの実装
    - [x] `save_stocks` のテンプレート移行
    - [x] `save_metrics` のテンプレート移行
    - [x] `save_fundamentals` のテンプレート移行
    - [x] `save_analysis_results` のテンプレート移行
    """

    def __init__(self, client: Optional[DuckDBClient] = None):
        self.logger = getLogger(__name__)
        self.client = client or DuckDBClient()
        self.ensure_schema()

    def _upsert_dataframe(
        self, 
        table_name: str, 
        df: pl.DataFrame, 
        conflict_keys: List[str]
    ) -> None:
        """[v6.3.0] 基盤的な UPSERT ロジック。
        入力 DF のカラム構成に依存せず、テーブル定義に基づいた固定 SQL 生成を強制する。
        """
        if df.is_empty():
            return

        # [v26.8] 硬質なカラム定義 (Schema Locking)
        # 動的な df.columns ではなく、テーブル定義に合わせた投影を行う
        # これにより、上流からカラムが欠落した DF が届いても、DB 側の既存データを Null で破壊するのを防ぐ
        with self.client.get_connection() as conn:
            # ターゲットテーブルの実際のカラム一覧を取得
            table_info = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
            fixed_cols = [row[1] for row in table_info if row[1] != "updated_at"]
            
            # 元の入力 DF に存在するカラムを保持（UPDATE対象を限定するため）
            input_cols = set(df.columns)

            # 入力 DF に存在しないカラムを Null で補完（Repository 側の最終防衛線: INSERT用）
            for col in fixed_cols:
                if col not in df.columns:
                    df = df.with_columns(pl.lit(None).alias(col))
            
            # 保存対象カラムの文字列化
            col_str = ", ".join(fixed_cols)
            # UPDATE 対象は「元の入力 DF に存在したカラム」かつ「conflict_keys でないもの」に限定
            update_cols = [c for c in fixed_cols if c not in conflict_keys and c in input_cols]
            set_exprs = [f"{c} = EXCLUDED.{c}" for c in update_cols]
            set_str = ", ".join(set_exprs)
            
            if "updated_at" in [row[1] for row in table_info]:
                if set_str:
                    set_str += ", updated_at = now()"
                else:
                    set_str = "updated_at = now()"
            
            conflict_target = ", ".join(conflict_keys)
            temp_table = f"_tmp_upsert_{table_name}"

            # Polars DF を temp table として登録（必要なカラムのみを select）
            df_to_save = df.select(fixed_cols)
            conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE {temp_table} AS SELECT * FROM df_to_save")
            
            if set_str:
                conflict_action = f"DO UPDATE SET {set_str}"
            else:
                conflict_action = "DO NOTHING"

            sql = f"""
                INSERT INTO {table_name} AS t ({col_str})
                SELECT {col_str} FROM {temp_table}
                ON CONFLICT ({conflict_target}) {conflict_action}
            """
            try:
                conn.execute(sql)
            except Exception as e:
                self.logger.error(f"❌ Strict UPSERT failed on {table_name}: {e}")
                self.logger.debug(f"Failed SQL: {sql}")
                raise
            finally:
                conn.execute(f"DROP TABLE IF EXISTS {temp_table}")

    def purge_holiday_data(self):
        """非営業日（土日）のレコードを一括削除する (v26.6 Resilience)。"""
        with self.client.get_connection() as conn:
            # DuckDB's dayofweek: 0 (Sunday) to 6 (Saturday)
            res = conn.execute("DELETE FROM daily_metrics WHERE dayofweek(entry_date) IN (0, 6)")
            self.logger.info("Purged weekend records from daily_metrics.")


    def ensure_schema(self):
        """必要なテーブルが存在することを確認する。"""
        with self.client.get_connection() as conn:
            # 銘柄マスタ
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stocks (
                    code VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    sector VARCHAR,
                    sector_17 VARCHAR,
                    market VARCHAR,
                    is_active BOOLEAN DEFAULT TRUE,
                    status VARCHAR DEFAULT 'active',
                    exclusion_reason VARCHAR,
                    excluded_until VARCHAR,
                    fail_count INTEGER DEFAULT 0,
                    edinet_code VARCHAR,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 日次指標 (時系列 : 拡張版 Wide Table)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_metrics (
                    code VARCHAR,
                    entry_date DATE,
                    -- 価格・出来高
                    price DOUBLE,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    volume BIGINT,
                    adj_close DOUBLE,
                    volume_ratio DOUBLE,
                    trading_value DOUBLE,
                    -- テクニカル指標
                    macd_hist DOUBLE,
                    rsi_14 DOUBLE,
                    ma_divergence DOUBLE,
                    volatility DOUBLE,
                    bb_mid DOUBLE,
                    bb_sigma DOUBLE,
                    bb_p1sig DOUBLE,
                    bb_p2sig DOUBLE,
                    bb_m1sig DOUBLE,
                    bb_m2sig DOUBLE,
                    -- 日次ファンダメンタルズ（時系列スナップショット）
                    per DOUBLE,
                    pbr DOUBLE,
                    roe DOUBLE,
                    dividend_yield DOUBLE,
                    equity_ratio DOUBLE,
                    market_cap DOUBLE,
                    operating_cf DOUBLE,
                    sales DOUBLE,
                    sales_growth DOUBLE,
                    profit_growth DOUBLE,
                    profit_growth_raw DOUBLE,
                    -- 分析補助・メタデータ
                    quant_score DOUBLE,
                    trend_score INTEGER,
                    fetch_status VARCHAR,
                    repair_metadata TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, entry_date)
                )
            """)
            # AI分析結果
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analysis_results (
                    code VARCHAR,
                    analyzed_at TIMESTAMP,
                    strategy_name VARCHAR,
                    quant_score DOUBLE,
                    ai_sentiment TEXT,
                    ai_reason TEXT,
                    ai_detail TEXT,
                    ai_risk TEXT,
                    ai_horizon VARCHAR,
                    snapshot_data TEXT,
                    api_call_count INTEGER,
                    row_hash VARCHAR, -- AIキャッシュ整合性に必須
                    score_long DOUBLE,
                    score_short DOUBLE,
                    score_gap DOUBLE,
                    active_style VARCHAR,
                    score_value DOUBLE,
                    score_growth DOUBLE,
                    score_quality DOUBLE,
                    score_trend DOUBLE,
                    score_penalty DOUBLE,
                    audit_version VARCHAR, -- リセット機能に必須
                    PRIMARY KEY (code, analyzed_at)
                )
            """)
            # 順序の作成（存在しない場合）
            conn.execute("CREATE SEQUENCE IF NOT EXISTS alert_id_seq")

            # Sentinel 通知履歴
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_alerts (
                    id INTEGER PRIMARY KEY DEFAULT nextval('alert_id_seq'),
                    code VARCHAR,
                    alert_type VARCHAR,
                    alert_message TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_processed BOOLEAN DEFAULT FALSE,
                    processed_at TIMESTAMP
                )
            """)

            # ファンダメンタルズ (最新ステータス)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fundamentals (
                    code VARCHAR PRIMARY KEY,
                    per DOUBLE,
                    pbr DOUBLE,
                    roe DOUBLE,
                    dividend_yield DOUBLE,
                    equity_ratio DOUBLE,
                    payout_ratio DOUBLE,
                    market_cap DOUBLE,
                    sales DOUBLE,
                    operating_income DOUBLE,
                    operating_margin DOUBLE,
                    operating_cf DOUBLE,
                    free_cf DOUBLE,
                    net_profit DOUBLE,
                    prev_net_profit DOUBLE,
                    shares_outstanding DOUBLE,
                    eps DOUBLE,
                    bps DOUBLE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # [v28.5] Schema Evolution
            conn.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS prev_net_profit DOUBLE")
            conn.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS shares_outstanding DOUBLE")
            conn.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS operating_income DOUBLE")

            # 順位履歴
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rank_history (
                    code VARCHAR,
                    strategy_name VARCHAR,
                    rank INTEGER,
                    score DOUBLE,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # [v27.2] 市場カレンダー・キャッシュ
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_calendar (
                    date DATE PRIMARY KEY
                )
            """)

    def get_all_codes(self) -> List[str]:
        """登録されている全銘柄コードを取得する。"""
        with self.client.get_connection() as conn:
            res = conn.execute("SELECT code FROM stocks WHERE is_active = TRUE").fetchall()
            return [str(r[0]) for r in res]
            
    def get_metrics_count(self) -> int:
        """蓄積されている時系列データの総件数を取得する。"""
        with self.client.get_connection() as conn:
            res = conn.execute("SELECT count(*) FROM daily_metrics").fetchone()
            return int(res[0]) if res else 0

    def save_stocks(self, df: pl.DataFrame):
        """銘柄マスタを保存・更新する。"""
        self._upsert_dataframe("stocks", df, ["code"])
        
    def save_metrics(self, df: pl.DataFrame):
        """日次指標データを保存・更新する。"""
        if df.is_empty():
            return
        if 'code' not in df.columns or 'entry_date' not in df.columns:
            raise ValueError("UPSERT to daily_metrics requires 'code' and 'entry_date'.")
        self._upsert_dataframe("daily_metrics", df, ["code", "entry_date"])
            
    def save_fundamentals(self, df: pl.DataFrame):
        """財務データを保存・更新する。"""
        if df.is_empty():
            return
        if 'code' not in df.columns:
            raise ValueError("UPSERT to fundamentals requires 'code'.")
        self._upsert_dataframe("fundamentals", df, ["code"])

    def load_metrics(self, codes: Optional[List[str]] = None, days: int = 365) -> pl.DataFrame:
        """指定された期間・銘柄のデータを取得する。"""
        with self.client.get_connection() as conn:
            # DuckDB で interval へのパラメータ指定は 'interval 1 day * ?' の形式が安全
            query = "SELECT * FROM daily_metrics WHERE entry_date >= current_date - (interval '1 day' * ?)"
            params = [days]
            
            if codes:
                query += " AND code IN (" + ",".join(["?" for _ in codes]) + ")"
                params.extend(codes)
            
            return conn.execute(query, params).pl()

    def load_stocks(self) -> pl.DataFrame:
        """全銘柄マスタを Polars DataFrame として取得する"""
        with self.client.get_connection() as conn:
            return conn.execute("SELECT * FROM stocks").pl()

    def save_analysis_results(self, df: pl.DataFrame):
        """分析結果を保存する。"""
        if df.is_empty():
            return
        if 'code' not in df.columns or 'analyzed_at' not in df.columns:
            raise ValueError("UPSERT requires 'code' and 'analyzed_at'.")
        self._upsert_dataframe("analysis_results", df, ["code", "analyzed_at"])

    def save_alert(self, code: str, alert_type: str, message: str):
        """通知を保存する"""
        with self.client.get_connection() as conn:
            conn.execute("""
                INSERT INTO sentinel_alerts (code, alert_type, alert_message)
                VALUES (?, ?, ?)
            """, [code, alert_type, message])

    def get_unprocessed_alerts(self) -> pl.DataFrame:
        """未処理の通知を取得する"""
        with self.client.get_connection() as conn:
            return conn.execute("SELECT * FROM sentinel_alerts WHERE is_processed = FALSE").pl()

    def mark_alert_processed(self, alert_id: int):
        """通知を処理済みとする"""
        with self.client.get_connection() as conn:
            conn.execute("""
                UPDATE sentinel_alerts 
                SET is_processed = TRUE, processed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, [alert_id])
