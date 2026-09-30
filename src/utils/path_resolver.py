"""データパス解決モジュール (PathResolver)

実行環境（ローカルPC、常駐サーバー、Google Colab 等）に応じて、
作業層・永続層のデータディレクトリパスを透過的に解決する。
"""

import os
from pathlib import Path
from typing import Optional


class PathResolver:
    """実行環境に応じたデータパスの解決クラス"""

    @classmethod
    def get_base_dir(cls, custom_path: Optional[str] = None) -> Path:
        """ベースデータディレクトリを取得する。

        優先順位:
        1. 引数 custom_path
        2. 環境変数 STOCK_ANALYZER_BASE_DIR
        3. デフォルト（リポジトリ直下の data ディレクトリ）
        """
        if custom_path:
            return Path(custom_path).resolve()
        env_path = os.getenv("STOCK_ANALYZER_BASE_DIR")
        if env_path:
            return Path(env_path).resolve()
        # デフォルト (プロジェクトルート直下の data ディレクトリ)
        return Path(__file__).resolve().parent.parent.parent / "data"

    @classmethod
    def get_cache_dir(cls, base_dir: Optional[Path] = None) -> Path:
        """キャッシュディレクトリ (DuckDB / メタデータ等)"""
        b = base_dir or cls.get_base_dir()
        p = b / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def get_output_dir(cls, base_dir: Optional[Path] = None) -> Path:
        """出力ディレクトリ (daily_report.csv 等)"""
        b = base_dir or cls.get_base_dir()
        p = b / "output"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def get_tmp_dir(cls, base_dir: Optional[Path] = None) -> Path:
        """一時作業ディレクトリ (ZIP展開・パース用)"""
        b = base_dir or cls.get_base_dir()
        p = b / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def get_duckdb_path(cls, base_dir: Optional[Path] = None) -> Path:
        """DuckDB データベースファイルパス (cache/stock_analyzer.duckdb に統一)"""
        b = base_dir or cls.get_base_dir()
        cache_db = b / "cache" / "stock_analyzer.duckdb"
        root_db = b / "stock_analyzer.duckdb"
        if root_db.exists() and not cache_db.exists():
            return root_db
        cache_db.parent.mkdir(parents=True, exist_ok=True)
        return cache_db
