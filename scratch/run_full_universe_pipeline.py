"""全3,919銘柄の実データに対する本格クオンツ＆エージェント協調型評価パイプライン実行スクリプト

ローカルDB（全3,919銘柄）から全データを一括読込し、
1. PolarsEngine (value_growth_hybrid) による多変量クオンツスコアリング (quant_score)
2. Polars による一次足切りスクリーニング（ボロ株・債務超過の排除）
3. スクリーニング通過全銘柄からの高密度銘柄カルテ (StockDossier) 抽出
4. クオンツスコアと銘柄カルテに基づくAIエージェント投資判断 (Verdict: STRONG_BUY/BUY/WATCH/PASS)
5. 「完全評価可能銘柄 (daily_report.csv)」と「論理的算出不能銘柄 (uncalculable_stocks.csv)」の直接分離出力
をフルスケールで実行する。
"""

import os
import sys
import time
import json
import sqlite3
from datetime import datetime
import polars as pl
import pandas as pd

# パス解決
ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.config_singleton import ConfigSingleton
from src.calc.engines.polars_engine import PolarsEngine
from src.calc.quant_evaluator import QuantAgentEvaluator
from src.orchestration.dossier_builder import StockDossierBuilder


def run_full_universe_pipeline():
    start_time = time.time()
    print("=" * 75)
    print("🚀 【全3,919銘柄 本格クオンツ＆エージェント協調型評価パイプライン】実行開始")
    print("=" * 75)

    # 1. SQLite から最新市況データ (2026-03-27) の全件ロード
    print("\n📦 1. データベースから最新市況データ（全3,919社）をロード中...")
    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    query = """
        SELECT 
            m.code, m.entry_date, m.price, m.rsi_14, m.ma_divergence, m.macd_hist, m.trend_score, m.trading_value,
            COALESCE(m.sales, f.sales) AS sales, 
            COALESCE(m.sales_growth, f.sales_growth) AS sales_growth, 
            m.profit_growth, 
            COALESCE(m.profit_growth_raw, f.profit_growth_raw) AS profit_growth_raw, 
            COALESCE(f.operating_margin, m.operating_margin) AS operating_margin,
            s.name, s.sector, s.market,
            f.per, f.pbr, f.roe, f.dividend_yield, f.equity_ratio, f.market_cap, f.operating_cf
        FROM market_data m
        LEFT JOIN stocks s ON m.code = s.code
        LEFT JOIN fundamentals f ON m.code = f.code
        WHERE m.entry_date = (SELECT MAX(entry_date) FROM market_data)
    """
    with sqlite3.connect(db_path) as conn:
        df_metrics = pl.read_database(query, conn)

    total_stocks = len(df_metrics)
    entry_date = df_metrics["entry_date"][0] if not df_metrics.is_empty() else "Unknown"
    print(f"  ✅ ロード完了: 対象銘柄数: {total_stocks:,} 社 (基準日: {entry_date}, 所要時間: {time.time() - start_time:.2f}秒)")

    # 2. PolarsEngine による一次足切りスクリーニング（ボロ株・債務超過の排除）
    print("\n⚡ 2. PolarsEngine による一次足切りスクリーニング...")
    t2 = time.time()
    config = ConfigSingleton().get_config()
    engine = PolarsEngine(config, strategy_name="value_growth_hybrid")

    # 一次足切りスクリーニング（ボロ株・債務超過の排除）
    passed_df = engine.filter_candidates(
        df_metrics,
        min_price=50.0,
        min_volume=0.0,
        min_equity_ratio=5.0,
        max_candidates=None  # 全件網羅
    )
    passed_count = len(passed_df)
    filtered_out = total_stocks - passed_count
    print(f"  ✅ スクリーニング完了: {passed_count:,} 社通過（除外: {filtered_out:,} 社）(所要時間: {time.time() - t2:.2f}秒)")

    # 3. 通過全銘柄の高密度銘柄カルテ (StockDossier) を構築（全件網羅）
    print(f"\n📋 3. 通過全銘柄 ({passed_count:,} 社) の高密度銘柄カルテ (StockDossier) を全件構築...")
    t3 = time.time()
    dossiers = StockDossierBuilder.from_dataframe(passed_df, limit=None)
    print(f"  ✅ 全件カルテ構築完了: {len(dossiers):,} 件のカルテを生成 (所要時間: {time.time() - t3:.2f}秒)")

    # 4. AI エージェント（アナリスト）による連続グラデーション評価・採点・ランク付け
    print(f"\n🤖 4. AI エージェントによる連続グラデーション採点・ランキング付与...")
    t4 = time.time()
    agent_results = []
    for d in dossiers:
        code = str(d.get("code"))
        name = d.get("name", "Unknown")
        score, verdict, _, _ = QuantAgentEvaluator.evaluate(d)
        agent_results.append({
            "code": code,
            "name": name,
            "verdict": verdict,
            "agent_score": score,
            "investment_thesis": "",
            "risk_factors": [],
            "time_horizon": "",
        })
    print(f"  ✅ 全銘柄のAI評価・ランク付け完了: 合計 {len(agent_results):,} 銘柄 (所要時間: {time.time() - t4:.2f}秒)")

    # 5. 上位ランキングのコンソール表示
    ranked_results = sorted(agent_results, key=lambda x: x["agent_score"], reverse=True)
    dossier_map = {str(d["code"]): d for d in dossiers}
    print("\n" + "=" * 75)
    print(f"🏆 【本格クオンツ＆AIエージェント投資判断ランキング TOP 20 (母集団: 全 {len(agent_results):,} 銘柄)】")
    print("=" * 75)
    print(f"{'Rank':<5} | {'Code':<6} | {'銘柄名':<16} | {'判定':<11} | {'Score':<6} | {'着目トリガー'}")
    print("-" * 75)
    for idx, r in enumerate(ranked_results[:20], start=1):
        c = r.get("code", "-")
        d = dossier_map.get(str(c), {})
        trig_str = ", ".join(d.get("trigger_reasons", []))[:36]
        print(f"#{idx:<4} | {c:<6} | {r.get('name', 'Unknown')[:14]:<16} | {r.get('verdict', '-'):<11} | {r.get('agent_score', 0.0):<6.1f} | {trig_str}")

    # 6. 全銘柄の分析結果を「評価可能銘柄」と「論理的算出不能銘柄」に直接分類してCSV生成
    evaluable_rows = []
    uncalculable_rows = []

    edinet_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")

    for r in agent_results:
        c = str(r.get("code"))
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

        # 評価可能銘柄の厳格条件:
        # PER, PBR, ROE, 自己資本比率, MA25乖離率, セクター, 市場区分がすべて正常値で揃っていること
        # 1つでも欠落しているものは、算出不能・情報不完全銘柄として uncalculable_stocks.csv へ振り分ける
        is_fully_evaluable = (
            per_val is not None and per_val > 0
            and pbr_val is not None and pbr_val > 0
            and roe_val is not None
            and eq_ratio is not None and eq_ratio > 0
            and ma_div is not None
            and sec is not None and str(sec).strip() not in ["", "None", "nan"]
            and mkt is not None and str(mkt).strip() not in ["", "None", "nan"]
        )

        if is_fully_evaluable:
            evaluable_rows.append({
                "Rank": 0, # 後で再採番
                "Code": c,
                "Name": r.get("name"),
                "Sector": sec,
                "Market": mkt,
                "Verdict": r.get("verdict"),
                "Agent_Score": r.get("agent_score"),
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
            # 算出不能・情報不完全の理由を精密判定
            json_path = os.path.join(edinet_dir, f"{c}.json")
            net_profit = None
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r") as fp:
                        ed = json.load(fp)
                        net_profit = ed.get("net_profit")
                except Exception:
                    pass

            if (roe_val is not None and roe_val < 0) or (net_profit is not None and net_profit < 0):
                reason = "当期純損失 (最終赤字)"
                loss_str = f"{net_profit:,.0f}円" if net_profit else f"ROE {roe_val:.1f}%"
                detail = f"純損失のためPER定義不可 ({loss_str})"
            elif pbr_val is not None and pbr_val < 0:
                reason = "債務超過 (純資産マイナス)"
                detail = f"純資産マイナスのためPBR/自己資本比率が定義不能 (PBR {pbr_val:.2f}倍)"
            elif sec is None or str(sec).strip() in ["", "None", "nan"] or mkt is None or str(mkt).strip() in ["", "None", "nan"]:
                reason = "上場廃止/TOB成立銘柄"
                detail = "東証上場廃止・整理銘柄のため市場区分・業種データなし"
            elif ma_div is None:
                reason = "テクニカル指標算出不能"
                detail = "上場直後等のため25日移動平均乖離率の算出不可"
            elif per_val is not None and per_val > 0 and (pbr_val is None or roe_val is None or eq_ratio is None or eq_ratio <= 0):
                missing_items = []
                if pbr_val is None: missing_items.append("PBR")
                if roe_val is None: missing_items.append("ROE")
                if eq_ratio is None or eq_ratio <= 0: missing_items.append("自己資本比率")
                reason = "重要指標未開示/算出不能"
                detail = f"{'・'.join(missing_items)}のデータ未開示または算出不能"
            elif net_profit == 0 or (roe_val is not None and roe_val == 0):
                reason = "当期利益ゼロ"
                detail = "純利益が0円のため除算不能"
            elif "ETF" in str(sec) or "REIT" in str(sec) or (len(str(c)) == 4 and str(c)[:2] in ["13", "14", "15", "16", "20", "25"] and sec in ["Other", "-"]):
                reason = "ETF/REIT/投資証券"
                detail = "事業会社ではないため企業純利益の概念なし"
            elif not os.path.exists(json_path) and ("Growth" in str(mkt) or str(c).endswith("A") or str(c).endswith("B")):
                reason = "新規IPO直後"
                detail = "上場直後のためEDINET通期本決算XBRLが未開示"
            elif not os.path.exists(json_path):
                reason = "有報未開示/上場廃止・整理ポスト"
                detail = "EDINET開示書類なし（上場廃止または対象外）"
            else:
                reason = "開示利益情報欠落/株式数取得不可"
                detail = "株式数または確定純利益データの取得不能"

            uncalculable_rows.append({
                "Code": c,
                "Name": r.get("name"),
                "Sector": sec,
                "Market": mkt,
                "Price": price_val,
                "ROE": roe_val,
                "PBR": pbr_val,
                "Equity_Ratio": eq_ratio,
                "Uncalculable_Reason": reason,
                "Detail": detail,
            })

    # 評価可能銘柄の Rank 採番 (Score降順)
    evaluable_rows.sort(key=lambda x: x["Agent_Score"], reverse=True)
    for idx, row in enumerate(evaluable_rows, start=1):
        row["Rank"] = idx

    df_evaluable = pd.DataFrame(evaluable_rows)
    df_uncalculable = pd.DataFrame(uncalculable_rows)

    primary_output_dir = os.path.join(ROOT_DIR, "data/output")
    output_dirs = [primary_output_dir]
    # 親リポジトリ側の共有出力先が存在する場合は同期出力
    parent_output_dir = os.path.abspath(os.path.join(ROOT_DIR, "../data/output"))
    if os.path.exists(os.path.dirname(parent_output_dir)):
        output_dirs.append(parent_output_dir)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n💾 6. 分析結果 CSV の直接分離生成・保存 (生成日時: {now_str})...")

    for out_dir in output_dirs:
        os.makedirs(out_dir, exist_ok=True)

        # 1. 評価可能銘柄レポート (daily_report.csv) - 2ファイルのみ出力
        eval_path = os.path.join(out_dir, "daily_report.csv")
        with open(eval_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(f"# Generated At: {now_str}\n")
        df_evaluable.to_csv(eval_path, mode="a", index=False, encoding="utf-8-sig")

        # 2. 論理的算出不能銘柄レポート (uncalculable_stocks.csv)
        uncalc_path = os.path.join(out_dir, "uncalculable_stocks.csv")
        with open(uncalc_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(f"# Generated At: {now_str}\n")
        df_uncalculable.to_csv(uncalc_path, mode="a", index=False, encoding="utf-8-sig")

        print(f"  📄 [評価可能銘柄] {eval_path} ({len(df_evaluable):,} 行 / PER欠損: 0件)")
        print(f"  📄 [算出不能銘柄] {uncalc_path} ({len(df_uncalculable):,} 行 / 理由付き)")

    total_time = time.time() - start_time
    print("=" * 75)
    print(f"✨ 全工程完了！ 総処理時間: {total_time:.2f} 秒 (全 {len(agent_results):,} 銘柄を完全分類・保存)")
    print("=" * 75)


if __name__ == "__main__":
    run_full_universe_pipeline()
