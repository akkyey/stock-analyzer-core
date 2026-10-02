"""データ取得フェーズ (Phase 1) - Turbo Fidelity Edition

1. EDINET Turbo 同期: 最新の財務データ (有報・四半報) を並列取得し DuckDB を更新。
2. Market Data 取得: yfinance から OHLCV を取得し、DB 履歴と結合。
"""

import queue
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd
import polars as pl

from src.fetcher.incremental import (
    ADJUSTMENT_TOLERANCE,
    FULL_PERIOD,
    REFETCH_PERIOD,
    adjustment_ratio,
    apply_split_adjustments,
    drop_unsettled_today,
    find_split_jump,
    merge_history,
    plan_period,
)
from src.orchestration.phases.base import BasePhase
from src.utils import get_current_time
from src.utils.diagnostics import record_fetch_stat


class AcquisitionPhase(BasePhase):
    """データ取得フェーズ (v10: yfinance financials 排除 / EDINET Turbo 統合)"""

    # 流動性指標 (直近20日平均売買代金) の算出に必要な最低履歴行数。
    # これ未満の銘柄は全期間 (1y) を取得する
    MIN_HISTORY_ROWS = 20
    # 差分更新でテクニカル指標の計算に使う DB 履歴の長さ (初回の 1y 取得と揃える)
    HISTORY_MONTHS = 13
    FETCH_BATCH_SIZE = 20  # OHLCVのみなのでバッチサイズ拡大
    # Producer から 1 件も届かない状態をどこまで待つか (秒)。Colab プロファイルの
    # 再試行・待機 (1 バッチ最大約 8 分) より長くし、取得途中で打ち切らない
    NO_ITEM_TIMEOUT_SEC = 900

    # EDINET の走査期間の上限 (日)。前回の走査から間が空いても、これより前は遡らない
    MAX_EDINET_SCAN_DAYS = 365
    EDINET_SCAN_META_KEY = "edinet_last_scan_date"
    # 旧版の EDINET 取り込みで NULL になった項目の復旧 (実施済みかどうかの記録キー)
    RESTORE_META_KEY = "fundamentals_restored_parser_v2"
    RESTORE_COLUMNS = (
        "net_profit",
        "prev_net_profit",
        "sales",
        "operating_income",
        "operating_margin",
        "eps",
        "bps",
        "dps",
        "shares_outstanding",
    )

    def _edinet_scan_days(self, repo: Any, configured: int) -> int:
        """EDINET の走査日数。前回の走査日から今日までを必ず含める (上限あり)。

        固定日数だけを走査すると、実行間隔がそれより空いた場合に、その間に提出された
        書類を取り逃し、翌年の提出まで反映されない。
        """
        try:
            last = repo.get_meta(self.EDINET_SCAN_META_KEY)
        except Exception:
            last = None
        days = configured
        if last:
            try:
                last_d = datetime.strptime(last[:10], "%Y-%m-%d").date()
                gap = (get_current_time().date() - last_d).days + 1
                days = max(days, gap)
            except ValueError:
                pass
        return min(days, self.MAX_EDINET_SCAN_DAYS)

    def _record_edinet_scan(self, repo: Any, failed_dates: list) -> None:
        """走査済みの日付を記録する。一覧を取得できなかった日があれば、次回はその日から走査する。"""
        today = get_current_time().date()
        if failed_dates:
            earliest = min(datetime.strptime(d, "%Y-%m-%d").date() for d in failed_dates)
            mark = earliest - timedelta(days=1)
            self.log_warn(
                f"EDINET の書類一覧を取得できなかった日が {len(failed_dates)} 日あります。次回 {earliest} から走査し直します。"
            )
        else:
            mark = today
        try:
            previous = repo.get_meta(self.EDINET_SCAN_META_KEY)
            # 失敗で記録を巻き戻す場合も、以前の記録より後ろへは進めない
            if failed_dates and previous and previous[:10] < mark.isoformat():
                mark = datetime.strptime(previous[:10], "%Y-%m-%d").date()
            repo.set_meta(self.EDINET_SCAN_META_KEY, mark.isoformat())
        except Exception as e:
            self.log_warn(f"EDINET の走査日の記録に失敗しました: {e}")

    @staticmethod
    def _last_history_date(df_db: Any) -> Optional[date]:
        """DB 履歴の最終日 (日付型でない場合は None)。"""
        if df_db is None or df_db.is_empty() or "Date" not in df_db.columns:
            return None
        if not df_db.schema["Date"].is_temporal():
            return None
        last = df_db.select(pl.col("Date").cast(pl.Date).max()).item()
        return last if isinstance(last, date) else None

    def _plan_fetch_batches(
        self,
        target_codes: list,
        db_hist_by_code: Dict[Any, Any],
        today: Optional[date] = None,
    ) -> list:
        """銘柄ごとに取得期間を決め、(codes, period) のバッチ列を返す。

        - 履歴が MIN_HISTORY_ROWS 未満 (新規・過去の取得欠落) → "1y"
        - 十分 → DB の最終日から今日までの欠落営業日数に合わせた差分 ("2d"〜"21d")。
          欠落が 1 か月を超える場合は "1y"
        - config["is_first_run"] が True の場合は全銘柄 "1y"

        実行間隔が空いても欠落日を取り込めるよう、期間は最終日から決める
        (固定の 2 日では、間の日付が永久に欠落していた)。
        """
        cfg = getattr(self.context, "config", None) or {}
        force_full = bool(cfg.get("is_first_run", False))
        today = today or get_current_time().date()

        by_period: Dict[str, list] = {}
        for code in target_codes:
            df_db = db_hist_by_code.get((code,))
            rows = 0 if df_db is None else df_db.height
            if force_full or rows < self.MIN_HISTORY_ROWS:
                period = FULL_PERIOD
            else:
                last = self._last_history_date(df_db)
                # 日付型でない履歴 (テスト用の簡易データ等) は従来どおり 2d
                period = plan_period(last, today) if last is not None else "2d"
            by_period.setdefault(period, []).append(code)

        size = self.FETCH_BATCH_SIZE
        batches: list[tuple[list, str]] = []
        for period, codes in by_period.items():
            batches.extend(
                (codes[i : i + size], period) for i in range(0, len(codes), size)
            )
        summary = ", ".join(f"{len(c)} stocks -> {p}" for p, c in by_period.items())
        self.log_info(f"Fetch plan: {summary or 'none'} ({len(batches)} batches)")
        record_fetch_stat(
            self.context, "plan_stocks_1y", len(by_period.get(FULL_PERIOD, []))
        )
        record_fetch_stat(
            self.context,
            "plan_stocks_2d",
            sum(len(c) for p, c in by_period.items() if p != FULL_PERIOD),
        )
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

        # 1-1b. 銘柄マスタの月次更新 (JPX 公式一覧との同期: 新規上場の追加・上場廃止の除外)
        fetcher_cfg0 = self.context.config.get("fetcher", {}) or {}
        if fetcher_cfg0.get("refresh_stock_master", True):
            from src.services.stock_master import refresh_stock_master

            res = refresh_stock_master(repo, fetcher.jpx_fetcher)
            if res.status == "updated":
                self.log_info(
                    f"🔄 銘柄マスタを更新しました: 新規 {res.added} / 上場廃止 {res.delisted} "
                    f"/ 再上場 {res.relisted} 銘柄"
                )
                record_fetch_stat(self.context, "master_added", res.added)
                record_fetch_stat(self.context, "master_delisted", res.delisted)
                target_codes = repo.get_all_codes()
            elif res.status in ("skipped_unsafe", "failed"):
                self.log_warn(f"銘柄マスタの更新をスキップしました: {res.detail}")

        # 1-2. 財務データの初期シード (全件ベースラインデータ未登録時はバンドルされたシードデータから自動投入)
        funda_repo = FundamentalsRepository(repo)
        seed_parquet = (
            Path(__file__).resolve().parent.parent.parent
            / "resources"
            / "fundamentals_seed.parquet"
        )
        if funda_repo.get_count() < 3000:
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

        # 1-3. 既存 DB への成長率の補完 (旧スキーマでは成長率がシードから取り込まれていなかった)
        try:
            backfilled = funda_repo.backfill_growth_from_seed(seed_parquet)
            if backfilled:
                self.log_info(f"ℹ️ 成長率を {backfilled} 銘柄分、シードから補完しました。")
        except Exception as e:
            self.log_error(f"❌ 成長率の補完に失敗しました (継続): {e}")

        # 1-4. 旧版の EDINET 取り込みで消えた財務値の復旧 (1 回だけ)。
        # 旧版は半期報告書などで取れなかった項目を NULL で上書きしていた。
        # 消えた項目のうち、株価に依存しない値だけをシードから補う (DB に値がある項目は変更しない)
        try:
            if repo.get_meta(self.RESTORE_META_KEY) is None:
                restored = funda_repo.restore_missing_from_seed(
                    seed_parquet, self.RESTORE_COLUMNS
                )
                repo.set_meta(self.RESTORE_META_KEY, get_current_time().date().isoformat())
                if restored:
                    self.log_info(f"ℹ️ 欠損していた財務値を {restored} 銘柄分、シードから補完しました。")
        except Exception as e:
            self.log_error(f"❌ 財務値の復旧に失敗しました (継続): {e}")

        # [Phase 0/1] 財務データの正典同期 (Fundamental Truth Sync)
        fetcher_cfg = self.context.config.get("fetcher", {})
        if fetcher_cfg.get("enable_edinet_turbo", True):
            scan_days = self._edinet_scan_days(
                repo, int(fetcher_cfg.get("edinet_scan_days", 30))
            )
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
                self._record_edinet_scan(repo, turbo_mgr.failed_dates)

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

        # 市場データを提供しない銘柄 (PRO Market 等) は、連続して取得できなかった場合に
        # 一定期間 (30日) 再取得を止める。除外銘柄リストには「取得不能」として残る
        from src.services.stock_master import get_cooling_codes, update_no_data_tracking

        try:
            cooling = get_cooling_codes(repo)
        except Exception as e:
            self.log_warn(f"再取得抑制の確認に失敗しました (全銘柄を取得): {e}")
            cooling = set()
        if cooling:
            target_codes = [c for c in target_codes if c not in cooling]
            self.log_info(
                f"⏭️ 市場データが提供されない銘柄 {len(cooling)} 件は再取得を一時停止しています (30日ごとに再確認)。"
            )
            record_fetch_stat(self.context, "skipped_no_data_stocks", len(cooling))

        print(
            f"📡 全 {len(target_codes)} 銘柄の市場データ取得を開始します...",
            flush=True,
        )

        # 2. DB から過去履歴を一括ロード (テクニカル指標の計算用)
        # 初回 (1y 取得) と同じ長さの履歴で計算しないと、MA75 (75 営業日必要) が計算できず、
        # RSI 等の値も初回と 2 回目以降で食い違う。3 か月 (約 63 営業日) では不足していた
        df_db_hist_all = market_repo.get_all_history_pl(months=self.HISTORY_MONTHS)

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

        # 当日足の確定判定に使う現在時刻 (JST)。バックデート指定時は判定しない
        now_jst: Optional[datetime] = (
            None if self.context.config.get("target_date") else get_current_time()
        )
        history_cutoff = df_db_hist_all.select(pl.col("Date").min()).item() if (
            not df_db_hist_all.is_empty() and "Date" in df_db_hist_all.columns
        ) else None

        def _to_polars(df_yf: Any) -> pl.DataFrame:
            # 指摘12: pandas への不要な往復変換を撤廃し Polars DataFrame のまま保持
            df_pl = pl.from_pandas(df_yf.reset_index()) if isinstance(df_yf, pd.DataFrame) else df_yf
            for old in ("index", "date"):
                if old in df_pl.columns:
                    df_pl = df_pl.rename({old: "Date"})
            if now_jst is not None:
                df_pl = drop_unsettled_today(df_pl, now_jst)
            return df_pl

        def _emit(code: str, df_final_pl: pl.DataFrame) -> bool:
            # Consumer 停止時の永久ブロック防止のためタイムアウト付きで put
            while not stop_event.is_set():
                try:
                    result_queue.put((code, df_final_pl), timeout=2.0)
                    return True
                except queue.Full:
                    continue
            return False

        def _producer():
            """Hybrid Producer: 市場価格データのみを取得"""
            self.log_info("📡 Market Data Producer started...")
            # 分割・配当落ちで DB の保存値と調整がずれた銘柄 (全期間を取り直す)
            readjust: Dict[str, pl.DataFrame] = {}
            # 株式分割 {code: [(日付, 比率)]}。記録済みの分と、今回の取得で見つかった分
            splits_by_code: Dict[str, list] = {}
            new_splits: list = []
            try:
                recorded = repo.load_splits()
                if isinstance(recorded, pl.DataFrame):
                    for code, d, r in recorded.iter_rows():
                        splits_by_code.setdefault(str(code), []).append((d, r))
            except Exception as e:
                self.log_warn(f"株式分割の記録を読めませんでした (継続): {e}")

            # 今回の取得で見つかった分割イベント (株価の段差を確認するまでは記録しない)
            candidates: Dict[str, list] = {}

            def _collect_splits() -> None:
                found = fetcher.market_fetcher.pop_detected_splits()
                if not isinstance(found, dict):
                    return
                for code, events in found.items():
                    known = splits_by_code.get(code, [])
                    for d, r in events:
                        if all(abs((d - kd).days) > 10 for kd, _ in known):
                            candidates.setdefault(code, []).append((d, r))

            def _adjust(code: str, df: pl.DataFrame) -> pl.DataFrame:
                # Yahoo は日本株の分割を過去に遡って調整しないため、分割前の値を自前で調整する。
                # 新しいイベントは、履歴に比率どおりの段差がある場合だけ分割として記録する
                # (Yahoo は異常な分割イベントを返すことがあるため)
                for d, r in candidates.pop(code, []):
                    if find_split_jump(df, d, r) is not None:
                        splits_by_code.setdefault(code, []).append((d, r))
                        new_splits.append((code, d, r))
                    else:
                        self.log_info(f"分割イベントを採用しませんでした ({code} {d} 比率 {r:g}): 株価に対応する段差がありません")
                events = splits_by_code.get(code)
                return apply_split_adjustments(df, events) if events else df
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
                    _collect_splits()

                    for code, df_yf in (hist_map or {}).items():
                        if stop_event.is_set():
                            break
                        df_yf_pl = _to_polars(df_yf)
                        if df_yf_pl.is_empty():
                            continue
                        df_db = db_hist_by_code.get((code,))
                        if period_to_use != FULL_PERIOD and df_db is not None:
                            ratio = adjustment_ratio(df_db, df_yf_pl)
                            if ratio is not None and abs(ratio - 1.0) > ADJUSTMENT_TOLERANCE:
                                readjust[code] = df_yf_pl
                                continue
                        _emit(code, _adjust(code, merge_history(df_db, df_yf_pl)))

                # 調整がずれた銘柄は、DB の履歴を使わず取り直した値だけで計算する
                # (保存時に DB の過去の値も取り直した値で上書きされる)
                if readjust and not stop_event.is_set():
                    self.log_info(
                        f"🔁 配当落ち等で過去の株価の調整が変わった {len(readjust)} 銘柄は、全期間を取り直します。"
                    )
                    record_fetch_stat(self.context, "readjusted_stocks", len(readjust))
                    size = self.FETCH_BATCH_SIZE
                    readjust_codes = list(readjust)
                    for j in range(0, len(readjust_codes), size):
                        if stop_event.is_set():
                            break
                        chunk = readjust_codes[j : j + size]
                        try:
                            hist_map = fetcher.fetch_stock_data(
                                chunk, period=REFETCH_PERIOD, context=self.context
                            )
                        except Exception as e:
                            self.log_error(f"Readjust batch failed: {e}")
                            hist_map = {}
                        _collect_splits()
                        for code in chunk:
                            df_yf = (hist_map or {}).get(code)
                            df_new = _to_polars(df_yf) if df_yf is not None else None
                            if df_new is None or df_new.is_empty():
                                # 取り直せなかった場合は、ずれを含むが従来どおり結合して評価を継続
                                _emit(code, _adjust(code, merge_history(db_hist_by_code.get((code,)), readjust[code])))
                                continue
                            if history_cutoff is not None:
                                df_new = df_new.filter(
                                    pl.col("Date").cast(pl.Datetime) >= pl.lit(history_cutoff).cast(pl.Datetime)
                                )
                            _emit(code, _adjust(code, df_new))
                # 株式分割を記録する (株価の調整と、1 株当たりの財務指標の補正に使う)
                if new_splits:
                    try:
                        repo.save_splits(new_splits)
                        self.log_info(
                            "✂️ 株式分割を記録しました: "
                            + ", ".join(f"{c} ({d} 1:{r:g})" for c, d, r in new_splits[:20])
                            + (f" ほか {len(new_splits) - 20} 件" if len(new_splits) > 20 else "")
                        )
                    except Exception as e:
                        self.log_warn(f"株式分割の記録に失敗しました (継続): {e}")
                record_fetch_stat(self.context, "splits_recorded", len(new_splits))
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
        last_item_at = time.monotonic()

        while True:
            try:
                item = result_queue.get(timeout=60)
            except queue.Empty:
                if not producer_thread.is_alive():
                    self.log_warn("Producer thread has finished. Completing consumption.")
                    break
                waited = time.monotonic() - last_item_at
                if waited < self.NO_ITEM_TIMEOUT_SEC:
                    # 再試行の待機中 (レート制限のバックオフ等)。打ち切らずに待つ
                    self.log_info(
                        f"Producer is still working. Waiting for items ({waited:.0f}s / {self.NO_ITEM_TIMEOUT_SEC}s)..."
                    )
                    continue
                err_msg = "Producer timed out exceeding max wait. Proceeding with collected data."
                self.log_error(err_msg)
                if hasattr(self.context, "add_error"):
                    self.context.add_error(err_msg)
                stop_event.set()  # Producer スレッドに停止シグナルを通知
                producer_thread.join(timeout=5)
                break

            if item is None:
                break

            last_item_at = time.monotonic()
            code, df_data = item
            all_data_map[code] = df_data
            processed_count += 1
            if processed_count % 500 == 0 or processed_count == num_targets:
                print(
                    f"   📊 市場データ取得進行中: {processed_count}/{num_targets} 銘柄完了...",
                    flush=True,
                )

        stop_event.set()  # 正常終了時も確実にセット

        try:
            tracked = update_no_data_tracking(
                repo,
                fetched=set(all_data_map),
                attempted=list(target_codes),
                codes_with_history={str(k[0]) for k in db_hist_by_code},
            )
            record_fetch_stat(self.context, "no_data_failed", tracked["failed"])
            record_fetch_stat(self.context, "no_data_cooled", tracked["cooled"])
        except Exception as e:
            self.log_warn(f"取得不能銘柄の記録に失敗しました (継続): {e}")

        print(
            f"✅ 市場データ取得完了: 全 {len(all_data_map)} 銘柄のデータを準備しました。",
            flush=True,
        )
        return all_data_map
