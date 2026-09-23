"""
EDINET 過去365日分バックフィル ＋ 全銘柄最新分析パイプライン
実測ベンチマーク（Qiita第4弾と同条件比較）スクリプト
"""

import os
import sys
import time
import json
import sqlite3
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv, dotenv_values
import requests
import yfinance as yf
import polars as pl
import pandas as pd

ROOT_DIR = "/home/irom/dev/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# .env を project-stock2 からロード
p2_env = "/home/irom/dev/project-stock2/stock-analyzer-core/.env"
if os.path.exists(p2_env):
    vals = dotenv_values(p2_env)
    for k, v in vals.items():
        if v and not os.environ.get(k):
            os.environ[k] = v

from src.config_singleton import ConfigSingleton
from src.database.duck_client import DuckDBClient
from src.fetcher.edinet_fetcher import EdinetFetcher
from src.fetcher.xbrl_parser import XbrlParser
from src.fetcher.turbo_acquisition import TurboAcquisitionManager
from src.services.financial_repair import FinancialRepairService
from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantAgentEvaluator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RebuildBenchmark")


def main():
    print("=" * 85, flush=True)
    print("🚀 【全パイプライン性能実測ベンチマーク（Qiita第4弾・完結編と完全同条件）】", flush=True)
    print(f"   実行日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"   EDINET API Key 有効性: {'OK' if os.environ.get('EDINET_API_KEY') else 'MISSING'}", flush=True)
    print("=" * 85, flush=True)

    timings = {}
    total_start = time.perf_counter()

    config = ConfigSingleton().get_config()
    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")

    # =========================================================================
    # Phase 1: EDINET 365日バックフィル (Turbo Acquisition & DB Rebuild)
    # =========================================================================
    print("\n" + "=" * 60, flush=True)
    print("📡 【Phase 1】 EDINET 過去365日分バックフィル & 確定財務DB更新", flush=True)
    print("=" * 60, flush=True)
    p1_start = time.perf_counter()

    # 一時キャッシュフォルダの準備 (今回の計測用にクリーンなディレクトリを使用)
    # Note: TurboAcquisitionManager のtmp_dir/results_dir をベンチマーク用に設定
    fetcher = EdinetFetcher(config)
    parser = XbrlParser()
    manager = TurboAcquisitionManager(fetcher, parser, config)

    # 365日分の書類スキャン ＆ 並列ダウンロード・パース
    print("  -> 過去365日分の開示書類スキャン ＆ 本決算(有報) 並列DL・パース開始...", flush=True)
    edinet_results = manager.run_turbo_acquisition(days=365)
    print(f"  ✅ EDINET 取得・パース完了: {len(edinet_results):,} 銘柄の最新財務データを抽出", flush=True)

    # DB (fundamentals) への書き込み更新
    print(f"  -> stock_master.db (fundamentals) へ確定財務データを更新中...", flush=True)
    ingest_t0 = time.perf_counter()

    upsert_sql = """
        INSERT INTO fundamentals (
            code, per, pbr, roe, dividend_yield, equity_ratio, market_cap,
            operating_cf, free_cf, sales, operating_margin, repair_metadata,
            fetch_status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, DATETIME('now', 'localtime'))
        ON CONFLICT(code) DO UPDATE SET
            roe = COALESCE(EXCLUDED.roe, fundamentals.roe),
            equity_ratio = COALESCE(EXCLUDED.equity_ratio, fundamentals.equity_ratio),
            sales = COALESCE(EXCLUDED.sales, fundamentals.sales),
            operating_margin = COALESCE(EXCLUDED.operating_margin, fundamentals.operating_margin),
            operating_cf = COALESCE(EXCLUDED.operating_cf, fundamentals.operating_cf),
            free_cf = COALESCE(EXCLUDED.free_cf, fundamentals.free_cf),
            repair_metadata = EXCLUDED.repair_metadata,
            fetch_status = 'edinet_synced',
            updated_at = DATETIME('now', 'localtime');
    """

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        batch_rows = []
        for code, data in edinet_results.items():
            sales = data.get("sales")
            op_income = data.get("operating_income")
            net_profit = data.get("net_profit")
            total_assets = data.get("total_assets")
            net_assets = data.get("net_assets")
            op_cf = data.get("operating_cf")
            investing_cf = data.get("investing_cf")

            free_cf = None
            if op_cf is not None and investing_cf is not None:
                free_cf = op_cf + investing_cf

            op_margin = None
            if sales and op_income and sales > 0:
                op_margin = round((op_income / sales) * 100, 2)

            equity_ratio = None
            if total_assets and net_assets and total_assets > 0:
                equity_ratio = round((net_assets / total_assets) * 100, 2)

            roe = None
            if net_assets and net_profit and net_assets > 0:
                roe = round((net_profit / net_assets) * 100, 2)

            meta = json.dumps({
                "source": "edinet_turbo",
                "doc_id": data.get("doc_id"),
                "submit_date": data.get("submit_date"),
                "extracted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            batch_rows.append((
                code, None, None, roe, None, equity_ratio, None,
                op_cf, free_cf, sales, op_margin, meta, 'edinet_synced'
            ))

        cur.executemany(upsert_sql, batch_rows)
        conn.commit()

    ingest_elapsed = time.perf_counter() - ingest_t0
    p1_end = time.perf_counter()
    timings["p1_edinet_backfill"] = p1_end - p1_start
    print(f"  ✅ [Phase 1 完了] EDINET バックフィル ＆ DB更新所要時間: {timings['p1_edinet_backfill']:.2f} 秒 ({timings['p1_edinet_backfill']/60:.2f} 分)", flush=True)

    # =========================================================================
    # Phase 2: 最新株価取得 ＆ 3層クオンツ分析・順位付け
    # =========================================================================
    print("\n" + "=" * 60, flush=True)
    print("📊 【Phase 2】 最新株価ライブ取得 ＆ 3層クオンツ分析・2ファイル分離出力", flush=True)
    print("=" * 60, flush=True)
    p2_start = time.perf_counter()

    # 1. 銘柄マスタの取得
    df_jpx = pl.read_csv(os.path.join(ROOT_DIR, "data/input/jp_stock_list.csv")).unique(subset=["code"])
    all_codes = df_jpx["code"].to_list()
    print(f"  -> 対象銘柄数: 全 {len(all_codes):,} 社", flush=True)

    # 2. yfinance による最新株価並列バッチ取得
    yf_t0 = time.perf_counter()
    CHUNK_SIZE = 500
    code_chunks = [all_codes[i : i + CHUNK_SIZE] for i in range(0, len(all_codes), CHUNK_SIZE)]
    print(f"  -> yfinance 並列ダウンロード (全 {len(code_chunks)} チャンク)...", flush=True)

    df_yf_rows = []
    for idx, chunk in enumerate(code_chunks, start=1):
        c_t0 = time.perf_counter()
        tickers = [f"{c}.T" for c in chunk]
        chunk_data = yf.download(tickers, period="5d", progress=False, threads=True)
        c_elapsed = time.perf_counter() - c_t0

        if hasattr(chunk_data, "columns") and hasattr(chunk_data.columns, "levels") and len(chunk_data.columns.levels) > 1:
            close_df = chunk_data["Close"]
            vol_df = chunk_data["Volume"]
            for t in tickers:
                code = t.replace(".T", "")
                if t in close_df.columns:
                    s_close = close_df[t].dropna()
                    s_vol = vol_df[t].dropna() if t in vol_df.columns else None
                    if not s_close.empty:
                        last_dt = s_close.index[-1]
                        df_yf_rows.append({
                            "code": code,
                            "entry_date": last_dt.strftime("%Y-%m-%d"),
                            "price": float(s_close.iloc[-1]),
                            "trading_value": float(s_vol.iloc[-1]) if s_vol is not None and not s_vol.empty else 0.0,
                        })
        print(f"     [{idx}/{len(code_chunks)}] {len(chunk)} 社取得完了 ({c_elapsed:.2f} 秒)", flush=True)
        if idx < len(code_chunks):
            time.sleep(0.3)

    yf_elapsed = time.perf_counter() - yf_t0
    df_live_prices = pl.DataFrame(df_yf_rows) if df_yf_rows else pl.DataFrame()
    print(f"  ✅ yfinance 株価取得完了: {yf_elapsed:.2f} 秒 ({len(df_live_prices):,} 社)", flush=True)

    # 3. 確定財務データとの結合 ＆ Polars ネイティブ深層補完
    repair_t0 = time.perf_counter()
    with sqlite3.connect(db_path) as conn:
        df_funda = pl.read_database("SELECT * FROM fundamentals", conn).unique(subset=["code"])

    df_merged = df_live_prices.join(df_funda, on="code", how="left")
    df_repaired = FinancialRepairService.apply_deep_repair(df_merged)
    repair_elapsed = time.perf_counter() - repair_t0
    print(f"  ✅ 財務データ深層補完完了: {repair_elapsed*1000:.2f} ms ({len(df_repaired):,} 社)", flush=True)

    # 4. 3層評価・分類 ＆ 2ファイル分離出力
    eval_t0 = time.perf_counter()

    # テクニカル指標ダミー補完（必要列の確保）
    if "rsi_14" not in df_repaired.columns:
        df_repaired = df_repaired.with_columns(pl.lit(50.0).alias("rsi_14"))
    if "macd_hist" not in df_repaired.columns:
        df_repaired = df_repaired.with_columns(pl.lit(0.0).alias("macd_hist"))
    if "ma_divergence" not in df_repaired.columns:
        df_repaired = df_repaired.with_columns(pl.lit(0.0).alias("ma_divergence"))

    # 第1層 PreFilter
    df_evaluable, df_uncalc = PreFilter.filter_uncalculable_stocks(df_repaired)

    # 第2層 & 第3層 QuantEvaluator
    qe = QuantAgentEvaluator(config)
    eval_results = qe.evaluate_batch_polars(df_evaluable)

    # 出力ファイル書き出し
    out_dir = os.path.join(ROOT_DIR, "data/output")
    os.makedirs(out_dir, exist_ok=True)
    daily_path = os.path.join(out_dir, "daily_report.csv")
    uncalc_path = os.path.join(out_dir, "uncalculable_stocks.csv")

    # ヘッダー付与
    gen_time_comment = f"# Generated At: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

    if eval_results is not None and not eval_results.is_empty():
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write(gen_time_comment)
        eval_results.to_pandas().to_csv(daily_path, mode="a", index=False)

    if not df_uncalc.is_empty():
        with open(uncalc_path, "w", encoding="utf-8") as f:
            f.write(gen_time_comment)
        df_uncalc.to_pandas().to_csv(uncalc_path, mode="a", index=False)

    eval_elapsed = time.perf_counter() - eval_t0
    p2_end = time.perf_counter()
    timings["p2_analysis"] = p2_end - p2_start
    print(f"  ✅ [Phase 2 完了] 分析フロー所要時間: {timings['p2_analysis']:.2f} 秒 ({timings['p2_analysis']/60:.2f} 分)", flush=True)

    total_elapsed = time.perf_counter() - total_start
    timings["total"] = total_elapsed

    # =========================================================================
    # 最終対比レポート出力
    # =========================================================================
    print("\n" + "=" * 85, flush=True)
    print("🏆 【全パイプライン性能ベンチマーク 実測対比結果】", flush=True)
    print("=" * 85, flush=True)
    print(f"{'測定フェーズ':<32} | {'前回Qiita記事 (2026-03)':<22} | {'今回実測 (2026-09 最新DB構築)':<22}", flush=True)
    print("-" * 85, flush=True)
    print(f"{'Phase 1: EDINET バックフィル':<30} | {'7分42秒 (462 秒)':<20} | {f'{int(timings["p1_edinet_backfill"]//60)}分{timings["p1_edinet_backfill"]%60:.1f}秒 ({timings["p1_edinet_backfill"]:.1f}秒)':<20}", flush=True)
    print(f"{'Phase 2: 分析・順位付けフロー':<30} | {'1分31秒 ( 91 秒)':<20} | {f'{int(timings["p2_analysis"]//60)}分{timings["p2_analysis"]%60:.1f}秒 ({timings["p2_analysis"]:.1f}秒)':<20}", flush=True)
    print("-" * 85, flush=True)
    print(f"{'総合パイプライン合計 (Total)':<28} | {'9分13秒 (553 秒)':<20} | {f'{int(timings["total"]//60)}分{timings["total"]%60:.1f}秒 ({timings["total"]:.1f}秒)':<20}", flush=True)
    print("=" * 85, flush=True)
    print(f"✅ 出力完了:", flush=True)
    print(f"   - 評価可能銘柄: {daily_path} ({len(eval_results):,} 社)", flush=True)
    print(f"   - 除外銘柄:     {uncalc_path} ({len(df_uncalc):,} 社)", flush=True)
    print("=" * 85, flush=True)


if __name__ == "__main__":
    main()
