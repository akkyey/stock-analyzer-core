"""オーケストレーションコンテキスト

オーケストレーターの状態管理、共通リポジトリの提供、および外部プロセスの実行を制御する。
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.config_singleton import ConfigSingleton
from src.database.duck_client import DuckDBClient
from src.repositories.duck_repository import DuckDBRepository
from src.utils import get_current_time, get_today_str

class OrchestratorContext:
    """オーケストレーションの共有コンテキスト (v7.0.0 DuckDB 一本化)"""

    def __init__(
        self,
        debug_mode: bool = False,
        no_ai: bool = False,
        threads: bool = True,
        chunk_size: int = 100,
    ) -> None:
        """コンテキストを初期化する。"""
        self.logger = logging.getLogger(__name__)
        raw_config = ConfigSingleton.get_config()
        self.config = dict(raw_config) if raw_config else {}
        self.config["no_ai"] = no_ai
        
        # DuckDB 系統の初期設定
        self._duck_repo = None
        self._stock_repo = None
        self._funda_repo = None
        self._market_repo = None
        self._analysis_repo = None
        self._sentinel_repo = None
        self._rank_repo = None
        
        self.debug_mode = debug_mode
        self.no_ai = no_ai
        self.threads = threads
        self.chunk_size = chunk_size
        self.config.update({
            "debug_mode": debug_mode,
            "no_ai": no_ai,
            "threads": threads,
            "chunk_size": chunk_size
        })
        self.limit: int | None = None
        self.offset: int = 0
        self.report_url: str | None = None

        self.errors: list[str] = []
        self.skipped_count: int = 0
        self.has_partial_failure: bool = False

        # 統計とパフォーマンス管理
        self.perf_stats = {
            "fetch_sec": 0.0,
            "calc_sec": 0.0,
            "db_sec": 0.0,
            "subprocess_sec": 0.0,
            "other_sec": 0.0,
        }
        
        from src.reporter import StockReporter
        paths = self.config.get("paths") or {}
        output_dir = paths.get("output_dir") or "data/output"
        self.reporter = StockReporter(output_dir=output_dir)

        self._auditor_path: str | None = None
        self._notifier: Any = None

    @property
    def duck_repo(self) -> DuckDBRepository:
        """DuckDBRepository インスタンスを取得する (Lazy Load)"""
        if self._duck_repo is None:
            self.logger.info("Initializing DuckDBRepository (Lazy Load)...")
            from src.repositories.duck_repository import DuckDBRepository
            self._duck_repo = DuckDBRepository()
        return self._duck_repo

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

    @property
    def analysis_repo(self):
        if self._analysis_repo is None:
            from src.repositories.analysis_repository import AnalysisRepository
            self._analysis_repo = AnalysisRepository(duck_repo=self.duck_repo)
        return self._analysis_repo

    @property
    def sentinel_repo(self):
        if self._sentinel_repo is None:
            from src.repositories.sentinel_repository import SentinelRepository
            self._sentinel_repo = SentinelRepository(duck_repo=self.duck_repo)
        return self._sentinel_repo

    @property
    def rank_repo(self):
        if self._rank_repo is None:
            from src.repositories.rank_history_repository import RankHistoryRepository
            self._rank_repo = RankHistoryRepository(duck_repo=self.duck_repo)
        return self._rank_repo

    def get_today_str(self) -> str:
        """今日の日付文字列 (YYYY-MM-DD) を取得する。"""
        return get_today_str()

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

    def get_auditor_path(self) -> str:
        """equity_auditor.py の絶対パスを取得する。"""
        if self._auditor_path is None:
            root_dir = Path(__file__).parent.parent.parent
            self._auditor_path = str(root_dir / "equity_auditor.py")
        return self._auditor_path

    def execute_equity_auditor(
        self, codes: list[str], strategy: str, batch_size: int = 50
    ) -> None:
        """equity_auditor.py をサブプロセスとして実行し、AI 分析を行う。"""
        if not codes:
            return

        import json
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        auditor_path = self.get_auditor_path()
        task_dir = Path("data/tasks")
        task_dir.mkdir(parents=True, exist_ok=True)

        batches = [codes[i : i + batch_size] for i in range(0, len(codes), batch_size)]
        MAX_AI_WORKERS = self.config.get("max_ai_threads", 3)

        def run_batch(batch_codes, batch_idx):
            task_file = task_dir / f"task_{strategy}_{batch_idx}_{get_current_time().strftime('%H%M%S')}.json"
            task_data = {
                "strategy": strategy,
                "codes": batch_codes,
                "created_at": get_current_time().isoformat(),
            }
            with task_file.open("w", encoding="utf-8") as f:
                json.dump(task_data, f, indent=2, ensure_ascii=False)

            cmd = [sys.executable, auditor_path, "--mode", "analyze", "--strategy", strategy, "--tasks", str(task_file)]
            if self.debug_mode:
                cmd.append("--debug")

            t_sub_start = time.time()
            try:
                project_root = str(Path(auditor_path).parent)
                subprocess.run(cmd, check=True, timeout=900, cwd=project_root)
                self.perf_stats["subprocess_sec"] += time.time() - t_sub_start

                if not self.debug_mode:
                    task_file.unlink()

                # DuckDBRepository 経由でアラートを処理済みに更新
                for c in batch_codes:
                    self.duck_repo.save_alert(c, "AI_ANALYSIS_COMPLETED", f"Processed by {strategy}")
                
                return len(batch_codes)
            except Exception as e:
                self.logger.error(f"  ❌ AI 分析バッチ {batch_idx} 失敗: {e}")
                self.add_error(f"分析失敗: {strategy} Batch {batch_idx}")
                self.skipped_count += len(batch_codes)
                return 0

        with ThreadPoolExecutor(max_workers=MAX_AI_WORKERS) as executor:
            futures = [executor.submit(run_batch, b, i) for i, b in enumerate(batches)]
            for future in as_completed(futures):
                future.result()

    def get_session_stats(self) -> dict[str, Any]:
        """今日の分析セッションの統計情報を DuckDB から取得する。"""
        stats = {
            "total_tasks": 0,
            "unique_codes": 0,
            "api_calls": 0,
            "repair_count": 0,
            "avg_confidence": 0.0,
        }
        try:
            with self.duck_repo.client.get_connection() as conn:
                # 1. 分析結果のカウント
                ar_count = conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
                stats["total_tasks"] = ar_count
                stats["unique_codes"] = conn.execute("SELECT COUNT(DISTINCT code) FROM analysis_results").fetchone()[0]
                stats["api_calls"] = ar_count * 5 # 推定値
        except Exception as e:
            self.logger.warning(f"⚠️ 統計情報の取得に失敗しました: {e}")
        return stats

    def execute_ingest_repair(self, codes: list[str], batch_size: int = 500) -> int:
        """データ修復用の ingest サブプロセスを実行する。"""
        total_repaired = 0
        import time
        auditor_path = self.get_auditor_path()
        project_root = str(Path(auditor_path).parent)

        for i in range(0, len(codes), batch_size):
            batch = codes[i : i + batch_size]
            code_str = ",".join(batch)
            cmd = [
                sys.executable,
                auditor_path,
                "--mode",
                "ingest",
                "--force",
                "--codes",
                code_str,
            ]
            if self.debug_mode:
                cmd.append("--debug")

            t_sub_start = time.time()
            try:
                # [v18.8] タイムアウト(5分)を設定
                subprocess.run(
                    cmd,
                    check=True,
                    timeout=300,
                    cwd=project_root,
                )
                self.perf_stats["subprocess_sec"] += time.time() - t_sub_start
                total_repaired += len(batch)
                
                if (i // batch_size) % 10 == 0:
                    self.logger.info(
                        f"  Repaired {total_repaired}/{len(codes)} stocks..."
                    )
            except Exception as e:
                self.logger.warning(f"Repair batch failed: {e}")
                
        return total_repaired
    def print_session_summary(self) -> None:
        """セッションの実行結果概要をログ出力する。"""
        stats = self.get_session_stats()
        self.logger.info("=" * 40)
        self.logger.info("📊 ORCHESTRATION SESSION SUMMARY")
        self.logger.info("-" * 40)
        self.logger.info(f"  Mode: {self.config.get('etl_mode', 'unknown')}")
        self.logger.info(f"  Analyzed: {stats['total_tasks']} records")
        self.logger.info(f"  Unique Stocks: {stats['unique_codes']}")
        self.logger.info(f"  Partial Failures: {len(self.errors)}")
        self.logger.info(f"  Skipped: {self.skipped_count}")
        
        # パフォーマンス統計
        for k, v in self.perf_stats.items():
            if v > 0:
                self.logger.info(f"  {k}: {v:.2f}s")
        
        if self.report_url:
            self.logger.info(f"  Report URL: {self.report_url}")
        self.logger.info("=" * 40)
