"""評価可能銘柄 (daily_report.csv) と論理的算出不能銘柄 (uncalculable_stocks.csv) の完全分離出力スクリプト"""

import os
import sys
import json
import sqlite3
from datetime import datetime
import pandas as pd

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
edinet_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")
source_csv = os.path.join(ROOT_DIR, "data/output/daily_report.csv")

def main():
    print("=" * 75, flush=True)
    print("🔄 【データ分離出力】 評価可能銘柄と論理的算出不能銘柄のCSV分割処理開始", flush=True)
    print("=" * 75, flush=True)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 元のスクリーニング結果全件読み込み
    df = pd.read_csv(source_csv, comment="#")
    print(f"📊 全評価対象銘柄: {len(df):,} 社", flush=True)

    # 1. 評価可能銘柄: PERが正常に算出できている（黒字・適格）銘柄
    evaluable_df = df[df["PER"].notnull()].copy()
    
    # 順位（Rank）を再採番 (1〜N)
    evaluable_df = evaluable_df.sort_values(by="Agent_Score", ascending=False).reset_index(drop=True)
    evaluable_df["Rank"] = range(1, len(evaluable_df) + 1)

    # 2. 論理的算出不能銘柄: PERが算出不能（赤字・未開示等）の銘柄
    uncalc_df = df[df["PER"].isnull()].copy()

    # 理由を付与
    reasons = []
    details = []

    for _, row in uncalc_df.iterrows():
        code = str(row["Code"])
        sec = str(row.get("Sector", ""))
        mkt = str(row.get("Market", ""))
        roe = row.get("ROE")

        # EDINET から純利益を取得
        json_path = os.path.join(edinet_dir, f"{code}.json")
        net_profit = None
        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as fp:
                    d = json.load(fp)
                    net_profit = d.get("net_profit")
            except Exception:
                pass

        if (roe is not None and pd.notna(roe) and float(roe) < 0) or (net_profit is not None and net_profit < 0):
            reason = "当期純損失 (最終赤字)"
            loss_str = f"{net_profit:,.0f}円" if net_profit else f"ROE {roe:.1f}%"
            detail = f"純損失のためPER定義不可 ({loss_str})"
        elif net_profit == 0 or (roe is not None and pd.notna(roe) and float(roe) == 0):
            reason = "当期利益ゼロ"
            detail = "純利益が0円のため除算不能"
        elif "ETF" in sec or "REIT" in sec or (len(code) == 4 and code[:2] in ["13", "14", "15", "16", "20", "25"] and sec in ["Other", "-"]):
            reason = "ETF/REIT/投資証券"
            detail = "事業会社ではないため企業純利益の概念なし"
        elif not os.path.exists(json_path) and ("Growth" in mkt or code.endswith("A") or code.endswith("B")):
            reason = "新規IPO直後"
            detail = "上場直後のためEDINET通期本決算XBRLが未開示"
        elif not os.path.exists(json_path):
            reason = "有報未開示/上場廃止・整理ポスト"
            detail = "EDINET開示書類なし"
        else:
            reason = "開示利益情報欠落/株式数取得不可"
            detail = "株式数または確定純利益データの取得不能"

        reasons.append(reason)
        details.append(detail)

    uncalc_df["Uncalculable_Reason"] = reasons
    uncalc_df["Detail"] = details

    # 不要なカラム整理（算出不能銘柄用）
    uncalc_cols = [
        "Code", "Name", "Sector", "Market", "Price", "ROE", "PBR", "Equity_Ratio",
        "Uncalculable_Reason", "Detail"
    ]
    uncalc_df_clean = uncalc_df[uncalc_cols].copy()

    # 3. CSV ファイル出力
    out_dir = os.path.join(ROOT_DIR, "data/output")
    os.makedirs(out_dir, exist_ok=True)

    evaluable_csv = os.path.join(out_dir, "daily_report.csv")
    uncalc_csv = os.path.join(out_dir, "uncalculable_stocks.csv")

    # A. 評価可能銘柄の出力
    with open(evaluable_csv, "w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# Generated At: {now_str}\n")
    evaluable_df.to_csv(evaluable_csv, mode="a", index=False, encoding="utf-8-sig")

    # B. 論理的算出不能銘柄の出力
    with open(uncalc_csv, "w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# Generated At: {now_str}\n")
    uncalc_df_clean.to_csv(uncalc_csv, mode="a", index=False, encoding="utf-8-sig")

    print(f"\n✅ 1. 評価可能銘柄 CSV: {evaluable_csv}")
    print(f"   ・ 銘柄数: {len(evaluable_df):,} 社 (PER欠損: 0件 / 完全クリーン)")
    print(f"\n✅ 2. 論理的算出不能銘柄 CSV: {uncalc_csv}")
    print(f"   ・ 銘柄数: {len(uncalc_df_clean):,} 社 (理由明記)")

    print("\n" + "=" * 75, flush=True)
    print("✨ 分割出力完了！", flush=True)
    print("=" * 75, flush=True)


if __name__ == "__main__":
    main()
