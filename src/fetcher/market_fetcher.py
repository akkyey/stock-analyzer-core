"""市場データフェッチャー

株価履歴、テクニカル指標、出来高などの市場データ取得に特化する。
[v1.0] YahooFetcher から物理的に分離。
"""

import random
import threading
import time
from typing import Any

import logging
import pandas as pd
import yfinance as yf
# [v28.4] Level set to WARNING to catch rate limits without clogging logs with DEBUG
logging.getLogger("yfinance").setLevel(logging.WARNING)

from .base import FetcherBase
from .technical import calc_technical_indicators


class MarketFetcher(FetcherBase):
    """株価・テクニカルデータの取得を担当するクラス。"""

    _warmed_up = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread_local = threading.local()
        self.lock = threading.Lock()


    def _warm_up(self) -> None:
        if MarketFetcher._warmed_up:
            return
        try:
            # self.logger.info("🔥 Warming up MarketFetcher session...")
            # yf.download("7203.T", period="1d", progress=False, session=self.session)
            MarketFetcher._warmed_up = True
            # time.sleep(0.5)
        except Exception as e:
            self.logger.warning(f"⚠️ Market-fill warm-up failed: {e}")
            MarketFetcher._warmed_up = True

    def fetch_market_data(
        self,
        ticker_symbols: list[str],
        period: str = "2y",
        threads: bool = True,
        context: Any = None,
    ) -> dict[str, pd.DataFrame]:
        """並列ダウンロードを実行し、銘柄ごとの生データ (DataFrame) を返す。"""
        if not ticker_symbols:
            return {}

        full_symbols = [f"{c}.T" if "." not in c else c for c in ticker_symbols]
        return self._parallel_download_and_raw_df(
            full_symbols, period, threads, context=context
        )

    def _parallel_download_and_raw_df(
        self, full_symbols: list[str], period: str, threads: bool, context: Any = None
    ) -> dict[str, pd.DataFrame]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        SUB_BATCH_SIZE = 20
        # [v30.4] ユーザー要請に基づき、1 多重（シングルスレッド）に制限
        MAX_WORKERS = 1

        if not MarketFetcher._warmed_up:
            self._warm_up()

        def download_sub_batch(
            sub_symbols: list[str], end_date: str | None = None
        ) -> tuple[pd.DataFrame, float]:
            t_sub_start = time.time()
            
            # [v30.4] Initial jitter to avoid synchronized requests
            time.sleep(random.uniform(0.1, 0.5))

            df = pd.DataFrame()
            # [v30.4] Increased retries with exponential backoff
            for attempt in range(3):
                try:
                    df = yf.download(
                        tickers=sub_symbols,
                        period=period,
                        end=end_date,
                        interval="1d",
                        group_by="ticker",
                        auto_adjust=True,
                        threads=False,
                        progress=False,
                    )
                    
                    if not df.empty:
                        # Success: small break before returning
                        time.sleep(0.2)
                        break
                    
                    # If empty, it might be 429 or simply no data
                    wait_time = (2 ** attempt) * 5 + random.uniform(1, 3)
                    self.logger.warning(
                        f"⚠️ Batch {sub_symbols[:2]}... empty. Backoff {wait_time:.1f}s (Attempt {attempt+1}/3)"
                    )
                    time.sleep(wait_time)
                    
                except Exception as e:
                    wait_time = (2 ** attempt) * 10 + random.uniform(2, 5)
                    self.logger.error(
                        f"❌ Error downloading {sub_symbols[:2]}...: {e}. Retry in {wait_time:.1f}s"
                    )
                    time.sleep(wait_time)
                    if attempt == 2:
                        raise e
                        
            return df, time.time() - t_sub_start

        t_method_start = time.time()
        results = {}
        sub_batches = [
            full_symbols[i : i + SUB_BATCH_SIZE]
            for i in range(0, len(full_symbols), SUB_BATCH_SIZE)
        ]

        # [v29.3] バックデート(target_date)の考慮
        end_date_str = None
        if context and hasattr(context, "config"):
            target_date = context.config.get("target_date")
            if target_date:
                from datetime import datetime, timedelta

                target_dt = datetime.strptime(target_date, "%Y-%m-%d")
                # [NOTE] yfinance の end は「その日を含まない」ため +1日 する
                end_date_str = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_batch = {
                executor.submit(download_sub_batch, batch, end_date_str): batch
                for batch in sub_batches
            }

            for future in as_completed(future_to_batch):
                batch_symbols = future_to_batch[future]
                try:
                    all_hist, dl_inner_time = future.result()
                    batch_dfs = self._extract_dfs_from_batch(all_hist, batch_symbols)
                    with self.lock:
                        results.update(batch_dfs)
                except Exception as e:
                    self.logger.warning(
                        f"⚠️ Market sub-batch fetch issue for {batch_symbols[:3]}: {e}"
                    )

        elapsed_method = time.time() - t_method_start
        if (
            context
            and hasattr(context, "perf_stats")
            and isinstance(context.perf_stats, dict)
        ):
            context.perf_stats["fetch_sec"] += elapsed_method

        return results

    def _extract_dfs_from_batch(
        self, all_hist: pd.DataFrame, full_symbols: list[str]
    ) -> dict[str, pd.DataFrame]:
        batch_results = {}
        if all_hist is None or all_hist.empty:
            self.logger.debug("--- [DEBUG] all_hist is empty or None ---")
            return batch_results

        self.logger.debug(f"--- [DEBUG] all_hist type: {type(all_hist)} ---")
        self.logger.debug(f"--- [DEBUG] all_hist columns: {all_hist.columns} ---")

        for ticker_symbol in full_symbols:
            code = ticker_symbol.split(".")[0]
            try:
                hist = pd.DataFrame()
                # Case 1: MultiIndex (Multiple tickers or group_by="ticker" explicit)
                if isinstance(all_hist.columns, pd.MultiIndex):
                    if ticker_symbol in all_hist.columns.levels[0]:
                        hist = all_hist[ticker_symbol].dropna(how="all")
                # Case 2: Single ticker results (index is just Open, High, Low...)
                elif len(full_symbols) == 1:
                    hist = all_hist.dropna(how="all")
                # Case 3: Empty or malformed
                else:
                    self.logger.debug(
                        f"--- [DEBUG] skipping {ticker_symbol} (MultiIndex={isinstance(all_hist.columns, pd.MultiIndex)}) ---"
                    )
                    continue

                if not hist.empty and "Close" in hist.columns:
                    batch_results[code] = hist
                else:
                    self.logger.debug(
                        f"--- [DEBUG] {ticker_symbol} extract empty or no Close ---"
                    )

            except Exception as e:
                import traceback

                self.logger.warning(f"⚠️ Error extracting for {code}: {e}")
                self.logger.debug(traceback.format_exc())

        return batch_results
