import time
from typing import Any, Dict, List

import pandas as pd

from .base import FetcherBase
from .fundamentals_fetcher import FundamentalsFetcher
from .jpx import JPXFetcher
from .market_fetcher import MarketFetcher


class DataFetcher(FetcherBase):
    """
    Facade class for DataFetcher components.
    [v1.0] Physical Separation: 分離された MarketFetcher と FundamentalsFetcher を統合的に管理。
    """

    def __init__(self, config_source=None):
        super().__init__(config_source)
        self.jpx_fetcher = JPXFetcher(self.config)
        self.market_fetcher = MarketFetcher(self.config)
        self.fundamentals_fetcher = FundamentalsFetcher(self.config)

    # ------------------------------------------------------------------
    # JPX Operations
    # ------------------------------------------------------------------
    def fetch_jpx_list(self, fallback_on_error=False, save_to_csv=True):
        return self.jpx_fetcher.fetch_jpx_list(fallback_on_error, save_to_csv)

    # ------------------------------------------------------------------
    # Daily Market Data Operations (OHLCV + Technicals)
    # ------------------------------------------------------------------
    def fetch_market_data(self, *args, **kwargs):
        return self.fetch_stock_data(*args, **kwargs)

    def fetch_stock_data(
        self,
        codes=None,
        fallback_on_error=False,
        threads: bool = True,
        chunk_size: int = 500,
        context: Any = None,
        period: str = "6mo",
    ) -> pd.DataFrame:
        """Batch fetch daily market data (平日配信用)"""
        # 1. 対象銘柄の解決
        codes, market_map, name_map = self._resolve_codes_for_fetching(
            codes, fallback_on_error
        )
        if not codes:
            return pd.DataFrame()

        # [v18.9] チャンクサイズを再度調整 (200 -> 500)
        # yfinanceの最新仕様に伴うセッション確立コスト（TLSオーバーヘッド）を削減するため、1回あたりの件数を増やします。
        CHUNK_SIZE = chunk_size
        all_batch_results = {}

        # 銘柄リストを分割
        code_chunks = [
            codes[i : i + CHUNK_SIZE] for i in range(0, len(codes), CHUNK_SIZE)
        ]
        self.logger.info(
            f"📊 Starting chunked fetch: {len(codes)} stocks in {len(code_chunks)} chunks."
        )

        for i, chunk in enumerate(code_chunks):
            self.logger.info(
                f"📦 Fetching chunk {i + 1}/{len(code_chunks)} ({len(chunk)} stocks)..."
            )
            # [v1.0] 分離された MarketFetcher を使用
            chunk_results = {}
            for attempt in range(2):
                try:
                    chunk_results = self.market_fetcher.fetch_market_data(
                        chunk, period=period, threads=threads, context=context
                    )
                    if chunk_results:
                        break
                    self.logger.warning(
                        f"⚠️ Chunk {i + 1} returned empty. Retry {attempt + 1}/2..."
                    )
                except Exception as e:
                    self.logger.warning(f"⚠️ Issue in chunk {i + 1}: {e}")

                if attempt == 0:
                    import time

                    time.sleep(2)

            # 結果をマージ
            all_batch_results.update(chunk_results)

            # チャンク間の待機時間 (安定性を考慮して 0.5s に調整)
            if i < len(code_chunks) - 1:
                import time

                time.sleep(0.5)

        # 3. 取得結果をそのまま返す (dict[str, pd.DataFrame])
        # [v1.0] ScanHandler/PolarsProcessor は dict 形式を期待しているため、DataFrameへの変換は行わない。
        return all_batch_results

    # ------------------------------------------------------------------
    # Heavy Fundamentals Operations (EPS, PBR, etc.)
    # ------------------------------------------------------------------
    def fetch_fundamentals_data(
        self, codes: list[str], resume: bool = True, context: Any = None
    ) -> List[Dict[str, Any]]:
        """休日バッチ・オンライン等で業績データを丁寧に取得する。
        [v1.2] Producer-Consumer Pipeline: 取得(I/O)と管理(Main)を分離し、2多重で高速化。
        JSONファイルに途中経過を保存し、次回起動時にレジューム（途中再開）する機能を持つ。
        """
        import json
        import os

        from src.config_singleton import ConfigSingleton

        cfg = ConfigSingleton().get_config()
        output_dir = cfg.get("paths", {}).get("output_dir", "data/output")
        resume_file = os.path.join(output_dir, "fundamentals_resume.json")

        results_map = {}
        if resume and os.path.exists(resume_file):
            try:
                with open(resume_file, "r", encoding="utf-8") as f:
                    results_map = json.load(f)
                self.logger.info(
                    f"🔄 Resumed from {len(results_map)} already fetched records."
                )
            except Exception as e:
                self.logger.warning(f"⚠️ Failed to load resume file: {e}")

        pending_codes = [c for c in codes if str(c) not in results_map]

        if not pending_codes:
            self.logger.info("✅ All fundamentals already fetched for the given list.")
            return list(results_map.values())

        self.logger.info(
            f"🚀 [Pipeline] Fetching fundamentals for {len(pending_codes)} stocks (Sequential)..."
        )

        from tqdm import tqdm

        pbar = tqdm(total=len(pending_codes), desc="Fetching Fundamentals")

        t_fetch_start = time.time()
        try:
            for code in pending_codes:
                try:
                    res = self.fundamentals_fetcher.fetch_heavy_fundamentals(code)
                    if res:
                        results_map[str(code)] = res

                    # 10軒ごとにレジューム保存
                    if len(results_map) % 10 == 0:
                        try:
                            os.makedirs(output_dir, exist_ok=True)
                            with open(resume_file, "w", encoding="utf-8") as f:
                                json.dump(results_map, f, ensure_ascii=False, indent=2)
                        except Exception as e:
                            self.logger.warning(f"⚠️ Intermediate save failed: {e}")

                except Exception as e:
                    self.logger.warning(f"⚠️ Fetch failed for {code}: {e}")
                    results_map[str(code)] = {
                        "code": code,
                        "fetch_status": "error_other",
                    }

                pbar.update(1)

        except KeyboardInterrupt:
            self.logger.warning("🛑 Interrupt received. Saving current progress...")
        finally:
            pbar.close()
            # [v29.3] 統計情報の更新
            fetch_elapsed = time.time() - t_fetch_start
            if context and hasattr(context, "perf_stats"):
                context.perf_stats["fetch_sec"] += fetch_elapsed

        # 最終保存
        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(resume_file, "w", encoding="utf-8") as f:
                json.dump(results_map, f, ensure_ascii=False, indent=2)
            self.logger.info(f"✅ Fetch completed ({len(results_map)} total stocks).")
        except Exception as e:
            self.logger.warning(f"⚠️ Final save failed: {e}")

        return list(results_map.values())

    # ------------------------------------------------------------------
    # Common Utilities
    # ------------------------------------------------------------------
    def _resolve_codes_for_fetching(self, codes, fallback_on_error):
        """フェッチ対象のコードとマスタマップを特定する。"""
        market_map, name_map = {}, {}

        # ローカルリスト読み込み
        try:
            local_jpx = self.jpx_fetcher.load_local_data()
            if not local_jpx.empty and "code" in local_jpx.columns:
                if "market" in local_jpx.columns:
                    market_map = dict(
                        zip(
                            local_jpx["code"].astype(str),
                            local_jpx["market"],
                            strict=False,
                        )
                    )
                if "name" in local_jpx.columns:
                    name_map = dict(
                        zip(
                            local_jpx["code"].astype(str),
                            local_jpx["name"],
                            strict=False,
                        )
                    )
        except Exception as e:
            self.jpx_fetcher.logger.warning(f"⚠️ Failed to load local JPX metadata: {e}")

        if codes is not None:
            return codes, market_map, name_map

        # JPXからの全件取得 (フォールバック)
        # [v18.9] Dailyスキャン等の文脈では、DBがソースであるべきなので、 here では空を返すか、
        # あるいは明示的に codes が None の場合は既存 codes を取得するように上位で制御する。
        # DataFetcher自体のデフォルト挙動としては、Localキャッシュがあればそれを使い、なければダウンロードする。
        try:
            # save_to_csv=False にすることで、単なるメモリ上の解決に留める
            jpx_df = self.jpx_fetcher.fetch_jpx_list(
                fallback_on_error=fallback_on_error, save_to_csv=False
            )
            if not jpx_df.empty and "code" in jpx_df.columns:
                codes = jpx_df["code"].tolist()
                # ...コード解決ロジック続く...
                if "market" in jpx_df.columns:
                    market_map = dict(
                        zip(jpx_df["code"].astype(str), jpx_df["market"], strict=False)
                    )
                if "name" in jpx_df.columns:
                    name_map = dict(
                        zip(jpx_df["code"].astype(str), jpx_df["name"], strict=False)
                    )
                return codes, market_map, name_map
        except Exception as e:
            self.jpx_fetcher.logger.error(f"❌ Failed to resolve codes: {e}")

        return [], market_map, name_map

    def _handle_fallback(self, code, current_data):
        """API取得失敗時、DBの最新データからフォールバックを試みる。"""
        if current_data and current_data.get("fetch_status") == self.STATUS_SUCCESS:
            return current_data

        self.logger.info(f"⚠️ API fetch failed for {code}. Attempting fallback to DB...")
        db_data = self.fetch_data_from_db([code])
        if not db_data.empty:
            data = db_data.iloc[0].to_dict()
            data["fetch_status"] = "success_fallback"
            return data

        self.logger.warning(f"❌ No data found in DB for {code}. Fallback failed.")
        return current_data

    def _process_fetched_data(self, code, data, market_map, name_map):
        """取得データに市場情報等(マスタ情報)をマージする。"""
        data_copy = data.copy()
        code_str = str(code)
        if code_str in market_map:
            data_copy["market"] = market_map[code_str]
        elif "market" not in data_copy or data_copy["market"] == "Unknown":
            data_copy["market"] = "Unknown"
        if code_str in name_map:
            data_copy["name"] = name_map[code_str]
        return data_copy

    def fetch_data_from_db(self, codes: list) -> pd.DataFrame:
        """データベースから指定銘柄の最新データを取得する。"""
        from src.repositories.duck_repository import DuckDBRepository

        duck_repo = DuckDBRepository()

        # 銘柄マスタと市況データを JOIN して取得
        query = """
            SELECT m.*, s.name, s.sector, s.market
            FROM daily_metrics m
            JOIN stocks s ON m.code = s.code
            WHERE m.code IN (SELECT UNNEST(?))
            QUALIFY ROW_NUMBER() OVER (PARTITION BY m.code ORDER BY m.entry_date DESC) = 1
        """

        try:
            with duck_repo.client.get_connection() as conn:
                df_pl = conn.execute(query, [codes]).pl()
                return df_pl.to_pandas()
        except Exception as e:
            self.logger.error(f"Error fetching data from DuckDB: {e}")
            return pd.DataFrame()
