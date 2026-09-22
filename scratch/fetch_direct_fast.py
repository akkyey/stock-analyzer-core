"""[直接並列取得版] yfinance ファンダメンタルズ高速フェッチスクリプト"""

import os
import sys
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def fetch_one(code: str) -> dict | None:
    sym = f"{code}.T" if "." not in code else code
    try:
        t = yf.Ticker(sym)
        fi = getattr(t, "fast_info", None)
        mc = getattr(fi, "market_cap", None) if fi else None
        
        info = t.info or {}
        market_cap = int(mc) if mc else info.get("marketCap")
        per = info.get("trailingPE") or info.get("forwardPE")
        pbr = info.get("priceToBook")
        div_yield = info.get("dividendYield")
        roe = info.get("returnOnEquity")

        if per or pbr or market_cap or div_yield:
            return {
                "code": code,
                "per": per,
                "pbr": pbr,
                "dividend_yield": div_yield,
                "market_cap": market_cap,
                "roe": roe,
            }
    except Exception:
        pass
    return None


def main():
    print("=" * 75)
    print("🚀 yfinance 迅速ファンダメンタルズフェッチ開始")
    print("=" * 75)

    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM stocks ORDER BY code;")
    all_codes = [r[0] for r in cursor.fetchall()]
    conn.close()

    # 300 銘柄を約10秒で確実取得
    codes = all_codes[:300]
    total = len(codes)
    print(f"📦 フェッチ対象: {total} 銘柄")

    start_time = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)

    print(f"⚡ フェッチ完了: {len(results)} 件成功 (所要時間: {time.time() - start_time:.1f}s)")

    if results:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        upsert_sql = """
            INSERT INTO fundamentals (code, per, pbr, dividend_yield, market_cap, roe, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, DATETIME('now', 'localtime'))
            ON CONFLICT(code) DO UPDATE SET
                per = COALESCE(EXCLUDED.per, fundamentals.per),
                pbr = COALESCE(EXCLUDED.pbr, fundamentals.pbr),
                dividend_yield = COALESCE(EXCLUDED.dividend_yield, fundamentals.dividend_yield),
                market_cap = COALESCE(EXCLUDED.market_cap, fundamentals.market_cap),
                roe = COALESCE(EXCLUDED.roe, fundamentals.roe),
                updated_at = DATETIME('now', 'localtime');
        """
        rows = [
            (r["code"], r["per"], r["pbr"], r["dividend_yield"], r["market_cap"], r["roe"])
            for r in results
        ]
        cursor.executemany(upsert_sql, rows)
        conn.commit()
        conn.close()
        print(f"💾 DBへ {len(results)} 件のファンダメンタルズを正常更新！")

    print("=" * 75)


if __name__ == "__main__":
    main()
