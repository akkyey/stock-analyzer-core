"""PBR / ROE / 自己資本比率の欠損補正および厳格化スクリプト"""

import sqlite3
import yfinance as yf

db_path = "/home/irom/dev/project-stock2/stock-analyzer-core/data/stock_master.db"

def main():
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. 8253 (クレディセゾン)
    try:
        t = yf.Ticker("8253.T")
        inf = t.info
        pbr = inf.get("priceToBook")
        if pbr:
            cur.execute("UPDATE fundamentals SET pbr = ? WHERE code = '8253'", (round(float(pbr), 2),))
            print(f"✅ 8253 PBR updated: {pbr}")
        
        # 8253 の自己資本比率: 金融業（総資産 約4.5兆円、純資産 約7000億円 => 約15%前後）
        # yfinance の balance sheet または financial data から
        cur.execute("SELECT equity_ratio FROM fundamentals WHERE code = '8253'")
        eq = cur.fetchone()[0]
        if eq is None:
            # yfinance から取得試行
            total_assets = inf.get("totalAssets")
            book_val = inf.get("bookValue")
            shares = t.fast_info.shares
            if total_assets and book_val and shares:
                equity = book_val * shares
                calc_eq = round((equity / total_assets) * 100, 2)
                cur.execute("UPDATE fundamentals SET equity_ratio = ? WHERE code = '8253'", (calc_eq,))
                print(f"✅ 8253 Equity Ratio updated: {calc_eq}%")
    except Exception as e:
        print(f"⚠️ 8253 update error: {e}")

    # 2. PBR と PER が存在し、ROE が欠損している銘柄 (ROE = PBR / PER * 100)
    cur.execute("""
        UPDATE fundamentals 
        SET roe = ROUND((pbr / per) * 100.0, 2)
        WHERE (roe IS NULL OR roe = 0) AND per IS NOT NULL AND per > 0 AND pbr IS NOT NULL AND pbr > 0
    """)
    print(f"✅ Updated ROE via (PBR/PER)*100: {cur.rowcount} rows")

    # 3. ROE と PER が存在し、PBR が欠損している銘柄 (PBR = PER * ROE / 100)
    cur.execute("""
        UPDATE fundamentals 
        SET pbr = ROUND((per * roe) / 100.0, 2)
        WHERE (pbr IS NULL OR pbr = 0) AND per IS NOT NULL AND per > 0 AND roe IS NOT NULL AND roe > 0
    """)
    print(f"✅ Updated PBR via (PER*ROE)/100: {cur.rowcount} rows")


    conn.commit()
    conn.close()
    print("✨ DB update completed successfully.")

if __name__ == "__main__":
    main()
