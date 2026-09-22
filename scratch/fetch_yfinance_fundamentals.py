"""yfinance から全銘柄のファンダメンタルズ (PER, PBR, 配当利回り, 時価総額, ROE 等) を取得し
stock_master.db の fundamentals テーブルを最新化するスクリプト
"""

import os
import sys
import sqlite3
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def fetch_single_fundamental(code: str) -> dict:
    """単一銘柄の yfinance データを安全にフェッチする"""
    symbol = f"{code}.T" if "." not in code else code
    ticker = yf.Ticker(symbol)
    res = {
        "code": code,
        "per": None,
        "pbr": None,
        "dividend_yield": None,
        "market_cap": None,
        "roe": None,
    }

    try:
        # fast_info から時価総額等を試行
        try:
            fi = ticker.fast_info
            if hasattr(fi, "market_cap") and fi.market_cap:
                res["market_cap"] = int(fi.market_cap)
        except Exception:
            pass

        # info から主要ファンダメンタルズを試行
        info = ticker.info or {}
        if not res["market_cap"]:
            res["market_cap"] = info.get("marketCap")

        res["per"] = info.get("trailingPE") or info.get("forwardPE")
        res["pbr"] = info.get("priceToBook")
        res["dividend_yield"] = info.get("dividendYield")
        res["roe"] = info.get("returnOnEquity")

    except Exception as e:
        pass

    return res


def main():
    print("=" * 75)
    print("🚀 yfinance 全銘柄ファンダメンタルズ一括フェッチ & DB更新 開始")
    print("=" * 75)

    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 対象銘柄コードの読み込み
    cursor.execute("SELECT code FROM stocks ORDER BY code;")
    codes = [r[0] for r in cursor.fetchall()]
    conn.close()

    total_codes = len(codes)
    print(f"📦 対象銘柄数: {total_codes:,} 件")

    print("⚡ yfinance 並列フェッチ開始 (レート制限回避付き)...")
    results = []
    success_cnt = 0
    start_time = time.time()

    # 並列度 8 で実行
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_code = {executor.submit(fetch_single_fundamental, code): code for code in codes}
        
        for idx, future in enumerate(as_completed(future_to_code), start=1):
            data = future.result()
            if data and (data["per"] or data["pbr"] or data["market_cap"]):
                results.append(data)
                success_cnt += 1

            if idx % 200 == 0 or idx == total_codes:
                elapsed = time.time() - start_time
                print(f"  📊 進捗: {idx:,} / {total_codes:,} 完了 (取得成功: {success_cnt:,} 件, 経過時間: {elapsed:.1f}s)")

    print(f"\n💾 DB (fundamentals) への取得結果更新書き込み ({len(results):,} 件)...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # upsert SQL
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

    rows_to_db = [
        (
            r["code"],
            r["per"],
            r["pbr"],
            r["dividend_yield"],
            r["market_cap"],
            r["roe"],
        )
        for r in results
    ]

    cursor.executemany(upsert_sql, rows_to_db)
    conn.commit()
    conn.close()

    print("=" * 75)
    print(f"✨ yfinance ファンダメンタルズ一括フェッチ完了！ (成功更新件数: {success_cnt:,} / {total_codes:,})")
    print("=" * 75)


if __name__ == "__main__":
    main()
