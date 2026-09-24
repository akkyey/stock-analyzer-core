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


class AcquisitionPhase(BasePhase):
    """データ取得フェーズ (v10: yfinance financials 排除 / EDINET Turbo 統合)"""

    def execute(self, df: Optional[pl.DataFrame] = None) -> Any:
        self.log_info("🚀 Starting Turbo Data Acquisition Phase...")

        from src.fetcher.edinet_fetcher import EdinetFetcher
        from src.fetcher.facade import DataFetcher
        from src.fetcher.turbo_acquisition import TurboAcquisitionManager
        from src.fetcher.xbrl_parser import XbrlParser
        from src.repositories.duck_repository import DuckDBRepository
        from src.repositories.market_data_repository import MarketDataRepository
        from src.services.edinet_bridge import EdinetBridge

        repo = DuckDBRepository()
        fetcher = DataFetcher(self.context.config)
        market_repo = MarketDataRepository()

        # [Phase 0/1] 財務データの正典同期 (Fundamental Truth Sync)
        fetcher_cfg = self.context.config.get("fetcher", {})
        if fetcher_cfg.get("enable_edinet_turbo", True):
            scan_days = fetcher_cfg.get("edinet_scan_days", 30)
            self.log_info(f"⚡ Synchronizing Fundamentals from EDINET (Turbo, {scan_days} days)...")
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
                self.log_error(f"❌ EDINET sync failed (Skipped): {e}")

        # 1. ターゲット銘柄の特定
        target_codes = repo.get_all_codes()
        if self.context.limit:
            target_codes = target_codes[: self.context.limit]

        self.log_info(
            f"Targeting {len(target_codes)} stocks for market data acquisition."
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
                FETCH_BATCH_SIZE = 20  # OHLCVのみなのでバッチサイズ拡大

                batches = [
                    target_codes[i : i + FETCH_BATCH_SIZE]
                    for i in range(0, num_targets, FETCH_BATCH_SIZE)
                ]

                for i, b in enumerate(batches):
                    if stop_event.is_set():
                        self.log_warn("Producer received stop signal. Aborting further fetches.")
                        break

                    # DB履歴が薄い場合は 1y、十分なら 2d (差分) を取得
                    is_db_thin = df_db_hist_all.height < (len(target_codes) * 10)
                    period_to_use = "1y" if is_db_thin else "2d"

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
                                if "entry_date" in df_db_norm.columns and "Date" not in df_db_norm.columns:
                                    df_db_norm = df_db_norm.rename({"entry_date": "Date"})
                                if "Volume" in df_db_norm.columns:
                                    df_db_norm = df_db_norm.with_columns(pl.col("Volume").cast(pl.Float64))

                                df_yf_norm = df_yf_pl
                                if "Volume" in df_yf_norm.columns:
                                    df_yf_norm = df_yf_norm.with_columns(pl.col("Volume").cast(pl.Float64))

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
            if processed_count % 100 == 0:
                self.log_info(
                    f"Acquired market data for {processed_count}/{num_targets} stocks..."
                )

        stop_event.set()  # 正常終了時も確実にセット
        self.log_info(
            f"Data Acquisition completed. {len(all_data_map)} stocks ready for evaluation."
        )
        return all_data_map
