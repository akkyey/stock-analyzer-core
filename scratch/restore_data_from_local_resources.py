"""ローカル保存済みマスターデータ (jp_stock_list.csv & fundamentals_resume.json) から DB を一括補完するスクリプト"""

import os
import sys
import sqlite3
import json
import pandas as pd

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
db_path = os.path.join(ROOT_DIR, "data/stock_master.db")

csv_path = os.path.join(ROOT_DIR, "data/input/jp_stock_list.csv")
json_path = os.path.join(ROOT_DIR, "data/fundamentals_resume.json")

print("=" * 75)
print("📦 【DBデータ補完】 ローカルリソースからの高速一括データ復元開始")
print("=" * 75)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 1. Sector & Market の一括補完 (jp_stock_list.csv)
stocks_updated = 0
if os.path.exists(csv_path):
    print(f"📂 銘柄マスタ読み込み: {csv_path}")
    df_stocks = pd.read_csv(csv_path)
    print(f"   -> 読み込み件数: {len(df_stocks):,} 銘柄")
    
    update_stocks_sql = """
        UPDATE stocks
        SET name = COALESCE(NULLIF(?, ''), name),
            sector = COALESCE(NULLIF(?, ''), sector),
            market = COALESCE(NULLIF(?, ''), market),
            updated_at = DATETIME('now', 'localtime')
        WHERE code = ?;
    """
    
    stock_rows = []
    for _, row in df_stocks.iterrows():
        code = str(row["code"]).strip()
        if len(code) == 5 and code.endswith("0"):
            code = code[:4]
        name = str(row.get("name", ""))
        sector = str(row.get("sector", ""))
        market = str(row.get("market", ""))
        stock_rows.append((name, sector, market, code))
        
    cur.executemany(update_stocks_sql, stock_rows)
    stocks_updated = cur.rowcount
    conn.commit()
    print(f"✅ stocks テーブル更新完了: {len(stock_rows):,} 件処理")
else:
    print(f"⚠️ {csv_path} が見つかりません")

# 2. Fundamentals の一括補完 (fundamentals_resume.json)
fund_updated = 0
if os.path.exists(json_path):
    print(f"📂 ファンダメンタルズデータ読み込み: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        fund_data = json.load(f)
    print(f"   -> 読み込み件数: {len(fund_data):,} 銘柄")
    
    upsert_fund_sql = """
        INSERT INTO fundamentals (
            code, per, pbr, roe, dividend_yield, equity_ratio, market_cap,
            operating_cf, payout_ratio, payout_status, current_ratio, quick_ratio,
            sales, sales_growth, sales_status, profit_growth, profit_growth_raw,
            turnaround_status, profit_status, is_turnaround, operating_margin, debt_equity_ratio, free_cf,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, DATETIME('now', 'localtime'))
        ON CONFLICT(code) DO UPDATE SET
            per = COALESCE(EXCLUDED.per, fundamentals.per),
            pbr = COALESCE(EXCLUDED.pbr, fundamentals.pbr),
            roe = COALESCE(EXCLUDED.roe, fundamentals.roe),
            dividend_yield = COALESCE(EXCLUDED.dividend_yield, fundamentals.dividend_yield),
            equity_ratio = COALESCE(EXCLUDED.equity_ratio, fundamentals.equity_ratio),
            market_cap = COALESCE(EXCLUDED.market_cap, fundamentals.market_cap),
            operating_cf = COALESCE(EXCLUDED.operating_cf, fundamentals.operating_cf),
            payout_ratio = COALESCE(EXCLUDED.payout_ratio, fundamentals.payout_ratio),
            payout_status = COALESCE(EXCLUDED.payout_status, fundamentals.payout_status),
            current_ratio = COALESCE(EXCLUDED.current_ratio, fundamentals.current_ratio),
            quick_ratio = COALESCE(EXCLUDED.quick_ratio, fundamentals.quick_ratio),
            sales = COALESCE(EXCLUDED.sales, fundamentals.sales),
            sales_growth = COALESCE(EXCLUDED.sales_growth, fundamentals.sales_growth),
            sales_status = COALESCE(EXCLUDED.sales_status, fundamentals.sales_status),
            profit_growth = COALESCE(EXCLUDED.profit_growth, fundamentals.profit_growth),
            profit_growth_raw = COALESCE(EXCLUDED.profit_growth_raw, fundamentals.profit_growth_raw),
            turnaround_status = COALESCE(EXCLUDED.turnaround_status, fundamentals.turnaround_status),
            profit_status = COALESCE(EXCLUDED.profit_status, fundamentals.profit_status),
            is_turnaround = COALESCE(EXCLUDED.is_turnaround, fundamentals.is_turnaround),
            operating_margin = COALESCE(EXCLUDED.operating_margin, fundamentals.operating_margin),
            debt_equity_ratio = COALESCE(EXCLUDED.debt_equity_ratio, fundamentals.debt_equity_ratio),
            free_cf = COALESCE(EXCLUDED.free_cf, fundamentals.free_cf),
            updated_at = DATETIME('now', 'localtime');
    """
    
    fund_rows = []
    for code, v in fund_data.items():
        if isinstance(v, dict):
            fund_rows.append((
                str(v.get("code", code)),
                v.get("per"),
                v.get("pbr"),
                v.get("roe"),
                v.get("dividend_yield"),
                v.get("equity_ratio"),
                v.get("market_cap"),
                v.get("operating_cf"),
                v.get("payout_ratio"),
                v.get("payout_status"),
                v.get("current_ratio"),
                v.get("quick_ratio"),
                v.get("sales"),
                v.get("sales_growth"),
                v.get("sales_status"),
                v.get("profit_growth"),
                v.get("profit_growth_raw"),
                v.get("turnaround_status"),
                v.get("profit_status"),
                v.get("is_turnaround"),
                v.get("operating_margin"),
                v.get("debt_equity_ratio"),
                v.get("free_cf"),
            ))
            
    cur.executemany(upsert_fund_sql, fund_rows)
    fund_updated = len(fund_rows)
    conn.commit()
    print(f"✅ fundamentals テーブル更新完了: {fund_updated:,} 件登録")
else:
    print(f"⚠️ {json_path} が見つかりません")

# 最終状態の検証
cnt_fund = cur.execute("SELECT count(*) FROM fundamentals WHERE per IS NOT NULL AND per != 0").fetchone()[0]
cnt_sec = cur.execute("SELECT count(*) FROM stocks WHERE sector IS NOT NULL AND sector != ''").fetchone()[0]
total_stocks = cur.execute("SELECT count(*) FROM stocks").fetchone()[0]

conn.close()

print("=" * 75)
print(f"✨ 補完結果検証:")
print(f"   ・ Sector / Market 設定済み: {cnt_sec:,} / {total_stocks:,} 銘柄 ({cnt_sec/total_stocks*100:.1f}%)")
print(f"   ・ PER 有効データ件数:      {cnt_fund:,} / {total_stocks:,} 銘柄 ({cnt_fund/total_stocks*100:.1f}%)")
print("=" * 75)
