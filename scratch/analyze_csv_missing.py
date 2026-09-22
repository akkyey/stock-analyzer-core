import os
import pandas as pd
import sqlite3

df = pd.read_csv("data/output/AnalysisResult_Agentic_Latest.csv", comment="#")
print("=== Output CSV Missing Values Breakdown ===", flush=True)
print(f"Total Rows: {len(df)}", flush=True)
print(df.isnull().sum(), flush=True)
print("\nMissing Percentages:", flush=True)
print((df.isnull().sum() / len(df) * 100).round(2).astype(str) + "%", flush=True)

db_path = "data/stock_master.db"
if not os.path.exists(db_path):
    db_path = "../data/stock_master.db"

if os.path.exists(db_path):
    print(f"\n=== SQLite {db_path} Tables ===", flush=True)
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(tables, flush=True)
    
    for t in tables:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  Table '{t}': {cnt} rows", flush=True)

    if "fundamentals" in tables:
        cnt_f = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        print(f"\nFundamentals total rows in DB: {cnt_f}", flush=True)

    conn.close()
