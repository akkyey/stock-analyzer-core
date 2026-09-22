"""全銘柄データに対する純粋定量CSVレポート（daily_report.csv）一括生成スクリプト"""

import os
import sys
import sqlite3
import time
import pandas as pd
import polars as pl

ROOT_DIR = "/home/irom/dev/project-stock2/stock-analyzer-core"
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.reporter import StockReporter
from src.calc.engines.polars_engine import PolarsEngine
from src.config_singleton import ConfigSingleton


def main():
    print("=" * 75)
    print("🚀 【新CSV仕様】全銘柄定量レポートデータ一括生成処理 開始")
    print("=" * 75)

    db_path = os.path.join(ROOT_DIR, "data/stock_master.db")
    if not os.path.exists(db_path):
        print(f"❌ データベースが見つかりません: {db_path}")
        return

    print("📦 1. データベースから全銘柄データのロード...")
    conn = sqlite3.connect(db_path)

    query = """
        SELECT 
            m.code, m.entry_date, m.price as close, m.rsi_14, m.ma_divergence, m.macd_hist, m.trend_score, m.trading_value,
            s.name, s.sector, s.market,
            f.per, f.pbr, f.roe, f.dividend_yield, f.equity_ratio, f.market_cap, f.operating_cf, f.free_cf, f.payout_ratio,
            f.current_ratio, f.quick_ratio, f.operating_margin, f.sales_growth, f.profit_growth
        FROM market_data m
        LEFT JOIN stocks s ON m.code = s.code
        LEFT JOIN fundamentals f ON m.code = f.code
        WHERE m.entry_date = (SELECT MAX(entry_date) FROM market_data)
    """

    df_pd = pd.read_sql_query(query, conn)
    conn.close()

    total_count = len(df_pd)
    print(f"  ✅ ロード完了: {total_count:,} 銘柄")

    df_pl = pl.from_pandas(df_pd)

    print("\n⚡ 2. 評価・スコアリング計算エンジンの適用...")
    config = ConfigSingleton().get_config()
    engine = PolarsEngine(config)
    scored_df = engine.calculate_scores(df_pl)

    # 辞書型リスト (results) に変換
    results = []
    for row in scored_df.to_dicts():
        results.append({
            "latest": row,
            "data": row,
        })

    print(f"  ✅ スコア計算完了: 合計 {len(results):,} 銘柄")

    output_dir = os.path.join(ROOT_DIR, "data/output")
    print(f"\n💾 3. 単一決定版 CSV レポートの保存 ({output_dir})...")
    reporter = StockReporter(output_dir=output_dir)
    paths = reporter.generate_reports(results, output_context="daily", limit=None)

    print("\n" + "=" * 75)
    print("✨ 全件レポート生成完了！")
    print(f"  📄 定量レポート (決定版): {paths['summary']}")
    print("=" * 75)


if __name__ == "__main__":
    main()
