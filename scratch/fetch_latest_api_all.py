"""[最新APIデータ一括取得] 残り1,766社・超高速バッチフェッチ (yfinance内部curl_cffi)"""

import os
import sys
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

db_path = os.path.join(ROOT_DIR, "data/stock_master.db")

def fetch_single_safe(code: str) -> dict:
    sym = f"{code}.T" if "." not in code else code
    item = {
        "code": code,
        "per": None,
        "pbr": None,
        "dividend_yield": None,
        "market_cap": None,
        "roe": None,
    }

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

        if item["dividend_yield"] and item["dividend_yield"] > 1:
            item["dividend_yield"] = item["dividend_yield"] / 100.0
        if item["roe"] and abs(item["roe"]) > 1:
            item["roe"] = item["roe"] / 100.0

    except Exception:
        pass

    return item


def main():
    print("=" * 75, flush=True)
    print("🚀 【最新API再取得】 残り未取得銘柄（6000番台以降等）の高速一括フェッチ開始", flush=True)
    print("=" * 75, flush=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # PERとPBRが共に未設定の銘柄を抽出
    target_query = """
        SELECT s.code FROM stocks s
        LEFT JOIN fundamentals f ON s.code = f.code
        WHERE (f.per IS NULL OR f.per = 0) AND (f.pbr IS NULL OR f.pbr = 0)
        ORDER BY s.code;
    """
    cur.execute(target_query)
    target_codes = [r[0] for r in cur.fetchall()]
    conn.close()

    total_codes = len(target_codes)
    print(f"📦 残りフェッチ対象: {total_codes:,} 銘柄", flush=True)

    if total_codes == 0:
        print("✨ すべての銘柄が取得完了しています！", flush=True)
        return

    start_time = time.time()
    completed = 0
    newly_saved = 0

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

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    batch_size = 50
    # 8並列スレッドで高速・安定取得
    with ThreadPoolExecutor(max_workers=8) as executor:
        for i in range(0, total_codes, batch_size):
            batch_codes = target_codes[i:i + batch_size]
            futures = [executor.submit(fetch_single_safe, code) for code in batch_codes]

            fund_rows = []
            for fut in as_completed(futures):
                r = fut.result()
                completed += 1
                if r["per"] or r["pbr"] or r["market_cap"] or r["dividend_yield"] or r["roe"]:
                    fund_rows.append((r["code"], r["per"], r["pbr"], r["dividend_yield"], r["market_cap"], r["roe"]))

            if fund_rows:
                cur.executemany(upsert_fund_sql, fund_rows)
                conn.commit()
                newly_saved += len(fund_rows)

            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            print(f"⏳ 進捗: {completed:,}/{total_codes:,} 銘柄完了 ({completed/total_codes*100:.1f}%) | DB保存成功: {newly_saved:,}社 | 速度: {rate:.1f} 銘柄/秒", flush=True)

    conn.close()

    total_time = time.time() - start_time
    print("=" * 75, flush=True)
    print(f"✨ 全件フェッチ・DB更新完了！ 総時間: {total_time:.1f}s | 新規保存数: {newly_saved:,}社", flush=True)
    print("=" * 75, flush=True)


if __name__ == "__main__":
    main()
