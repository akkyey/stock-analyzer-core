"""市場データフェッチャー

株価履歴、テクニカル指標、出来高などの市場データ取得に特化する。
[v1.0] YahooFetcher から物理的に分離。
"""

import logging
import random
import threading
import time
from typing import Any

import pandas as pd
import yfinance as yf

from src.utils.diagnostics import record_fetch_stat

from .base import FetcherBase
from .fetch_profile import resolve_fetch_profile

# ユーザー体験向上のため、未上場・欠落銘柄の個別 404/Missing エラーを抑制
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)


class MarketFetcher(FetcherBase):
    """株価・テクニカルデータの取得を担当するクラス。"""

    _warmed_up = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread_local = threading.local()
        self.lock = threading.Lock()
        # 取得したデータに含まれていた株式分割 {code: [(日付, 比率)]}
        self._detected_splits: dict[str, list[tuple[Any, float]]] = {}

    def pop_detected_splits(self) -> dict[str, list[tuple[Any, float]]]:
        """前回呼び出し以降の取得で見つかった株式分割を返し、記録を空にする。"""
        with self.lock:
            found, self._detected_splits = self._detected_splits, {}
        return found

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

        profile = resolve_fetch_profile(context)

        def download_sub_batch(
            sub_symbols: list[str], end_date: str | None = None
        ) -> tuple[pd.DataFrame, float]:
            t_sub_start = time.time()

            # [v30.4] Initial jitter to avoid synchronized requests
            time.sleep(random.uniform(0.1, 0.5))

            df = pd.DataFrame()
            record_fetch_stat(context, "sub_batches")
            # [v30.4] Increased retries with exponential backoff
            for attempt in range(profile.max_retries):
                try:
                    df = yf.download(
                        tickers=sub_symbols,
                        period=period,
                        end=end_date,
                        interval="1d",
                        group_by="ticker",
                        auto_adjust=True,
                        # 株式分割のイベントも同じリクエストで受け取る (Yahoo は日本株の
                        # 分割を過去に遡って調整しないため、自前で調整する)
                        actions=True,
                        threads=False,
                        progress=False,
                    )

                    if not df.empty:
                        record_fetch_stat(context, "sub_batches_ok")
                        # Success: プロファイル別の事前インターバル (429 の未然防止)
                        time.sleep(profile.inter_batch_sleep)
                        break

                    # If empty, it might be 429 or simply no data
                    record_fetch_stat(context, "empty_responses")
                    # 確実にデータがある銘柄で試し取得し、取れれば「レート制限ではなく、
                    # このバッチの銘柄にデータが無い (上場廃止・PRO Market 等)」と判断して
                    # 待機・再試行をしない (無駄なバックオフの回避)
                    if self._is_market_reachable(period, end_date):
                        record_fetch_stat(context, "empty_no_data_batches")
                        self.logger.info(
                            f"ℹ️ Batch {sub_symbols[:2]}... にはデータがありません (レート制限ではないため再試行しません)"
                        )
                        break
                    if attempt == profile.max_retries - 1:
                        # 最後の試行の後は待っても再試行しないため、待たずに諦める
                        self.logger.warning(
                            f"⚠️ Batch {sub_symbols[:2]}... empty after {profile.max_retries} attempts."
                        )
                        break
                    wait_time = (2**attempt) * profile.empty_backoff_base + random.uniform(1, 3)
                    self.logger.warning(
                        f"⚠️ Batch {sub_symbols[:2]}... empty. Backoff {wait_time:.1f}s (Attempt {attempt + 1}/{profile.max_retries})"
                    )
                    time.sleep(wait_time)

                except Exception as e:
                    record_fetch_stat(context, "download_exceptions")
                    if attempt == profile.max_retries - 1:
                        raise e
                    wait_time = (2**attempt) * profile.error_backoff_base + random.uniform(2, 5)
                    self.logger.error(
                        f"❌ Error downloading {sub_symbols[:2]}...: {e}. Retry in {wait_time:.1f}s"
                    )
                    time.sleep(wait_time)

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

        # 取得できなかった銘柄を集計 (429 によるバッチ欠落の可視化)
        requested = {sym.split(".")[0] for sym in full_symbols}
        missing = sorted(requested - set(results))
        if context is not None and hasattr(context, "config"):
            try:
                acc = context.config.setdefault("fetch_missing_codes", [])
                acc.extend(missing)
            except Exception:
                pass
        if missing:
            self.logger.warning(
                f"⚠️ [{profile.name}] 取得できなかった銘柄: {len(missing)}/{len(requested)} 件"
            )

        elapsed_method = time.time() - t_method_start
        if (
            context
            and hasattr(context, "perf_stats")
            and isinstance(context.perf_stats, dict)
        ):
            context.perf_stats["fetch_sec"] += elapsed_method

        return results

    # レート制限の判定に使う、常にデータがある銘柄 (トヨタ自動車)
    CANARY_SYMBOL = "7203.T"

    def _is_market_reachable(self, period: str, end_date: str | None) -> bool:
        """常にデータがある銘柄を 1 件取得できれば True (= レート制限を受けていない)。"""
        try:
            probe = yf.download(
                tickers=[self.CANARY_SYMBOL],
                period=period,
                end=end_date,
                interval="1d",
                auto_adjust=True,
                threads=False,
                progress=False,
            )
            return probe is not None and not probe.empty
        except Exception:
            return False

    def _extract_dfs_from_batch(
        self, all_hist: pd.DataFrame, full_symbols: list[str]
    ) -> dict[str, pd.DataFrame]:
        batch_results: dict[str, pd.DataFrame] = {}
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

                # 株式分割のイベント列を取り出し、履歴からは除く
                if not hist.empty and "Stock Splits" in hist.columns:
                    events = hist["Stock Splits"]
                    # 異常な比率 (Yahoo が 2e-07 などを返すことがある) は除く
                    events = events[
                        events.notna() & (events >= 0.02) & (events <= 100) & (events != 1)
                    ]
                    if len(events):
                        with self.lock:
                            self._detected_splits.setdefault(code, []).extend(
                                (ts.date() if hasattr(ts, "date") else ts, float(r))
                                for ts, r in events.items()
                            )
                if not hist.empty:
                    hist = hist.drop(
                        columns=[c for c in ("Dividends", "Stock Splits", "Capital Gains") if c in hist.columns]
                    )

                # 終値が空の行 (Yahoo が直近日を NaN で返すことがある) は採用しない。
                # 残すと DB 履歴との結合で同じ日付の正しい値を空で上書きし、
                # 1 日前のデータが「最新」として扱われてしまう
                if not hist.empty and "Close" in hist.columns:
                    hist = hist[hist["Close"].notna()]
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
