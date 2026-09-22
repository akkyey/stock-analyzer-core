"""DuckDB クライアント

DuckDB への接続管理と初期化を担う。
スレッドセーフなシングルトンとして提供され、コンテナ環境におけるメモリ制限にも対応する。
"""

import os
import threading
from pathlib import Path
from typing import Optional

import duckdb
from src.config_singleton import ConfigSingleton
from src.constants import _PROJECT_ROOT
from logging import getLogger

logger = getLogger(__name__)

class DuckDBClient:
    """DuckDB 接続管理クラス (Singleton)"""
    _instance: Optional['DuckDBClient'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[str] = None):
        """初期化。初回のみ実行される。"""
        if hasattr(self, "_initialized") and self._initialized:
            return
            
        with self._lock:
            if hasattr(self, "_initialized") and self._initialized:
                return

            # 設定からパスを取得
            config = ConfigSingleton.get_config()
            paths = config.get("paths", {})
            
            # [v15.0] Primary DuckDB Storage for stock-analyzer-core
            default_path = str(_PROJECT_ROOT / "data" / "stock_analyzer.duckdb")
            self.db_path = db_path or paths.get("duckdb_file") or default_path
            
            # ディレクトリの作成
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            
            # コンテナ環境向けのメモリ制限 (デフォルト 4GB または環境変数から取得)
            self.memory_limit = os.getenv("DUCKDB_MEMORY_LIMIT", "4GB")
            
            self._initialized = True
            logger.info(f"DuckDBClient initialized with path: {self.db_path}, limit: {self.memory_limit}")

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """データベース接続を取得する。"""
        try:
            # インメモリモードの場合、同一の DB インスタンスを共有する必要がある
            if self.db_path == ":memory:":
                if not hasattr(self, "_memory_conn") or self._memory_conn is None:
                    with self._lock:
                        if not hasattr(self, "_memory_conn") or self._memory_conn is None:
                            self._memory_conn = duckdb.connect(self.db_path, config={
                                'max_memory': self.memory_limit,
                                'threads': os.cpu_count() or 4
                            })
                # メイン接続からカーソルを作成して返す
                return self._memory_conn.cursor()

            # 通常のファイルベース接続
            conn = duckdb.connect(self.db_path, config={
                'max_memory': self.memory_limit,
                'threads': os.cpu_count() or 4
            })
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to DuckDB: {e}")
            raise

    def execute_ddl(self, ddl: str):
        """DDLを実行する（初期化用）。"""
        with self.get_connection() as conn:
            conn.execute(ddl)
            logger.info("DDL executed successfully.")

# 便利関数
def get_duck_connection() -> duckdb.DuckDBPyConnection:
    """シングルトンインスタンスから接続を取得するショートカット。"""
    return DuckDBClient().get_connection()
