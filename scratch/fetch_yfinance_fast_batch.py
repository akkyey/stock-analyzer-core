"""[高信頼性取得版] yfinance 全銘柄ファンダメンタルズ & 業種・市場区分並列フェッチ・DB更新スクリプト"""

import os
import sys
import sqlite3
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import argparse
import requests

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

SECTOR_MAP = {
    "Technology": "電気機器・IT",
    "Financial Services": "金融・銀行",
    "Consumer Cyclicals": "一般消費財・小売",
    "Industrials": "機械・建設",
    "Healthcare": "医薬品・ヘルスケア",
    "Basic Materials": "素材・化学",
    "Real Estate": "不動産",
    "Consumer Defensive": "食品・生活用品",
    "Utilities": "電力・ガス",
    "Communication Services": "情報通信",
    "Energy": "エネルギー・石油",
}

def fetch_single(code: str) -> dict:
    sym = f"{code}.T" if "." not in code else code
    item = {
        "code": code,
        "per": None,
        "pbr": None,
        "dividend_yield": None,
        "market_cap": None,
        "roe": None,
        "sector": None,
        "market": None,
    }
    
    # 軽度のジッターを挟んでレートリミットを回避
    time.sleep(random.uniform(0.02, 0.08))

    try:
        t = yf.Ticker(sym)
        fi = getattr(t, "fast_info", None)
        if fi:
            mc = getattr(fi, "market_cap", None)
            if mc:
                item["market_cap"] = int(mc)

        info = getattr(t, "info", None) or {}
        if not item["market_cap"]:
            item["market_cap"] = info.get("marketCap")

        item["per"] = info.get("trailingPE") or info.get("forwardPE")
        item["pbr"] = info.get("priceToBook")
        item["dividend_yield"] = info.get("dividendYield")
        item["roe"] = info.get("returnOnEquity")

        raw_sec = info.get("sector")
        if raw_sec:
            item["sector"] = SECTOR_MAP.get(raw_sec, raw_sec)
        
        raw_ind = info.get("industry")
        if raw_ind:
            item["market"] = raw_ind

    except Exception:
        pass

    return item


def main():
    parser = argparse.ArgumentParser(description="yfinance 全件データ取得スクリプト")
    parser.add_argument("--limit", type=int, default=None, help="取得上限")
    parser.add_argument("--workers", type=int, default=5, help="並列ワーカー数 (デフォルト: 5)")
    args = parser.parse_args()

    print("=" * 75)
    print("⚡ 【高精度モード】 yfinance 全銘柄ファンダメンタルズ & 業種一括フェッチ開始")
    print("=" * 75)

    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM stocks ORDER BY code;")
    all_codes = [r[0] for r in cursor.fetchall()]
    conn.close()

    codes = all_codes[:args.limit] if args.limit else all_codes
    total_codes = len(codes)
    print(f"📦 対象銘柄数: {total_codes:,} 銘柄 (並列ワーカー数: {args.workers})")

    start_time = time.time()
    completed_count = 0
    fund_updated = 0
    stock_updated = 0

    upsert_fund_sql = """
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

    update_stocks_sql = """
        UPDATE stocks
        SET sector = COALESCE(?, sector),
            market = COALESCE(?, market),
            updated_at = DATETIME('now', 'localtime')
        WHERE code = ?;
    """

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    batch_size = 50
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for i in range(0, total_codes, batch_size):
            batch_codes = codes[i:i + batch_size]
            futures = [executor.submit(fetch_single, code) for code in batch_codes]

            fund_rows = []
            stock_rows = []
            for fut in as_completed(futures):
                r = fut.result()
                completed_count += 1
                if r["per"] or r["pbr"] or r["market_cap"] or r["dividend_yield"] or r["roe"]:
                    fund_rows.append((r["code"], r["per"], r["pbr"], r["dividend_yield"], r["market_cap"], r["roe"]))
                if r["sector"] or r["market"]:
                    stock_rows.append((r["sector"], r["market"], r["code"]))

            if fund_rows:
                cursor.executemany(upsert_fund_sql, fund_rows)
                fund_updated += len(fund_rows)
            if stock_rows:
                cursor.executemany(update_stocks_sql, stock_rows)
                stock_updated += len(stock_rows)

            conn.commit()

            elapsed = time.time() - start_time
            rate = completed_count / elapsed if elapsed > 0 else 0
            print(f"⏳ 進捗: {completed_count:,}/{total_codes:,} 銘柄完了 ({completed_count/total_codes*100:.1f}%) | PER取得: {fund_updated:,}件 | 業種取得: {stock_updated:,}件 | 速度: {rate:.1f} 銘柄/秒")

    conn.close()

    total_time = time.time() - start_time
    print("=" * 75)
    print(f"✨ 全件フェッチ・DB更新完了！ 総時間: {total_time:.1f}s | fundamentals更新: {fund_updated:,}件 | stocks更新: {stock_updated:,}件")
    print("=" * 75)


if __name__ == "__main__":
    main()
