"""PER 最終補正 & 算出不能銘柄の理由究明・レポート生成スクリプト"""

import os
import sys
import json
import sqlite3
import pandas as pd
import yfinance as yf

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
edinet_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")
csv_path = os.path.join(ROOT_DIR, "data/output/daily_report.csv")

def main():
    print("=" * 75, flush=True)
    print("🔍 【PER最終チェック & 補正プログラム】 実行開始", flush=True)
    print("=" * 75, flush=True)

    df = pd.read_csv(csv_path, comment="#")
    null_per_df = df[df["PER"].isnull()]
    print(f"📊 現在の PER 欠損銘柄数: {len(null_per_df)} 社", flush=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 最新株価マップ
    price_map = {
        c: p for c, p in cur.execute(
            "SELECT code, price FROM market_data WHERE entry_date = (SELECT MAX(entry_date) FROM market_data);"
        ).fetchall() if p and p > 0
    }

    repaired_count = 0
    reason_records = []

    update_sql = "UPDATE fundamentals SET per = ?, updated_at = DATETIME('now', 'localtime') WHERE code = ?;"

    for _, row in null_per_df.iterrows():
        code = str(row["Code"])
        name = str(row["Name"])
        sec = str(row.get("Sector", ""))
        mkt = str(row.get("Market", ""))
        pbr = row.get("PBR")
        roe = row.get("ROE")
        price = price_map.get(code, row.get("Price"))

        # 1. EDINET データの確認
        json_path = os.path.join(edinet_dir, f"{code}.json")
        net_profit = None
        sales = None
        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as fp:
                    ed = json.load(fp)
                    net_profit = ed.get("net_profit")
                    sales = ed.get("sales")
            except Exception:
                pass

        # 2. 再補正チェック: もし黒字利益があり、株価と株式数が取れるなら再計算
        calc_per = None
        if price and net_profit and net_profit > 0:
            try:
                t = yf.Ticker(f"{code}.T")
                shares = getattr(t.fast_info, "shares", None)
                if shares and shares > 0:
                    eps = net_profit / shares
                    if eps > 0:
                        calc_per = round(price / eps, 2)
            except Exception:
                pass

        if calc_per:
            cur.execute(update_sql, (calc_per, code))
            repaired_count += 1
            continue

        # 3. 補正不能な理由の特定
        reason = "理由不明"
        detail = ""

        # A. 最終赤字 (純損失 / ROEマイナス)
        if (roe is not None and pd.notna(roe) and float(roe) < 0) or (net_profit is not None and net_profit < 0):
            reason = "当期純損失 (最終赤字)"
            loss_str = f"{net_profit:,.0f}円" if net_profit else f"ROE {roe:.1f}%"
            detail = f"純損失のためPER定義不可 ({loss_str})"

        # B. 利益ゼロ / 収益なし
        elif net_profit == 0 or (roe is not None and pd.notna(roe) and float(roe) == 0):
            reason = "当期利益ゼロ (無益)"
            detail = "純利益が0円のためPER算出不能"

        # C. ETF / 投資信託 / REIT
        elif "ETF" in sec or "REIT" in sec or (len(code) == 4 and code[:2] in ["13", "14", "15", "16", "20", "25"] and sec in ["Other", "-"]):
            reason = "ETF/REIT/投資証券"
            detail = "事業会社ではないため企業当期利益の概念なし"

        # D. 新規上場 (IPO) 直後で初回決算未提出
        elif not os.path.exists(json_path) and ("Growth" in mkt or code.endswith("A") or code.endswith("B")):
            reason = "新規IPO直後 (初回有報未提出)"
            detail = "上場直後のためEDINET本決算XBRLが未開示"

        # E. 非上場化 / 監理・整理ポスト / 株式併合中
        elif not os.path.exists(json_path):
            reason = "有報未開示/上場廃止・整理ポスト"
            detail = "EDINET開示書類なし"

        else:
            reason = "開示利益情報欠落/株式数取得不可"
            detail = "株式数または確定利益データの取得不能"

        reason_records.append({
            "Code": code,
            "Name": name,
            "Sector": sec,
            "Market": mkt,
            "Price": price,
            "ROE": roe,
            "PBR": pbr,
            "Reason_Category": reason,
            "Detail": detail,
        })

    conn.commit()
    conn.close()

    print(f"✨ 再補正チェック完了: {repaired_count} 件を新規算出・救済！", flush=True)
    print(f"📝 補正不能（算出不可）銘柄: {len(reason_records)} 件", flush=True)

    # 理由別集計
    rdf = pd.DataFrame(reason_records)
    print("\n" + "=" * 75, flush=True)
    print("📊 【PER算出不能 理由別内訳集計】", flush=True)
    print("=" * 75, flush=True)
    print(rdf["Reason_Category"].value_counts().to_string(), flush=True)

    # レポートCSVの保存
    reason_csv_path = os.path.join(ROOT_DIR, "data/output/per_missing_reasons.csv")
    rdf.to_csv(reason_csv_path, index=False, encoding="utf-8-sig")
    print(f"\n📄 理由明細一覧を保存: {reason_csv_path}", flush=True)

    # サンプル表示
    print("\n" + "=" * 75, flush=True)
    print("📋 【算出不能銘柄のサンプル (先頭25社)】", flush=True)
    print("=" * 75, flush=True)
    print(rdf[["Code", "Name", "Reason_Category", "Detail"]].head(25).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
