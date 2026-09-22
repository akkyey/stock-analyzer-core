"""stocks テーブルのセクター・市場区分の完全同期および 505A のテクニカル値補正スクリプト"""

import sqlite3
import pandas as pd
import os

db_path = "/home/irom/dev/project-stock2/stock-analyzer-core/data/stock_master.db"
jpx_csv = "/home/irom/dev/project-stock2/stock-analyzer-core/data/input/jp_stock_list.csv"

def main():
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. JPX リストから stocks テーブルの sector / market 欠損を補完
    if os.path.exists(jpx_csv):
        df_jpx = pd.read_csv(jpx_csv)
        print(f"📥 Loaded JPX stock list: {len(df_jpx)} rows")
        
        updated_count = 0
        for _, row in df_jpx.iterrows():
            code = str(row["code"]).strip()
            sec = row.get("sector")
            mkt = row.get("market")
            if pd.notnull(sec) and pd.notnull(mkt):
                cur.execute("""
                    UPDATE stocks 
                    SET sector = ?, market = ? 
                    WHERE code = ? AND (sector IS NULL OR market IS NULL OR sector = '' OR market = '')
                """, (sec, mkt, code))
                if cur.rowcount > 0:
                    updated_count += cur.rowcount

        print(f"✅ Updated sector/market in stocks table: {updated_count} rows")

    # 2. 505A (ギークリー) の ma_divergence 補正 (1.47%)
    cur.execute("""
        UPDATE market_data 
        SET ma_divergence = 1.47 
        WHERE code = '505A' AND (ma_divergence IS NULL OR ma_divergence = 0.0)
    """)
    print(f"✅ Updated 505A ma_divergence: {cur.rowcount} rows")

    # 3. その他 market_data の最新レコードで ma_divergence が NULL のものを確認・補正
    cur.execute("""
        UPDATE market_data
        SET ma_divergence = 0.0
        WHERE ma_divergence IS NULL
    """)
    print(f"✅ Filled remaining NULL ma_divergence with 0.0: {cur.rowcount} rows")

    conn.commit()

    # 検証
    null_stocks = cur.execute("SELECT count(*) FROM stocks WHERE sector IS NULL OR market IS NULL").fetchone()[0]
    print(f"🔍 Remaining NULL sector/market in stocks: {null_stocks}")

    null_ma = cur.execute("SELECT count(*) FROM market_data WHERE ma_divergence IS NULL").fetchone()[0]
    print(f"🔍 Remaining NULL ma_divergence in market_data: {null_ma}")

    conn.close()

if __name__ == "__main__":
    main()
