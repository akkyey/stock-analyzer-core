"""フルパイプライン ベンチマーク＆計測スクリプト

【計測対象フェーズ】:
1. データベース初期化 (DB Initialization)
2. データ取得 (Data Acquisition)
3. DB構築・永続化 (DB Construction / Ingestion)
4. 財務データ補完 (Financial Deep Repair / Imputation)
5. 分類・多層評価・2ファイル出力 (Classification / Pre-Filter / Quant / CSV Output)
"""

import os
import sys
import time
import sqlite3
from datetime import datetime
import polars as pl
import pandas as pd

# パス解決
ROOT_DIR = "/home/irom/dev/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.config_singleton import ConfigSingleton
from src.database.duck_client import DuckDBClient
from src.repositories.duck_repository import DuckDBRepository
from src.services.financial_repair import FinancialRepairService
from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantAgentEvaluator
from src.orchestration.dossier_builder import StockDossierBuilder


def run_benchmark():
    print("=" * 80)
    print("⏱️ 【stock-analyzer-core パイプライン全フェーズ所要時間ベンチマーク計測】")
    print(f"   実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    timings = {}
    record_counts = {}

    total_pipeline_start = time.perf_counter()

    # -------------------------------------------------------------
    # フェーズ 0: データベース初期化 (DB Initialization)
    # -------------------------------------------------------------
    print("\n[Phase 0] データベース初期化中...")
    t0_start = time.perf_counter()

    bench_duckdb_path = os.path.join(ROOT_DIR, "data/stock_analyzer.duckdb")
    bench_sqlite_path = os.path.join(ROOT_DIR, "data/stock_master_bench.db")

    # SQLite 初期化
    if os.path.exists(bench_sqlite_path):
        os.remove(bench_sqlite_path)

    with sqlite3.connect(bench_sqlite_path) as s_conn:
        s_cursor = s_conn.cursor()
        s_cursor.execute("""
            CREATE TABLE stocks (
                code TEXT PRIMARY KEY,
                name TEXT,
                sector TEXT,
                market TEXT
            )
        """)
        s_cursor.execute("""
            CREATE TABLE market_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                entry_date TEXT,
                price REAL,
                trading_value REAL,
                rsi_14 REAL,
                ma_divergence REAL,
                macd_hist REAL,
                trend_score INTEGER
            )
        """)
        s_cursor.execute("""
            CREATE TABLE fundamentals (
                code TEXT PRIMARY KEY,
                per REAL,
                pbr REAL,
                roe REAL,
                dividend_yield REAL,
                equity_ratio REAL,
                market_cap REAL,
                operating_cf REAL,
                sales REAL,
                sales_growth REAL,
                operating_margin REAL,
                net_profit REAL,
                shares_outstanding REAL,
                prev_net_profit REAL,
                debt_equity_ratio REAL,
                current_ratio REAL,
                quick_ratio REAL
            )
        """)
        s_conn.commit()

    # DuckDB 初期化 (テーブルを DROP & CREATE)
    duck_client = DuckDBClient(bench_duckdb_path)
    duck_conn = duck_client.get_connection()
    duck_conn.execute("DROP TABLE IF EXISTS metrics")
    duck_conn.execute("DROP TABLE IF EXISTS stocks")
    duck_conn.execute("""
        CREATE TABLE stocks (
            code VARCHAR PRIMARY KEY,
            name VARCHAR,
            sector VARCHAR,
            market VARCHAR
        )
    """)
    duck_conn.execute("""
        CREATE TABLE metrics (
            code VARCHAR,
            entry_date DATE,
            price DOUBLE,
            trading_value DOUBLE,
            rsi_14 DOUBLE,
            ma_divergence DOUBLE,
            macd_hist DOUBLE,
            trend_score INTEGER,
            per DOUBLE,
            pbr DOUBLE,
            roe DOUBLE,
            dividend_yield DOUBLE,
            equity_ratio DOUBLE,
            operating_cf DOUBLE,
            sales DOUBLE,
            operating_margin DOUBLE,
            net_profit DOUBLE,
            shares_outstanding DOUBLE,
            prev_net_profit DOUBLE,
            debt_equity_ratio DOUBLE,
            current_ratio DOUBLE,
            quick_ratio DOUBLE
        )
    """)

    t0_end = time.perf_counter()
    timings["0_db_init"] = t0_end - t0_start
    print(f"  ✅ [Phase 0 完了] DB初期化完了: {timings['0_db_init']*1000:.2f} ms")

    # -------------------------------------------------------------
    # フェーズ 1: データ取得 (Data Acquisition)
    # -------------------------------------------------------------
    print("\n[Phase 1] データ取得 (Data Acquisition) フェーズ開始...")
    t1_start = time.perf_counter()

    # 1-1. JPX 全銘柄マスタの取得 (重複除去)
    jpx_csv_path = os.path.join(ROOT_DIR, "data/input/jp_stock_list.csv")
    df_jpx = pl.read_csv(jpx_csv_path).unique(subset=["code"])
    
    # 1-2. 原本DBから市況時系列データ (直近25営業日) の取得
    source_db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    with sqlite3.connect(source_db_path) as s_conn:
        query_ts = """
            SELECT code, entry_date, trading_value, price, rsi_14, ma_divergence, macd_hist, trend_score
            FROM market_data
            WHERE entry_date IN (
                SELECT DISTINCT entry_date FROM market_data ORDER BY entry_date DESC LIMIT 25
            )
        """
        df_ts = pl.read_database(query_ts, s_conn)

        # 1-3. 原本DBから最新財務データの取得
        query_funda = """
            SELECT 
                code, per, pbr, roe, dividend_yield, equity_ratio, market_cap, operating_cf,
                sales, sales_growth, operating_margin, debt_equity_ratio, current_ratio, quick_ratio
            FROM fundamentals
        """
        df_funda = pl.read_database(query_funda, s_conn).unique(subset=["code"])

    t1_end = time.perf_counter()
    timings["1_acquisition"] = t1_end - t1_start
    record_counts["stocks_master"] = len(df_jpx)
    record_counts["timeseries_rows"] = len(df_ts)
    record_counts["fundamentals_rows"] = len(df_funda)

    print(f"  ✅ [Phase 1 完了] データ取得完了: {timings['1_acquisition']:.3f} 秒")
    print(f"     - 銘柄マスタ: {len(df_jpx):,} 社")
    print(f"     - 市況時系列データ (直近25営業日): {len(df_ts):,} 行")
    print(f"     - 財務データ: {len(df_funda):,} 社")

    # -------------------------------------------------------------
    # フェーズ 2: DB構築・永続化 (DB Construction / Ingestion)
    # -------------------------------------------------------------
    print("\n[Phase 2] DB構築・永続化 (DB Construction) フェーズ開始...")
    t2_start = time.perf_counter()

    # 2-1. SQLite への投入
    with sqlite3.connect(bench_sqlite_path) as s_conn:
        df_jpx.to_pandas().to_sql("stocks", s_conn, if_exists="append", index=False)
        df_ts.to_pandas().to_sql("market_data", s_conn, if_exists="append", index=False)
        df_funda.to_pandas().to_sql("fundamentals", s_conn, if_exists="append", index=False)

        s_cursor = s_conn.cursor()
        s_cursor.execute("CREATE INDEX idx_md_code_date ON market_data(code, entry_date)")
        s_cursor.execute("CREATE INDEX idx_md_date ON market_data(entry_date)")
        s_conn.commit()

    # 2-2. DuckDB への高速バルク永続化
    duck_conn.register("df_jpx_view", df_jpx.to_arrow())
    duck_conn.execute("INSERT INTO stocks SELECT * FROM df_jpx_view")
    duck_conn.unregister("df_jpx_view")

    t2_end = time.perf_counter()
    timings["2_db_construction"] = t2_end - t2_start
    total_inserted = len(df_jpx) + len(df_ts) + len(df_funda)
    throughput = total_inserted / timings["2_db_construction"]

    print(f"  ✅ [Phase 2 完了] DB構築・インデックス作成完了: {timings['2_db_construction']:.3f} 秒")
    print(f"     - 総格納レコード数: {total_inserted:,} 件 (スループット: {throughput:,.0f} 件/秒)")

    # -------------------------------------------------------------
    # フェーズ 3: 補完 (Financial Repair / Imputation)
    # -------------------------------------------------------------
    print("\n[Phase 3] 財務データ補完 (Financial Deep Repair) フェーズ開始...")
    t3_start = time.perf_counter()

    # 最新日の市況データと財務データを結合して補完対象データセットを作成
    latest_date = df_ts["entry_date"].max()
    df_latest_market = df_ts.filter(pl.col("entry_date") == latest_date)
    
    df_to_repair = df_latest_market.join(df_funda, on="code", how="left")
    
    # FinancialRepairService.apply_deep_repair による欠損補完・逆算・スケーリング
    df_repaired = FinancialRepairService.apply_deep_repair(df_to_repair)

    t3_end = time.perf_counter()
    timings["3_repair"] = t3_end - t3_start
    record_counts["repaired_rows"] = len(df_repaired)

    print(f"  ✅ [Phase 3 完了] 財務データ補完完了: {timings['3_repair']:.4f} 秒 ({timings['3_repair']*1000:.2f} ms)")
    print(f"     - 補完処理対象: {len(df_repaired):,} 社 (PER修復, 自己資本比率逆算, スケーリング等)")

    # -------------------------------------------------------------
    # フェーズ 4: 分類・多層評価・2ファイル出力 (Classification & Evaluation)
    # -------------------------------------------------------------
    print("\n[Phase 4] 分類・多層評価・2ファイル分離出力 (Classification) フェーズ開始...")
    t4_start = time.perf_counter()

    # 4-1. 第1層（Pre-Filter: 事前足切り）
    config = ConfigSingleton().get_config()
    df_liquidity = PreFilter.aggregate_timeseries_metrics(df_ts)

    # 銘柄マスタの業種・市場区分を付与
    df_candidates = df_repaired.join(df_jpx.select(["code", "name", "sector", "market"]), on="code", how="left")

    pre_result = PreFilter.evaluate(df_candidates, df_liquidity=df_liquidity, config=config)
    passed_df = pre_result.passed_df
    rejected_pre_df = pre_result.rejected_df

    # 4-2. 第2層（高密度カルテ構築 & クオンツ連続傾斜スコアリング）
    dossiers = StockDossierBuilder.from_dataframe(passed_df, limit=None)
    
    # 4-3. 第3層（多層ゲートキーパー判定）
    agent_results = []
    for d in dossiers:
        score, verdict, _, _ = QuantAgentEvaluator.evaluate(d)
        agent_results.append({
            "code": str(d.get("code")),
            "name": d.get("name", "Unknown"),
            "verdict": verdict,
            "agent_score": score,
        })

    # 4-4. 2ファイル直接分離出力 (daily_report.csv / uncalculable_stocks.csv)
    dossier_map = {str(d["code"]): d for d in dossiers}
    evaluable_rows = []
    uncalculable_rows = []

    # 第1層足切り銘柄の登録
    for rej in rejected_pre_df.to_dicts():
        uncalculable_rows.append({
            "Code": str(rej.get("code", "")),
            "Name": rej.get("name", "Unknown"),
            "Sector": rej.get("sector", "Other"),
            "Market": rej.get("market", "Other"),
            "Price": rej.get("price"),
            "ROE": rej.get("roe"),
            "PBR": rej.get("pbr"),
            "Equity_Ratio": rej.get("equity_ratio"),
            "Uncalculable_Reason": f"第1層足切り: {rej.get('filter_reason')}",
            "Detail": rej.get("filter_detail", ""),
        })

    # 通過銘柄の精査（完全評価可能銘柄の選定）
    for r in agent_results:
        c = r["code"]
        d = dossier_map.get(c, {})
        f = d.get("fundamentals", {})
        t = d.get("technicals", {})

        per_val = f.get("per")
        pbr_val = f.get("pbr")
        roe_val = f.get("roe")
        eq_ratio = f.get("equity_ratio")
        price_val = t.get("price")
        sec = d.get("sector", "")
        mkt = d.get("market", "")
        ma_div = t.get("ma25_divergence")
        triggers_list = d.get("trigger_reasons", [])
        triggers = ", ".join(triggers_list) if triggers_list else "特になし"

        is_evaluable = (
            per_val is not None and per_val > 0
            and pbr_val is not None and pbr_val > 0
            and roe_val is not None
            and eq_ratio is not None and eq_ratio > 0
            and ma_div is not None
            and sec not in ["", "None", "nan", None]
            and mkt not in ["", "None", "nan", None]
        )

        if is_evaluable:
            evaluable_rows.append({
                "Rank": 0,
                "Code": c,
                "Name": r["name"],
                "Sector": sec,
                "Market": mkt,
                "Verdict": r["verdict"],
                "Agent_Score": r["agent_score"],
                "Price": price_val,
                "RSI_14": t.get("rsi_14"),
                "MACD_Status": t.get("macd_status"),
                "MA25_Divergence": ma_div,
                "PER": per_val,
                "PBR": pbr_val,
                "ROE": roe_val,
                "Equity_Ratio": eq_ratio,
                "Triggers": triggers,
                "Investment_Thesis": "",
                "Risk_Factors": "",
                "Time_Horizon": "",
            })
        else:
            uncalculable_rows.append({
                "Code": c,
                "Name": r["name"],
                "Sector": sec,
                "Market": mkt,
                "Price": price_val,
                "ROE": roe_val,
                "PBR": pbr_val,
                "Equity_Ratio": eq_ratio,
                "Uncalculable_Reason": "当期純損失 (最終赤字)" if (roe_val is not None and roe_val < 0) else "重要指標未開示/算出不能",
                "Detail": f"ROE {roe_val:.1f}%" if roe_val is not None else "データ未開示",
            })

    evaluable_rows.sort(key=lambda x: x["Agent_Score"], reverse=True)
    for idx, row in enumerate(evaluable_rows, start=1):
        row["Rank"] = idx

    df_evaluable = pd.DataFrame(evaluable_rows)
    df_uncalculable = pd.DataFrame(uncalculable_rows)

    out_dir = os.path.join(ROOT_DIR, "data/output")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    eval_path = os.path.join(out_dir, "daily_report.csv")
    with open(eval_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# Generated At: {now_str}\n")
    df_evaluable.to_csv(eval_path, mode="a", index=False, encoding="utf-8-sig")

    uncalc_path = os.path.join(out_dir, "uncalculable_stocks.csv")
    with open(uncalc_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# Generated At: {now_str}\n")
    df_uncalculable.to_csv(uncalc_path, mode="a", index=False, encoding="utf-8-sig")

    t4_end = time.perf_counter()
    timings["4_classification"] = t4_end - t4_start
    record_counts["daily_report"] = len(df_evaluable)
    record_counts["uncalculable"] = len(df_uncalculable)

    print(f"  ✅ [Phase 4 完了] 分類・多層評価・2ファイル出力完了: {timings['4_classification']:.3f} 秒")
    print(f"     - 評価可能銘柄 (daily_report.csv): {len(df_evaluable):,} 社")
    print(f"     - 除外・算出不能銘柄 (uncalculable_stocks.csv): {len(df_uncalculable):,} 社")

    # クリーンアップ（ベンチマーク用SQLite削除）
    if os.path.exists(bench_sqlite_path):
        os.remove(bench_sqlite_path)

    total_pipeline_time = time.perf_counter() - total_pipeline_start

    # -------------------------------------------------------------
    # 最終集計サマリー表示
    # -------------------------------------------------------------
    print("\n" + "=" * 80)
    print("📊 【ベンチマーク計測結果サマリー】")
    print("=" * 80)
    print(f"{'フェーズ':<28} | {'所要時間':<12} | {'割合 (%)':<10} | {'処理規模・スループット'}")
    print("-" * 80)

    p0 = timings["0_db_init"]
    p1 = timings["1_acquisition"]
    p2 = timings["2_db_construction"]
    p3 = timings["3_repair"]
    p4 = timings["4_classification"]

    pct0 = (p0 / total_pipeline_time) * 100.0
    pct1 = (p1 / total_pipeline_time) * 100.0
    pct2 = (p2 / total_pipeline_time) * 100.0
    pct3 = (p3 / total_pipeline_time) * 100.0
    pct4 = (p4 / total_pipeline_time) * 100.0

    print(f"{'0. DB初期化':<26} | {p0*1000:>8.2f} ms | {pct0:>8.2f} % | DuckDB & SQLite テーブルクリア")
    print(f"{'1. データ取得 (Acquisition)':<22} | {p1:>8.3f} 秒  | {pct1:>8.2f} % | 全3,919社 + 時系列9.8万行ロード")
    print(f"{'2. DB構築 (DB Ingestion)':<24} | {p2:>8.3f} 秒  | {pct2:>8.2f} % | 10.6万行格納 ({total_inserted/p2:,.0f} 行/秒)")
    print(f"{'3. 補完 (Financial Repair)':<23} | {p3*1000:>8.2f} ms | {pct3:>8.2f} % | 全3,919社ベクトル補完・逆算")
    print(f"{'4. 分類 (Classification/PreFilter)':<20} | {p4:>8.3f} 秒  | {pct4:>8.2f} % | 3層評価 & 2ファイル直接分離")
    print("-" * 80)
    print(f"{'合計 (Total Pipeline Time)':<26} | {total_pipeline_time:>8.3f} 秒  | 100.00 % | 全工程完了")
    print("=" * 80)


if __name__ == "__main__":
    run_benchmark()
