"""データ取得フェーズ (Phase 1) - Turbo Fidelity Edition

1. EDINET Turbo 同期: 最新の財務データ (有報・四半報) を並列取得し DuckDB を更新。
2. Market Data 取得: yfinance から OHLCV を取得し、DB 履歴と結合。
"""

import queue
import threading
from typing import Any, Dict, Optional

import pandas as pd
import polars as pl

from src.orchestration.phases.base import BasePhase
from src.utils.diagnostics import record_fetch_stat


class AcquisitionPhase(BasePhase):
    """データ取得フェーズ (v10: yfinance financials 排除 / EDINET Turbo 統合)"""

    # 流動性指標 (直近20日平均売買代金) の算出に必要な最低履歴行数。
    # これ未満の銘柄は 1y、十分な銘柄は 2d (差分) で取得する。
    MIN_HISTORY_ROWS = 20
    FETCH_BATCH_SIZE = 20  # OHLCVのみなのでバッチサイズ拡大

    def _plan_fetch_batches(
        self,
        target_codes: list,
        db_hist_by_code: Dict[Any, Any],
    ) -> list:
        """銘柄ごとに履歴の厚みで取得期間を決め、(codes, period) のバッチ列を返す。

        - 履歴が MIN_HISTORY_ROWS 未満 (新規・過去の取得欠落) → "1y"
        - 十分 → "2d" (差分)
        - config["is_first_run"] が True の場合は全銘柄 "1y"

        リクエスト総数は増えず (銘柄を 2 グループに分けるだけ)、429 などで
        欠落した銘柄も次回は自動的に 1y で取り直される。
        """
        cfg = getattr(self.context, "config", None) or {}
        force_full = bool(cfg.get("is_first_run", False))

        thin, enough = [], []
        for code in target_codes:
            df_db = db_hist_by_code.get((code,))
            rows = 0 if df_db is None else df_db.height
            if force_full or rows < self.MIN_HISTORY_ROWS:
                thin.append(code)
            else:
                enough.append(code)

        size = self.FETCH_BATCH_SIZE
        batches: list[tuple[list, str]] = []
        for codes, period in ((thin, "1y"), (enough, "2d")):
            batches.extend(
                (codes[i : i + size], period) for i in range(0, len(codes), size)
            )
        self.log_info(
            f"Fetch plan: {len(thin)} stocks -> 1y, {len(enough)} stocks -> 2d "
            f"({len(batches)} batches)"
        )
        record_fetch_stat(self.context, "plan_stocks_1y", len(thin))
        record_fetch_stat(self.context, "plan_stocks_2d", len(enough))
        record_fetch_stat(self.context, "plan_batches", len(batches))
        return batches

    def execute(self, df: Optional[pl.DataFrame] = None) -> Any:
        self.log_info("🚀 Starting Turbo Data Acquisition Phase...")

        from pathlib import Path

        from src.fetcher.edinet_fetcher import (
            EdinetAuthenticationError,
            EdinetFetcher,
            mask_api_key,
        )
        from src.fetcher.facade import DataFetcher
        from src.fetcher.turbo_acquisition import TurboAcquisitionManager
        from src.fetcher.xbrl_parser import XbrlParser
        from src.repositories.duck_repository import DuckDBRepository
        from src.repositories.fundamentals_repository import FundamentalsRepository
        from src.repositories.market_data_repository import MarketDataRepository
        from src.services.edinet_bridge import EdinetBridge

        repo = DuckDBRepository()
        fetcher = DataFetcher(self.context.config)
        market_repo = MarketDataRepository()

        # 1. ターゲット銘柄の特定 (未登録時は JPX から自動初期シード)
        target_codes = repo.get_all_codes()
        if not target_codes:
            self.log_info(
                "ℹ️ 銘柄マスタが未登録です。JPX銘柄リストから初期登録を実行します..."
            )
            try:
                jpx_df = fetcher.jpx_fetcher.fetch_jpx_list(fallback_on_error=True)
                if not jpx_df.empty:
                    df_pl = pl.from_pandas(
                        jpx_df[["code", "name", "sector", "market"]]
                    ).with_columns(pl.lit(True).alias("is_active"))
                    repo.save_stocks(df_pl)
                    target_codes = repo.get_all_codes()
                    print(
                        f"   ✅ 銘柄マスタに {len(target_codes)} 銘柄を初期登録しました。",
                        flush=True,
                    )
            except Exception as e:
                self.log_error(f"❌ 銘柄マスタの初期登録に失敗しました: {e}")

        # 1-2. 財務データの初期シード (全件ベースラインデータ未登録時はバンドルされたシードデータから自動投入)
        funda_repo = FundamentalsRepository(repo)
        if funda_repo.get_count() < 3000:
            seed_parquet = (
                Path(__file__).resolve().parent.parent.parent
                / "resources"
                / "fundamentals_seed.parquet"
            )
            if seed_parquet.exists():
                self.log_info(
                    "ℹ️ 財務ベースラインデータが未登録/不足しています。バンドルされたシードデータから初期登録を実行します..."
                )
                try:
                    df_seed = pl.read_parquet(str(seed_parquet))
                    repo.save_fundamentals(df_seed)
                    print(
                        f"   ✅ 財務データに {len(df_seed)} 銘柄を初期登録しました。",
                        flush=True,
                    )
                except Exception as e:
                    self.log_error(f"❌ 財務データの初期登録に失敗しました: {e}")

        # [Phase 0/1] 財務データの正典同期 (Fundamental Truth Sync)
        fetcher_cfg = self.context.config.get("fetcher", {})
        if fetcher_cfg.get("enable_edinet_turbo", True):
            scan_days = fetcher_cfg.get("edinet_scan_days", 30)
            self.log_info(
                f"⚡ Synchronizing Fundamentals from EDINET (Turbo, {scan_days} days)..."
            )
            try:
                edinet_fetcher = EdinetFetcher(self.context.config)
                xbrl_parser = XbrlParser()
                turbo_mgr = TurboAcquisitionManager(
                    edinet_fetcher, xbrl_parser, self.context.config
                )

                # 過去 N 日分の書類を並列・差分スキャニング (二層キャッシュガード)
                turbo_mgr.run_turbo_acquisition(days=scan_days)

                # ブリッジによる DB 反映 (成果物キャッシュを保持して平常時の実通信を遮断)
                bridge = EdinetBridge()
                sync_count = bridge.bridge_all(purge_after=False)
                self.log_info(
                    f"✅ EDINET sync completed. {sync_count} documents integrated."
                )
            except Exception as e:
                if isinstance(e, EdinetAuthenticationError):
                    self.log_error(f"❌ EDINET 認証失敗: {e}")
                    raise RuntimeError(
                        f"EDINET APIキーの認証に失敗したため処理を中断しました: {e}"
                    ) from e
                self.log_error(
                    f"❌ EDINET sync failed (Skipped): {mask_api_key(str(e))}"
                )

        if self.context.limit:
            target_codes = target_codes[: self.context.limit]

        print(
            f"📡 全 {len(target_codes)} 銘柄の市場データ取得を開始します...",
            flush=True,
        )

        # 2. DB から過去履歴を一括ロード (RSI等バッファ用)
        df_db_hist_all = market_repo.get_all_history_pl(months=3)

        # 3. Producer-Consumer パイプライン (Market Data 取得)
        result_queue: queue.Queue = queue.Queue(maxsize=200)
        # 指摘10: ループ内の全行走査を回避するため、あらかじめcode別パーティション辞書を作成
        if not df_db_hist_all.is_empty() and "code" in df_db_hist_all.columns:
            db_hist_by_code = df_db_hist_all.partition_by("code", as_dict=True)
        else:
            db_hist_by_code = {}

        num_targets = len(target_codes)

        # 指摘11: yfinance のレート制限回避と内部マルチスレッド (yf.download の threads=True) を
        # 最大限活かすため、バッチ単位で同期的に直列取得（外部の ThreadPoolExecutor は不要）
        stop_event = threading.Event()

        def _producer():
            """Hybrid Producer: 市場価格データのみを取得"""
            self.log_info("📡 Market Data Producer started...")
            try:
                batches = self._plan_fetch_batches(target_codes, db_hist_by_code)

                for i, (b, period_to_use) in enumerate(batches):
                    if stop_event.is_set():
                        self.log_warn(
                            "Producer received stop signal. Aborting further fetches."
                        )
                        break

                    try:
                        # [v10] fetch_stock_data は内部で yf.download(threads=True) を呼ぶため、ここは軽量
                        hist_map = fetcher.fetch_stock_data(
                            b, period=period_to_use, context=self.context
                        )
                    except Exception as e:
                        self.log_error(f"Batch {i + 1} failed: {e}")
                        continue

                    if hist_map:
                        for code, df_yf in hist_map.items():
                            if stop_event.is_set():
                                break

                            df_db = db_hist_by_code.get((code,))

                            # 指摘12: pandas への不要な往復変換を撤廃し Polars DataFrame のまま保持
                            if isinstance(df_yf, pd.DataFrame):
                                df_yf_pl = pl.from_pandas(df_yf.reset_index())
                            else:
                                df_yf_pl = df_yf

                            rename_map = {"index": "Date", "date": "Date"}
                            for old, new in rename_map.items():
                                if old in df_yf_pl.columns:
                                    df_yf_pl = df_yf_pl.rename({old: new})

                            if df_db is None or df_db.is_empty():
                                df_final_pl = df_yf_pl
                            else:
                                # 結合 & 重複排除 (指摘7: 型差異の吸収と keep="last")
                                df_db_norm = df_db
                                if (
                                    "entry_date" in df_db_norm.columns
                                    and "Date" not in df_db_norm.columns
                                ):
                                    df_db_norm = df_db_norm.rename(
                                        {"entry_date": "Date"}
                                    )
                                if "Volume" in df_db_norm.columns:
                                    df_db_norm = df_db_norm.with_columns(
                                        pl.col("Volume").cast(pl.Float64)
                                    )

                                df_yf_norm = df_yf_pl
                                if "Volume" in df_yf_norm.columns:
                                    df_yf_norm = df_yf_norm.with_columns(
                                        pl.col("Volume").cast(pl.Float64)
                                    )

                                df_final_pl = (
                                    pl.concat(
                                        [
                                            df_db_norm.with_columns(
                                                pl.col("Date").cast(pl.Datetime)
                                            ),
                                            df_yf_norm.with_columns(
                                                pl.col("Date").cast(pl.Datetime)
                                            ),
                                        ],
                                        how="diagonal_relaxed",
                                    )
                                    .unique("Date", keep="last")
                                    .sort("Date")
                                )

                            # Consumer 停止時の永久ブロック防止のためタイムアウト付きで put
                            while not stop_event.is_set():
                                try:
                                    result_queue.put((code, df_final_pl), timeout=2.0)
                                    break
                                except queue.Full:
                                    continue
            except Exception as e:
                self.log_error(f"Producer Fatal Error: {e}")
            finally:
                try:
                    result_queue.put(None, timeout=2.0)
                except queue.Full:
                    pass

        # daemon=True にしてプロセス終了を妨げない設計にする
        producer_thread = threading.Thread(target=_producer, daemon=True)
        producer_thread.start()

        # 4. Consumer 集約 (指摘7-3: タイムアウト耐性とプロデューサー生存確認)
        all_data_map: Dict[str, Any] = {}
        processed_count = 0
        timeout_retries = 0
        MAX_TIMEOUT_RETRIES = 3

        while True:
            try:
                item = result_queue.get(timeout=60)
            except queue.Empty:
                if producer_thread.is_alive():
                    timeout_retries += 1
                    self.log_info(
                        f"Producer is still working. Waiting for items (retry {timeout_retries}/{MAX_TIMEOUT_RETRIES})..."
                    )
                    if timeout_retries < MAX_TIMEOUT_RETRIES:
                        continue
                    err_msg = "Producer timed out exceeding max retries. Proceeding with collected data."
                    self.log_error(err_msg)
                    if hasattr(self.context, "add_error"):
                        self.context.add_error(err_msg)
                    stop_event.set()  # Producer スレッドに停止シグナルを通知
                    break
                self.log_warn("Producer thread has finished. Completing consumption.")
                stop_event.set()
                break

            if item is None:
                break

            timeout_retries = 0  # 正常受信時はリセット
            code, df_data = item
            all_data_map[code] = df_data
            processed_count += 1
            if processed_count % 500 == 0 or processed_count == num_targets:
                print(
                    f"   📊 市場データ取得進行中: {processed_count}/{num_targets} 銘柄完了...",
                    flush=True,
                )

        stop_event.set()  # 正常終了時も確実にセット
        print(
            f"✅ 市場データ取得完了: 全 {len(all_data_map)} 銘柄のデータを準備しました。",
            flush=True,
        )
        return all_data_map
