"""中間解析済み EDINET データを DB へ同期するスクリプト"""

import os
import json
import sqlite3

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
results_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")
db_path = os.path.join(ROOT_DIR, "data/stock_master.db")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 最新株価マップ
price_map = {
    c: p for c, p in cur.execute(
        "SELECT code, price FROM market_data WHERE entry_date = (SELECT MAX(entry_date) FROM market_data);"
    ).fetchall() if p and p > 0
}

files = [f for f in os.listdir(results_dir) if f.endswith(".json")]
print(f"Importing {len(files)} parsed EDINET files into DB...")

upsert_sql = """
    INSERT INTO fundamentals (
        code, per, pbr, roe, equity_ratio, sales, operating_margin, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, DATETIME('now', 'localtime'))
    ON CONFLICT(code) DO UPDATE SET
        roe = COALESCE(EXCLUDED.roe, fundamentals.roe),
        equity_ratio = COALESCE(EXCLUDED.equity_ratio, fundamentals.equity_ratio),
        sales = COALESCE(EXCLUDED.sales, fundamentals.sales),
        operating_margin = COALESCE(EXCLUDED.operating_margin, fundamentals.operating_margin),
        per = COALESCE(fundamentals.per, EXCLUDED.per),
        pbr = COALESCE(fundamentals.pbr, EXCLUDED.pbr),
        updated_at = DATETIME('now', 'localtime');
"""

rows = []
for f in files:
    code = f.replace(".json", "")
    try:
        with open(os.path.join(results_dir, f), "r") as fp:
            data = json.load(fp)
            sales = data.get("sales")
            op_income = data.get("operating_income")
            net_profit = data.get("net_profit")
            total_assets = data.get("total_assets")
            net_assets = data.get("net_assets")
            shares = data.get("shares_outstanding")

            op_margin = round((op_income / sales) * 100, 2) if (sales and op_income and sales > 0) else None
            equity_ratio = round((net_assets / total_assets) * 100, 2) if (total_assets and net_assets and total_assets > 0) else None
            roe = round((net_profit / net_assets) * 100, 2) if (net_assets and net_profit and net_assets > 0) else None

            per = None
            pbr = None
            price = price_map.get(code)
            if price and shares and shares > 0:
                if net_profit and net_profit > 0:
                    eps = net_profit / shares
                    if eps > 0:
                        per = round(price / eps, 2)
                if net_assets and net_assets > 0:
                    bps = net_assets / shares
                    if bps > 0:
                        pbr = round(price / bps, 2)

            rows.append((code, per, pbr, roe, equity_ratio, sales, op_margin))
    except Exception:
        pass

cur.executemany(upsert_sql, rows)
conn.commit()
print(f"Successfully imported {len(rows)} EDINET records into fundamentals table!")

cnt_eq = cur.execute("SELECT count(*) FROM fundamentals WHERE equity_ratio IS NOT NULL AND equity_ratio != 0;").fetchone()[0]
cnt_pbr = cur.execute("SELECT count(*) FROM fundamentals WHERE pbr IS NOT NULL AND pbr != 0;").fetchone()[0]
cnt_per = cur.execute("SELECT count(*) FROM fundamentals WHERE per IS NOT NULL AND per != 0;").fetchone()[0]
cnt_roe = cur.execute("SELECT count(*) FROM fundamentals WHERE roe IS NOT NULL AND roe != 0;").fetchone()[0]
total = cur.execute("SELECT count(*) FROM stocks;").fetchone()[0]

print(f"DB Status Now:")
print(f"  Equity Ratio valid: {cnt_eq}/{total} ({cnt_eq/total*100:.1f}%)")
print(f"  ROE valid:          {cnt_roe}/{total} ({cnt_roe/total*100:.1f}%)")
print(f"  PBR valid:          {cnt_pbr}/{total} ({cnt_pbr/total*100:.1f}%)")
print(f"  PER valid:          {cnt_per}/{total} ({cnt_per/total*100:.1f}%)")
conn.close()
