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
    FULL_PERIOD,
    REFETCH_PERIOD,
    SplitTracker,
    drop_unsettled_today,
    has_adjustment_mismatch,
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
    # 株式分割を記録するための全期間の取り直しを済ませたか (DB ごとに 1 回)
    SPLIT_BACKFILL_META_KEY = "split_history_backfilled"
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
                last_d = date.fromisoformat(last[:10])
                gap = (get_current_time().date() - last_d).days + 1
                days = max(days, gap)
            except ValueError:
                pass
        return min(days, self.MAX_EDINET_SCAN_DAYS)

    def _record_edinet_scan(self, repo: Any, failed_dates: list) -> None:
        """走査済みの日付を記録する。一覧を取得できなかった日があれば、次回はその日から走査する。"""
        today = get_current_time().date()
        if failed_dates:
            earliest = min(date.fromisoformat(d) for d in failed_dates)
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
                mark = date.fromisoformat(previous[:10])
            repo.set_meta(self.EDINET_SCAN_META_KEY, mark.isoformat())
        except Exception as e:
            self.log_warn(f"EDINET の走査日の記録に失敗しました: {e}")

    def _refetch_readjusted(
        self,
        fetcher: Any,
        readjust: Dict[str, pl.DataFrame],
        db_hist_by_code: Dict[Any, Any],
        history_cutoff: Any,
        to_polars: Any,
        emit: Any,
        collect_splits: Any,
        splits: SplitTracker,
        stop_event: threading.Event,
    ) -> None:
        """調整がずれた銘柄を全期間取り直し、DB の履歴を使わずに取り直した値だけで渡す。

        保存時に DB の過去の値も取り直した値で上書きされる。取り直せなかった銘柄は、
        ずれを含むが従来どおり DB の履歴と結合して評価を継続する。
        """
        self.log_info(
            f"🔁 配当落ち等で過去の株価の調整が変わった {len(readjust)} 銘柄は、全期間を取り直します。"
        )
        record_fetch_stat(self.context, "readjusted_stocks", len(readjust))
        codes = list(readjust)
        for j in range(0, len(codes), self.FETCH_BATCH_SIZE):
            if stop_event.is_set():
                break
            chunk = codes[j : j + self.FETCH_BATCH_SIZE]
            try:
                hist_map = fetcher.fetch_stock_data(
                    chunk, period=REFETCH_PERIOD, context=self.context
                ) or {}
            except Exception as e:
                self.log_error(f"Readjust batch failed: {e}")
                hist_map = {}
            collect_splits()
            for code in chunk:
                df_yf = hist_map.get(code)
                df_new = to_polars(df_yf) if df_yf is not None else None
                if df_new is None or df_new.is_empty():
                    merged = merge_history(db_hist_by_code.get((code,)), readjust[code])
                    emit(code, splits.adjust(code, merged))
                    continue
                if history_cutoff is not None:
                    df_new = df_new.filter(
                        pl.col("Date").cast(pl.Datetime)
                        >= pl.lit(history_cutoff).cast(pl.Datetime)
                    )
                emit(code, splits.adjust(code, df_new))

    def _save_splits(self, repo: Any, splits: SplitTracker) -> None:
        """今回見つかった株式分割を記録する (株価の調整と、1 株当たり指標の補正に使う)。"""
        new = splits.new
        if new:
            try:
                repo.save_splits(new)
                shown = ", ".join(f"{c} ({d} 1:{r:g})" for c, d, r in new[:20])
                more = f" ほか {len(new) - 20} 件" if len(new) > 20 else ""
                self.log_info(f"✂️ 株式分割を記録しました: {shown}{more}")
            except Exception as e:
                self.log_warn(f"株式分割の記録に失敗しました (継続): {e}")
        record_fetch_stat(self.context, "splits_recorded", len(new))

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
        force_full: bool = False,
    ) -> list:
        """銘柄ごとに取得期間を決め、(codes, period) のバッチ列を返す。

        - 履歴が MIN_HISTORY_ROWS 未満 (新規・過去の取得欠落) → "1y"
        - 十分 → DB の最終日から今日までの欠落営業日数に合わせた差分 ("2d"〜"21d")。
          欠落が 1 か月を超える場合は "1y"
        - config["is_first_run"] が True、または force_full の場合は全銘柄 "1y"

        実行間隔が空いても欠落日を取り込めるよう、期間は最終日から決める
        (固定の 2 日では、間の日付が永久に欠落していた)。
        """
        cfg = getattr(self.context, "config", None) or {}
        force_full = force_full or bool(cfg.get("is_first_run", False))
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
            "plan_stocks_2d",  # 差分 (2d〜21d) 全体の銘柄数。キー名は互換のため維持
            sum(len(c) for p, c in by_period.items() if p != FULL_PERIOD),
        )
        record_fetch_stat(self.context, "plan_batches", len(batches))
        return batches

    def execute(self, df: Optional[pl.DataFrame] = None) -> Any:
        self.log_info("🚀 Starting Turbo Data Acquisition Phase...")

        from src.fetcher.facade import DataFetcher
        from src.repositories.duck_repository import DuckDBRepository
        from src.repositories.market_data_repository import MarketDataRepository

        repo = DuckDBRepository()
        fetcher = DataFetcher(self.context.config)
        market_repo = MarketDataRepository()

        target_codes = self._ensure_stock_master(repo, fetcher)
        self._ensure_fundamentals(repo)
        self._sync_edinet(repo)
        target_codes = self._select_targets(repo, target_codes)
        return self._fetch_market_data(repo, fetcher, market_repo, target_codes)

    def _ensure_stock_master(self, repo: Any, fetcher: Any) -> list[str]:
        """銘柄マスタを用意し (未登録なら JPX から初期登録、月次で JPX と同期)、取得対象のコードを返す。"""
        # 1. ターゲット銘柄の特定 (未登録時は JPX から自動初期シード)
        target_codes: list[str] = list(repo.get_all_codes() or [])
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
                    target_codes = list(repo.get_all_codes() or [])
                    print(
                        f"   ✅ 東証(JPX)公式リストから銘柄マスタに {len(target_codes)} 銘柄を初期登録しました。",
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
                target_codes = list(repo.get_all_codes() or [])
            elif res.status in ("skipped_unsafe", "failed"):
                self.log_warn(f"銘柄マスタの更新をスキップしました: {res.detail}")

        return target_codes

    def _ensure_fundamentals(self, repo: Any) -> None:
        """財務データの初期シード・成長率の補完・旧版で消えた値の復旧 (いずれも必要な場合のみ)。"""
        from pathlib import Path

        from src.repositories.fundamentals_repository import FundamentalsRepository

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
                    all_master_codes = list(repo.get_all_codes() or [])
                    diff_seed = len(all_master_codes) - len(df_seed) if len(all_master_codes) > len(df_seed) else 0
                    diff_note = f" (直近の新規上場等 {diff_seed} 銘柄を除く)" if diff_seed > 0 else ""
                    print(
                        f"   ✅ 財務シードデータから {len(df_seed)} 銘柄を初期登録しました{diff_note}。",
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


    def _sync_edinet(self, repo: Any) -> None:
        """EDINET から最新の財務データを取り込む (前回の走査日以降の書類)。"""
        from src.fetcher.edinet_fetcher import (
            EdinetAuthenticationError,
            EdinetFetcher,
            mask_api_key,
        )
        from src.fetcher.turbo_acquisition import TurboAcquisitionManager
        from src.fetcher.xbrl_parser import XbrlParser
        from src.services.edinet_bridge import EdinetBridge

        # [Phase 0/1] 財務データの正典同期 (Fundamental Truth Sync)
        fetcher_cfg = self.context.config.get("fetcher", {})
        if fetcher_cfg.get("enable_edinet_turbo", True):
            scan_days = self._edinet_scan_days(
                repo, int(fetcher_cfg.get("edinet_scan_days", 30))
            )
            print(f"   🔍 EDINET 最新開示の同期確認中 (過去 {scan_days} 日分)...", flush=True)
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
                print(f"   ✨ EDINET 同期完了 ({sync_count} 件の開示書類を反映 / キャッシュ有効)", flush=True)
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


    def _select_targets(self, repo: Any, target_codes: list) -> list:
        """取得対象を、件数の上限と再取得の一時停止 (市場データが提供されない銘柄) で絞る。"""
        if self.context.limit:
            target_codes = target_codes[: self.context.limit]

        # 市場データを提供しない銘柄 (PRO Market 等) は、連続して取得できなかった場合に
        # 一定期間 (30日) 再取得を止める。除外銘柄リストには「取得不能」として残る
        from src.services.stock_master import get_cooling_codes

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

        return target_codes

    def _fetch_market_data(
        self, repo: Any, fetcher: Any, market_repo: Any, target_codes: list
    ) -> Dict[str, Any]:
        """株価を取得し、DB の履歴と結合・株式分割を調整した銘柄ごとの履歴を返す。"""
        from src.services.stock_master import update_no_data_tracking

        all_master_count = len(list(repo.get_all_codes() or []))
        excluded_count = all_master_count - len(target_codes) if all_master_count > len(target_codes) else 0
        if self.context.limit:
            print(
                f"   ℹ️ 市場データ取得対象: {len(target_codes)} 銘柄 (指定上限: {self.context.limit} 件 / マスタ全 {all_master_count} 銘柄中)",
                flush=True,
            )
        elif excluded_count > 0:
            print(
                f"   ℹ️ 市場データ取得対象: {len(target_codes)} 銘柄 (株価配信のない PRO Market 等 {excluded_count} 銘柄を除外)",
                flush=True,
            )
        print(
            f"📡 全 {len(target_codes)} 銘柄の市場データ取得を開始します...",
            flush=True,
        )

        # 1-5. 株式分割の記録が無い DB (v1.3.0 より前に作成) は、1 回だけ全銘柄を全期間で取り直す。
        # 過去 1 年の分割を記録し (1 株当たり指標の補正用)、履歴に残った分割の段差を調整するため
        try:
            needs_split_backfill = repo.get_meta(self.SPLIT_BACKFILL_META_KEY) is None
        except Exception:
            needs_split_backfill = False
        if needs_split_backfill and not self.context.config.get("is_first_run"):
            self.log_info(
                "ℹ️ 株式分割の記録を作るため、今回は全銘柄の株価を 1 年分取り直します (初回のみ)。"
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
            history_cutoff = df_db_hist_all.select(pl.col("Date").min()).item()
        else:
            db_hist_by_code = {}
            history_cutoff = None
        del df_db_hist_all  # 銘柄別に分けた後は不要 (13 か月分を二重に持たない)

        num_targets = len(target_codes)

        # 指摘11: yfinance のレート制限回避と内部マルチスレッド (yf.download の threads=True) を
        # 最大限活かすため、バッチ単位で同期的に直列取得（外部の ThreadPoolExecutor は不要）
        stop_event = threading.Event()

        # 当日足の確定判定に使う現在時刻 (JST)。バックデート指定時は判定しない
        now_jst: Optional[datetime] = (
            None if self.context.config.get("target_date") else get_current_time()
        )

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
            """Producer: 市場価格を取得し、DB 履歴との結合・株式分割の調整をして Consumer に渡す"""
            self.log_info("📡 Market Data Producer started...")
            # 配当落ち等で DB の保存値と調整がずれた銘柄 (全期間を取り直す)
            readjust: Dict[str, pl.DataFrame] = {}
            try:
                recorded = repo.load_splits()
                splits = SplitTracker(recorded if isinstance(recorded, pl.DataFrame) else None)
            except Exception as e:
                self.log_warn(f"株式分割の記録を読めませんでした (継続): {e}")
                splits = SplitTracker()

            def _collect_splits() -> None:
                found = fetcher.market_fetcher.pop_detected_splits()
                if isinstance(found, dict):
                    splits.add_events(found)

            try:
                batches = self._plan_fetch_batches(
                    target_codes, db_hist_by_code, force_full=needs_split_backfill
                )

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
                        if (
                            period_to_use != FULL_PERIOD
                            and df_db is not None
                            and has_adjustment_mismatch(df_db, df_yf_pl)
                        ):
                            readjust[code] = df_yf_pl
                            continue
                        _emit(code, splits.adjust(code, merge_history(df_db, df_yf_pl)))

                if readjust and not stop_event.is_set():
                    self._refetch_readjusted(
                        fetcher, readjust, db_hist_by_code, history_cutoff,
                        _to_polars, _emit, _collect_splits, splits, stop_event,
                    )
                self._save_splits(repo, splits)
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
        all_data_map = self._consume(result_queue, producer_thread, stop_event, num_targets)

        stop_event.set()  # 正常終了時も確実にセット

        # 全期間の取り直し (株式分割の記録) が十分に済んだら記録する。取得率が低い実行
        # (通信障害など) では記録せず、次回もう一度取り直す
        if needs_split_backfill and num_targets and len(all_data_map) >= num_targets * 0.7:
            try:
                repo.set_meta(self.SPLIT_BACKFILL_META_KEY, get_current_time().date().isoformat())
            except Exception as e:
                self.log_warn(f"株式分割の記録状態を保存できませんでした: {e}")

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
            f"✅ 市場データ取得完了: 対象 {len(target_codes)} 銘柄中 {len(all_data_map)} 銘柄のデータを準備しました。",
            flush=True,
        )
        return all_data_map

    def _consume(
        self,
        result_queue: queue.Queue,
        producer_thread: threading.Thread,
        stop_event: threading.Event,
        num_targets: int,
    ) -> Dict[str, Any]:
        """Producer から届いた銘柄ごとの履歴を集める (タイムアウト耐性とプロデューサー生存確認)。"""
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

        return all_data_map
