"""データ取得フェーズ (Phase 1) - Turbo Fidelity Edition

1. EDINET Turbo 同期: 最新の財務データ (有報・四半報) を並列取得し DuckDB を更新。
2. Market Data 取得: yfinance から OHLCV を取得し、DB 履歴と結合。
"""

import queue
import threading
import polars as pl
from typing import Optional, Dict, Any
from src.orchestration.phases.base import BasePhase

class AcquisitionPhase(BasePhase):
    """データ取得フェーズ (v10: yfinance financials 排除 / EDINET Turbo 統合)"""

    def execute(self, df: Optional[pl.DataFrame] = None) -> Optional[pl.DataFrame]:
        self.log_info("🚀 Starting Turbo Data Acquisition Phase...")
        
        from src.fetcher.facade import DataFetcher
        from src.fetcher.edinet_fetcher import EdinetFetcher
        from src.fetcher.xbrl_parser import XbrlParser
        from src.fetcher.turbo_acquisition import TurboAcquisitionManager
        from src.services.edinet_bridge import EdinetBridge
        from src.repositories.duck_repository import DuckDBRepository
        from src.repositories.market_data_repository import MarketDataRepository

        repo = DuckDBRepository()
        fetcher = DataFetcher(self.context.config)
        market_repo = MarketDataRepository()

        # [Phase 0/1] 財務データの正典同期 (Fundamental Truth Sync)
        if self.context.config.get("fetcher", {}).get("enable_edinet_turbo", True):
            self.log_info("⚡ Synchronizing Fundamentals from EDINET (Turbo)...")
            try:
                edinet_fetcher = EdinetFetcher(self.context.config)
                xbrl_parser = XbrlParser()
                turbo_mgr = TurboAcquisitionManager(edinet_fetcher, xbrl_parser, self.context.config)
                
                # 直近 5 日分の書類を並列・差分スキャニング
                turbo_mgr.run_turbo_acquisition(days=5)
                
                # ブリッジによる DB 反映
                bridge = EdinetBridge()
                sync_count = bridge.bridge_all(purge_after=True)
                self.log_info(f"✅ EDINET sync completed. {sync_count} documents integrated.")
            except Exception as e:
                self.log_error(f"❌ EDINET sync failed (Skipped): {e}")

        # 1. ターゲット銘柄の特定
        target_codes = repo.get_all_codes()
        if self.context.limit:
            target_codes = target_codes[:self.context.limit]
        
        self.log_info(f"Targeting {len(target_codes)} stocks for market data acquisition.")

        # 2. DB から過去履歴を一括ロード (RSI等バッファ用)
        df_db_hist_all = market_repo.get_all_history_pl(months=3)

        # 3. Producer-Consumer パイプライン (Market Data 取得)
        result_queue = queue.Queue(maxsize=200)
        num_targets = len(target_codes)

        def _producer():
            """Hybrid Producer: 市場価格データのみを取得"""
            self.log_info("📡 Market Data Producer started...")
            try:
                FETCH_BATCH_SIZE = 20 # OHLCVのみなのでバッチサイズ拡大
                from concurrent.futures import ThreadPoolExecutor

                batches = [target_codes[i:i + FETCH_BATCH_SIZE] for i in range(0, num_targets, FETCH_BATCH_SIZE)]

                with ThreadPoolExecutor(max_workers=2) as executor:
                    for i, b in enumerate(batches):
                        # DB履歴が薄い場合は 1y、十分なら 2d (差分) を取得
                        is_db_thin = df_db_hist_all.height < (len(target_codes) * 10)
                        period_to_use = "1y" if is_db_thin else "2d"
                        
                        try:
                            # [v10] fetch_stock_data は内部で yf.download(threads=True) を呼ぶため、ここは軽量
                            hist_map = fetcher.fetch_stock_data(b, period=period_to_use, context=self.context)
                        except Exception as e:
                            self.log_error(f"Batch {i+1} failed: {e}")
                            continue

                        if hist_map:
                            for code, df_yf in hist_map.items():
                                df_db = df_db_hist_all.filter(pl.col("code") == code)
                                
                                if df_db.is_empty():
                                    df_final_pd = df_yf
                                else:
                                    df_yf_pl = pl.from_pandas(df_yf.reset_index())
                                    rename_map = {"index": "Date", "date": "Date"}
                                    for old, new in rename_map.items():
                                        if old in df_yf_pl.columns:
                                            df_yf_pl = df_yf_pl.rename({old: new})

                                    # 結合 & 重複排除
                                    df_final_pl = pl.concat([df_db, df_yf_pl.with_columns(pl.col("Date").cast(pl.Datetime))], how="diagonal").unique("Date").sort("Date")
                                    df_final_pd = df_final_pl.to_pandas()
                                
                                result_queue.put((code, df_final_pd))
            except Exception as e:
                self.log_error(f"Producer Fatal Error: {e}")
            finally:
                result_queue.put(None)

        threading.Thread(target=_producer, daemon=False).start()

        # 4. Consumer 集約
        all_data_map: Dict[str, Any] = {}
        processed_count = 0
        while True:
            item = result_queue.get(timeout=120)
            if item is None:
                break
            code, df_pd = item
            all_data_map[code] = df_pd
            processed_count += 1
            if processed_count % 100 == 0:
                self.log_info(f"Acquired market data for {processed_count}/{num_targets} stocks...")

        self.log_info(f"Data Acquisition completed. {len(all_data_map)} stocks ready for evaluation.")
        return all_data_map
