"""EDINET (TurboAcquisitionManager) による全銘柄財務データのバッチ取得 & PER/PBR/自己資本比率算出・DB反映スクリプト"""

import os
import sys
import sqlite3
import json
import logging
import argparse
from dotenv import load_dotenv

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# .env を明示ロード
env_path = os.path.join(ROOT_DIR, ".env")
load_dotenv(env_path, override=True)

from src.fetcher.edinet_fetcher import EdinetFetcher
from src.fetcher.xbrl_parser import XbrlParser
from src.fetcher.turbo_acquisition import TurboAcquisitionManager
from src.config_singleton import ConfigSingleton

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("EDINET_Acquisition")


def main():
    parser = argparse.ArgumentParser(description="EDINET 財務データ一括取得スクリプト")
    parser.add_argument("--days", type=int, default=365, help="スキャン対象日数 (デフォルト: 365日)")
    args = parser.parse_args()

    print("=" * 75, flush=True)
    print(f"🚀 EDINET (Turbo Acquisition) 全銘柄公的開示データ一括取得開始 (対象: 過去 {args.days} 日間)", flush=True)
    api_key = os.getenv("EDINET_API_KEY")
    if api_key:
        print(f"🔑 EDINET_API_KEY ロード成功: {api_key[:6]}...{api_key[-4:]}", flush=True)
    else:
        print("⚠️ EDINET_API_KEY が読み込まれていません。", flush=True)
    print("=" * 75, flush=True)

    config = ConfigSingleton().get_config()
    fetcher = EdinetFetcher(config)
    parser = XbrlParser()
    manager = TurboAcquisitionManager(fetcher, parser, config)

    # 指定日数分の書類をスキャン・ダウンロード・パース
    results = manager.run_turbo_acquisition(days=args.days)
    print(f"\n✅ EDINET オンライン取得完了: {len(results):,} 銘柄の財務データをパース", flush=True)

    if not results:
        print("ℹ️ 対象となる新規提出書類がないため、既存キャッシュをチェックします。", flush=True)
        results_dir = os.path.join(ROOT_DIR, "data/tmp/edinet_results")
        if os.path.exists(results_dir):
            for fname in os.listdir(results_dir):
                if fname.endswith(".json"):
                    code = fname.replace(".json", "")
                    with open(os.path.join(results_dir, fname), "r") as f:
                        results[code] = json.load(f)

    if not results:
        print("⚠️ 有効な EDINET 財務データが見つかりませんでした。", flush=True)
        return

    # 最新株価を DB (market_data) からロードして PER / PBR を計算補完
    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    price_map = {}
    cur_prices = cursor.execute("""
        SELECT code, price FROM market_data
        WHERE entry_date = (SELECT MAX(entry_date) FROM market_data);
    """).fetchall()
    for c, p in cur_prices:
        if p and p > 0:
            price_map[c] = p

    print(f"\n💾 DB (fundamentals) への書き込み更新 ({len(results):,} 件)...", flush=True)

    upsert_sql = """
        INSERT INTO fundamentals (
            code, per, pbr, roe, equity_ratio, sales, operating_margin, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, DATETIME('now', 'localtime'))
        ON CONFLICT(code) DO UPDATE SET
            per = COALESCE(fundamentals.per, EXCLUDED.per),
            pbr = COALESCE(fundamentals.pbr, EXCLUDED.pbr),
            roe = COALESCE(EXCLUDED.roe, fundamentals.roe),
            equity_ratio = COALESCE(EXCLUDED.equity_ratio, fundamentals.equity_ratio),
            sales = COALESCE(EXCLUDED.sales, fundamentals.sales),
            operating_margin = COALESCE(EXCLUDED.operating_margin, fundamentals.operating_margin),
            updated_at = DATETIME('now', 'localtime');
    """

    rows = []
    for code, data in results.items():
        sales = data.get("sales")
        op_income = data.get("operating_income")
        net_profit = data.get("net_profit")
        total_assets = data.get("total_assets")
        net_assets = data.get("net_assets")
        shares = data.get("shares_outstanding")

        op_margin = None
        if sales and op_income and sales > 0:
            op_margin = round((op_income / sales) * 100, 2)

        equity_ratio = None
        if total_assets and net_assets and total_assets > 0:
            equity_ratio = round((net_assets / total_assets) * 100, 2)

        roe = None
        if net_assets and net_profit and net_assets > 0:
            roe = round((net_profit / net_assets) * 100, 2)

        # 株価と株数から PER, PBR を算出
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

        rows.append((
            code,
            per,
            pbr,
            roe,
            equity_ratio,
            sales,
            op_margin,
        ))

    cursor.executemany(upsert_sql, rows)
    conn.commit()
    conn.close()

    print("=" * 75, flush=True)
    print(f"✨ EDINET 最新財務データ取得・DB保存完了！ (合計 {len(rows):,} 銘柄)", flush=True)
    print("=" * 75, flush=True)


if __name__ == "__main__":
    main()
