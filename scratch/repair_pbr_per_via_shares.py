"""[PBR/PER完全解消] fast_info.shares & EDINET純資産・当期利益による超高速精密算出・補完スクリプト"""

import os
import sys
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
edinet_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")

def fetch_shares(code: str) -> tuple[str, float | None]:
    sym = f"{code}.T" if "." not in code else code
    try:
        t = yf.Ticker(sym)
        shares = getattr(t.fast_info, "shares", None)
        if shares and shares > 0:
            return code, float(shares)
        
        # fast_info.market_cap と price から逆算
        mc = getattr(t.fast_info, "market_cap", None)
        p = getattr(t.fast_info, "last_price", None)
        if mc and p and p > 0:
            return code, float(mc / p)
    except Exception:
        pass
    return code, None


def main():
    print("=" * 75, flush=True)
    print("🚀 【PBR・PER完全補完】 EDINET確定財務データ × 発行済株式数による精密算出開始", flush=True)
    print("=" * 75, flush=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. 最新株価マップ
    price_map = {
        c: p for c, p in cur.execute(
            "SELECT code, price FROM market_data WHERE entry_date = (SELECT MAX(entry_date) FROM market_data);"
        ).fetchall() if p and p > 0
    }

    # 2. EDINET 解析結果（純資産・当期利益）のロード
    edinet_data = {}
    if os.path.exists(edinet_dir):
        for f in os.listdir(edinet_dir):
            if f.endswith(".json"):
                code = f.replace(".json", "")
                with open(os.path.join(edinet_dir, f), "r") as fp:
                    edinet_data[code] = json.load(fp)

    print(f"📂 EDINET 解析データ: {len(edinet_data):,} 社ロード完了", flush=True)

    # 3. PBR または PER が欠損している銘柄を対象にする
    cur.execute("""
        SELECT code FROM stocks
        WHERE code IN (
            SELECT code FROM fundamentals WHERE pbr IS NULL OR pbr = 0 OR per IS NULL OR per = 0
        )
        ORDER BY code;
    """)
    target_codes = [r[0] for r in cur.fetchall()]
    conn.close()

    total_targets = len(target_codes)
    print(f"📦 PBR/PER補完対象: {total_targets:,} 銘柄", flush=True)

    start_time = time.time()
    shares_map = {}
    completed = 0

    # 4. fast_info による高速並列取得 (15ワーカー)
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_shares, code): code for code in target_codes}
        for fut in as_completed(futures):
            code, shares = fut.result()
            completed += 1
            if shares:
                shares_map[code] = shares
            if completed % 200 == 0 or completed == total_targets:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"⏳ 株式数取得進捗: {completed:,}/{total_targets:,} 銘柄完了 ({completed/total_targets*100:.1f}%) | 速度: {rate:.1f} 銘柄/秒", flush=True)

    print(f"⚡ 株式数取得完了: {len(shares_map):,} 社", flush=True)

    # 5. PBR と PER の精密算出と DB 更新
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    upsert_sql = """
        UPDATE fundamentals
        SET pbr = COALESCE(?, pbr),
            per = COALESCE(?, per),
            updated_at = DATETIME('now', 'localtime')
        WHERE code = ?;
    """

    update_rows = []
    pbr_fixed = 0
    per_fixed = 0

    for code in target_codes:
        shares = shares_map.get(code)
        price = price_map.get(code)
        ed = edinet_data.get(code, {})

        net_assets = ed.get("net_assets")
        net_profit = ed.get("net_profit")

        calc_pbr = None
        calc_per = None

        if price and shares and shares > 0:
            if net_assets and net_assets > 0:
                bps = net_assets / shares
                if bps > 0:
                    calc_pbr = round(price / bps, 2)
                    pbr_fixed += 1

            if net_profit and net_profit > 0:
                eps = net_profit / shares
                if eps > 0:
                    calc_per = round(price / eps, 2)
                    per_fixed += 1

        if calc_pbr or calc_per:
            update_rows.append((calc_pbr, calc_per, code))

    if update_rows:
        cur.executemany(upsert_sql, update_rows)
        conn.commit()

    print(f"\n💾 DB更新完了: {len(update_rows):,} 件反映")
    print(f"   ・ PBR 新規算出補完: {pbr_fixed:,} 社")
    print(f"   ・ PER 新規算出補完: {per_fixed:,} 社")

    # 最終集計
    cnt_pbr = cur.execute("SELECT count(*) FROM fundamentals WHERE pbr IS NOT NULL AND pbr != 0;").fetchone()[0]
    cnt_per = cur.execute("SELECT count(*) FROM fundamentals WHERE per IS NOT NULL AND per != 0;").fetchone()[0]
    total_stocks = cur.execute("SELECT count(*) FROM stocks;").fetchone()[0]
    conn.close()

    total_time = time.time() - start_time
    print("=" * 75, flush=True)
    print(f"✨ 補完完了！ 所要時間: {total_time:.1f} 秒", flush=True)
    print(f"   ・ PBR 有効データ保有率: {cnt_pbr:,} / {total_stocks:,} 社 ({cnt_pbr/total_stocks*100:.1f}%)", flush=True)
    print(f"   ・ PER 有効データ保有率: {cnt_per:,} / {total_stocks:,} 社 ({cnt_per/total_stocks*100:.1f}%)", flush=True)
    print("=" * 75, flush=True)


if __name__ == "__main__":
    main()
