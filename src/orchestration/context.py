"""オーケストレーションコンテキスト

オーケストレーターの状態管理、共通リポジトリの提供、および実行セッションを制御する。
"""

import logging
from typing import Any

from src.config_singleton import ConfigSingleton
from src.utils import get_today_str


class OrchestratorContext:
    """オーケストレーションの共有コンテキスト (DuckDB + Polars)"""

    def __init__(
        self,
        debug_mode: bool = False,
        threads: bool = True,
        chunk_size: int = 100,
    ) -> None:
        """コンテキストを初期化する。"""
        self.logger = logging.getLogger(__name__)
        raw_config = ConfigSingleton.get_config()
        self.config = dict(raw_config) if raw_config else {}

        # DuckDB 系統の初期設定
        self._duck_repo: Any = None
        self._stock_repo: Any = None
        self._funda_repo: Any = None
        self._market_repo: Any = None

        self.debug_mode = debug_mode
        self.threads = threads
        self.chunk_size = chunk_size
        self.config.update(
            {
                "debug_mode": debug_mode,
                "threads": threads,
                "chunk_size": chunk_size,
            }
        )
        self.limit: int | None = None
        self.offset: int = 0
        self.report_url: str | None = None

        self.errors: list[str] = []
        self.skipped_count: int = 0
        self.evaluated_count: int = 0
        self.excluded_count: int = 0
        self.uncalculable_df: Any = None
        self.has_partial_failure: bool = False

        # 統計とパフォーマンス管理
        self.perf_stats = {
            "fetch_sec": 0.0,
            "calc_sec": 0.0,
            "db_sec": 0.0,
            "integration_sec": 0.0,
            "other_sec": 0.0,
        }

        from src.reporter import StockReporter

        paths = self.config.get("paths") or {}
        output_dir = paths.get("output_dir") or "data/output"
        self.reporter = StockReporter(output_dir=output_dir)
        self._notifier: Any = None

    @property
    def duck_repo(self) -> Any:
        """DuckDBRepository インスタンスを取得する (Lazy Load)"""
        if self._duck_repo is None:
            self.logger.info("Initializing DuckDBRepository (Lazy Load)...")
            from src.repositories.duck_repository import DuckDBRepository

            self._duck_repo = DuckDBRepository()
        return self._duck_repo

    @property
    def db(self) -> Any:
        """互換性のための DuckDBRepository エイリアス"""
        return self.duck_repo

    @property
    def stock_repo(self):
        if self._stock_repo is None:
            from src.repositories.stock_repository import StockRepository

            self._stock_repo = StockRepository(duck_repo=self.duck_repo)
        return self._stock_repo

    @property
    def funda_repo(self):
        if self._funda_repo is None:
            from src.repositories.fundamentals_repository import FundamentalsRepository

            self._funda_repo = FundamentalsRepository(duck_repo=self.duck_repo)
        return self._funda_repo

    @property
    def market_repo(self):
        if self._market_repo is None:
            from src.repositories.market_data_repository import MarketDataRepository

            self._market_repo = MarketDataRepository(duck_repo=self.duck_repo)
        return self._market_repo

    def get_today_str(self) -> str:
        """今日の日付文字列 (YYYY-MM-DD) を取得する。"""
        return get_today_str()

    def get_execution_date(self) -> str:
        """実行日付文字列 (YYYY-MM-DD) を取得する。"""
        return self.get_today_str()

    @property
    def notifier(self) -> Any:
        """DiscordNotifier インスタンスを取得する。"""
        if self._notifier is None:
            from src.notifier import DiscordNotifier

            self._notifier = DiscordNotifier()
        return self._notifier

    def add_error(self, message: str, is_fatal: bool = False) -> None:
        """セッション中に発生したエラーを記録する。"""
        log_func = self.logger.error if is_fatal else self.logger.warning
        log_func(f"🚩 Error recorded: {message}")
        self.errors.append(message)
        if not is_fatal:
            self.has_partial_failure = True

    def print_session_summary(self) -> None:
        """セッションの実行結果概要をログ出力する。"""
        self.logger.info("=" * 40)
        self.logger.info("📊 ORCHESTRATION SESSION SUMMARY")
        self.logger.info("-" * 40)
        self.logger.info(f"  Mode: {self.config.get('etl_mode', 'unknown')}")
        self.logger.info(f"  Evaluated (Passed): {self.evaluated_count}")
        self.logger.info(f"  Excluded (PreFilter): {self.excluded_count}")
        self.logger.info(f"  Partial Failures: {len(self.errors)}")
        self.logger.info(f"  Skipped: {self.skipped_count}")

        # パフォーマンス統計
        for k, v in self.perf_stats.items():
            if v > 0:
                self.logger.info(f"  {k}: {v:.2f}s")

        if self.report_url:
            self.logger.info(f"  Report URL: {self.report_url}")
        self.logger.info("=" * 40)

