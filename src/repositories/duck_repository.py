"""DuckDB リポジトリ層

DuckDB への具体的なデータ読み書きを担当する。
既存の Peewee Repository に代わり、Polars/Arrow ベースの高速 I/O を提供する。
"""

from logging import getLogger
from typing import Any, List, Optional

import polars as pl

from src.database.duck_client import DuckDBClient


class DuckDBRepository:
    """DuckDB 向けリポジトリ実装 (v6.1.0 Unified)

    - [x] **Task 1: DuckDBRepository の基盤化 (Structural Hardening)**
    - [x] `_upsert_dataframe` 共通テンプレートメソッドの実装
    - [x] `save_stocks` のテンプレート移行
    - [x] `save_metrics` のテンプレート移行
    - [x] `save_fundamentals` のテンプレート移行
    """

    def __init__(self, client: Optional[DuckDBClient] = None):
        self.logger = getLogger(__name__)
        self.client = client or DuckDBClient()
        self.ensure_schema()

    def _upsert_dataframe(
        self, table_name: str, df: pl.DataFrame, conflict_keys: List[str]
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

            # 入力 DF に存在しないカラムを補完（stocks.is_active は True、その他は Null）
            for col in fixed_cols:
                if col not in df.columns:
                    if table_name == "stocks" and col == "is_active":
                        df = df.with_columns(pl.lit(True).alias(col))
                    else:
                        df = df.with_columns(pl.lit(None).alias(col))

            # 保存対象カラムの文字列化
            col_str = ", ".join(fixed_cols)
            # UPDATE 対象は「元の入力 DF に存在したカラム」かつ「conflict_keys でないもの」に限定
            update_cols = [
                c for c in fixed_cols if c not in conflict_keys and c in input_cols
            ]
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
            df_to_save = df.select(fixed_cols)  # noqa: F841
            conn.execute(
                f"CREATE OR REPLACE TEMPORARY TABLE {temp_table} AS SELECT * FROM df_to_save"
            )

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
            conn.execute(
                "DELETE FROM daily_metrics WHERE dayofweek(entry_date) IN (0, 6)"
            )
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
                    macd DOUBLE,
                    macd_signal DOUBLE,
                    macd_hist DOUBLE,
                    macd_status VARCHAR,
                    rsi_14 DOUBLE,
                    ma25 DOUBLE,
                    ma75 DOUBLE,
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
                    trend_signal INTEGER,
                    trend_up DOUBLE,
                    fetch_status VARCHAR,
                    repair_metadata TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, entry_date)
                )
            """)
            # アラート監視テーブル（シーケンス作成）
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
            for stock_col in (
                "status VARCHAR DEFAULT 'active'",
                "exclusion_reason VARCHAR",
                "excluded_until VARCHAR",
                "fail_count INTEGER DEFAULT 0",
                "edinet_code VARCHAR",
            ):
                conn.execute(f"ALTER TABLE stocks ADD COLUMN IF NOT EXISTS {stock_col}")

            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS prev_net_profit DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS shares_outstanding DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS operating_income DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS net_assets DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS total_assets DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS dps DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS net_profit DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS eps DOUBLE"
            )
            conn.execute(
                "ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS bps DOUBLE"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fundamentals_code ON fundamentals(code)"
            )
            # 財務値の出所となった書類の決算期末・提出日時。古い期の書類 (過年度の訂正報告書など)
            # で新しい値を上書きしないための判定と、株式分割の調整に使う。
            # period_end/submitted_at は損益・1 株当たり指標 (有価証券報告書)、
            # bs_* は貸借対照表の項目 (有価証券報告書または半期報告書) の出所
            for meta_col in ("period_end", "submitted_at", "bs_period_end", "bs_submitted_at"):
                conn.execute(
                    f"ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS {meta_col} VARCHAR"
                )
            # 成長率 (旧スキーマには列が無く、シードの値が取り込み時に捨てられていた)
            for growth_col in ("sales_growth", "profit_growth", "profit_growth_raw"):
                conn.execute(
                    f"ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS {growth_col} DOUBLE"
                )

            # EDINET 処理済み書類メタデータ (セッション跨ぎ差分キャッシュ用)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS edinet_documents (
                    doc_id VARCHAR PRIMARY KEY,
                    code VARCHAR,
                    doc_type VARCHAR,
                    submit_date VARCHAR,
                    is_annual BOOLEAN DEFAULT FALSE,
                    status VARCHAR DEFAULT 'success',
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "ALTER TABLE edinet_documents ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'success'"
            )
            conn.execute(
                "ALTER TABLE edinet_documents ADD COLUMN IF NOT EXISTS parser_version INTEGER"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_edinet_docs_code ON edinet_documents(code)"
            )
            # 株式分割 (1 株当たりの財務指標を分割後の株価に合わせるために使う)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_splits (
                    code VARCHAR,
                    split_date DATE,
                    ratio DOUBLE,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, split_date)
                )
            """)

            # [v27.2] 市場カレンダー・キャッシュ
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_calendar (
                    date DATE PRIMARY KEY
                )
            """)
            # 小さな状態値 (銘柄マスタの最終更新日など)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # stocks テーブルのスキーマ進化
            conn.execute(
                "ALTER TABLE stocks ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"
            )
            conn.execute(
                "UPDATE stocks SET is_active = TRUE WHERE is_active IS NULL"
            )

            # [v29.0] daily_metrics のスキーマ進化 (SSOT 同期)
            for col_def in [
                ("macd", "DOUBLE"),
                ("macd_signal", "DOUBLE"),
                ("macd_status", "VARCHAR"),
                ("ma25", "DOUBLE"),
                ("ma75", "DOUBLE"),
                ("trend_signal", "INTEGER"),
                ("trend_up", "DOUBLE"),
            ]:
                conn.execute(
                    f"ALTER TABLE daily_metrics ADD COLUMN IF NOT EXISTS {col_def[0]} {col_def[1]}"
                )

    def get_all_codes(self) -> List[str]:
        """登録されている全銘柄コードを取得する。"""
        with self.client.get_connection() as conn:
            cols = [
                r[1] for r in conn.execute("PRAGMA table_info('stocks')").fetchall()
            ]
            if "is_active" in cols:
                res = conn.execute(
                    "SELECT code FROM stocks WHERE is_active IS NOT FALSE"
                ).fetchall()
            else:
                res = conn.execute("SELECT code FROM stocks").fetchall()
            return [str(r[0]) for r in res]

    def get_metrics_count(self) -> int:
        """蓄積されている時系列データの総件数を取得する。"""
        with self.client.get_connection() as conn:
            res = conn.execute("SELECT count(*) FROM daily_metrics").fetchone()
            return int(res[0]) if res else 0

    def get_meta(self, key: str) -> Optional[str]:
        """app_meta の値を取得する (無ければ None)。"""
        with self.client.get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", [key]
            ).fetchall()
        return str(row[0][0]) if row and row[0][0] is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """app_meta に値を保存する。"""
        with self.client.get_connection() as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                [key, value],
            )

    def save_stocks(self, df: pl.DataFrame):
        """銘柄マスタを保存・更新する。"""
        self._upsert_dataframe("stocks", df, ["code"])

    def save_metrics(self, df: pl.DataFrame):
        """日次指標データを保存・更新する。"""
        if df.is_empty():
            return
        if "code" not in df.columns or "entry_date" not in df.columns:
            raise ValueError(
                "UPSERT to daily_metrics requires 'code' and 'entry_date'."
            )
        self._upsert_dataframe("daily_metrics", df, ["code", "entry_date"])

    def save_fundamentals(self, df: pl.DataFrame):
        """財務データを保存・更新する。"""
        if df.is_empty():
            return
        if "code" not in df.columns:
            raise ValueError("UPSERT to fundamentals requires 'code'.")
        self._upsert_dataframe("fundamentals", df, ["code"])

    def load_metrics(
        self, codes: Optional[List[str]] = None, days: int = 365
    ) -> pl.DataFrame:
        """指定された期間・銘柄のデータを取得する。"""
        with self.client.get_connection() as conn:
            # DuckDB で interval へのパラメータ指定は 'interval 1 day * ?' の形式が安全
            query = "SELECT * FROM daily_metrics WHERE entry_date >= current_date - (interval '1 day' * ?)"
            params: list[Any] = [days]

            if codes:
                query += " AND code IN (" + ",".join(["?" for _ in codes]) + ")"
                params.extend(codes)

            return conn.execute(query, params).pl()

    def load_stocks(self) -> pl.DataFrame:
        """全銘柄マスタを Polars DataFrame として取得する"""
        with self.client.get_connection() as conn:
            return conn.execute("SELECT * FROM stocks").pl()

    def save_alert(self, code: str, alert_type: str, message: str):
        """通知を保存する"""
        with self.client.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sentinel_alerts (code, alert_type, alert_message)
                VALUES (?, ?, ?)
            """,
                [code, alert_type, message],
            )

    def get_unprocessed_alerts(self) -> pl.DataFrame:
        """未処理の通知を取得する"""
        with self.client.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM sentinel_alerts WHERE is_processed = FALSE"
            ).pl()

    def mark_alert_processed(self, alert_id: int):
        """通知を処理済みとする"""
        with self.client.get_connection() as conn:
            conn.execute(
                """
                UPDATE sentinel_alerts 
                SET is_processed = TRUE, processed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                [alert_id],
            )

    def get_processed_edinet_doc_ids(self, min_parser_version: int = 0) -> set[str]:
        """処理済みの EDINET doc_id セットを取得する (Colab セッション跨ぎ差分キャッシュ用)

        Args:
            min_parser_version: この版以上のパーサーで処理した書類だけを返す
                (古い版で処理した書類は取り込み直す。0 なら全件)
        """
        try:
            with self.client.get_connection() as conn:
                res = conn.execute(
                    "SELECT doc_id FROM edinet_documents WHERE coalesce(parser_version, 0) >= ?",
                    [min_parser_version],
                ).fetchall()
                return {row[0] for row in res if row and row[0]}
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to fetch processed EDINET docs: {e}")
            return set()

    def record_edinet_document(
        self,
        doc_id: str,
        code: str,
        doc_type: Optional[str] = None,
        submit_date: Optional[str] = None,
        is_annual: bool = False,
        status: str = "success",
        parser_version: Optional[int] = None,
    ) -> None:
        """EDINET 書類の処理ステータスを記録する (success, parse_failed, superseded, error 等)"""
        self.record_edinet_documents(
            [
                {
                    "doc_id": doc_id,
                    "code": code,
                    "doc_type": doc_type,
                    "submit_date": submit_date,
                    "is_annual": is_annual,
                    "status": status,
                    "parser_version": parser_version,
                }
            ]
        )

    def record_edinet_documents(self, docs: List[dict]) -> None:
        """複数の EDINET 書類の処理ステータスを、1 つの接続でまとめて記録する。"""
        if not docs:
            return
        try:
            with self.client.get_connection() as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO edinet_documents
                        (doc_id, code, doc_type, submit_date, is_annual, status, parser_version, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [
                        [
                            d["doc_id"], d["code"], d.get("doc_type"), d.get("submit_date"),
                            d.get("is_annual", False), d.get("status", "success"),
                            d.get("parser_version"),
                        ]
                        for d in docs
                    ],
                )
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to record {len(docs)} EDINET docs: {e}")

    def save_splits(self, rows: list[tuple[str, Any, float]]) -> None:
        """株式分割 (code, split_date, ratio) を保存する。同じ日の分割は上書き。"""
        if not rows:
            return
        with self.client.get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_splits (code, split_date, ratio) VALUES (?, ?, ?)",
                [[c, d, r] for c, d, r in rows],
            )

    def load_splits(self) -> pl.DataFrame:
        """記録済みの株式分割を返す (code, split_date, ratio)。"""
        with self.client.get_connection() as conn:
            return conn.execute(
                "SELECT code, split_date, ratio FROM stock_splits"
            ).pl()
