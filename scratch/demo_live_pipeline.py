"""実データ取得＆エージェント協調型パイプライン動作デモ

yfinance から直近のリアル市場データを取得し、
Polars によるテクニカル計算、StockDossier（銘柄カルテ）の生成、
AI エージェント向け投資判断プロンプトの生成までを一気通貫で実演する。
"""

import os
import sys
import polars as pl
import yfinance as yf

# パス解決
sys.path.insert(0, os.path.abspath("."))

from src.fetcher.polars_processor import PolarsProcessor
from src.orchestration.dossier_builder import StockDossierBuilder
from src.ai.prompt_builder import PromptBuilder


def run_live_demo(codes=["7203", "9984"]):
    print(f"📡 1. yfinance から実データを取得中: {codes} ...")
    data_map = {}
    for code in codes:
        ticker = f"{code}.T"
        df = yf.download(ticker, period="3mo", progress=False)
        if df.empty:
            print(f"⚠️ {code} のデータ取得に失敗しました")
            continue
        
        # MultiIndex カラムをフラット化
        if hasattr(df.columns, "levels") and len(df.columns.levels) > 1:
            df.columns = [col[0] for col in df.columns]

        df = df.reset_index()
        # カラム名を小文字化
        df.columns = [c.lower() for c in df.columns]
        # date カラム名を正規化
        if "date" in df.columns:
            df["entry_date"] = df["date"].astype(str)
        df["code"] = code
        if "close" in df.columns:
            df["price"] = df["close"]
        
        data_map[code] = df
        print(f"  ✅ {code}: {len(df)} 日分の株価履歴を取得完了")

    print("\n⚡ 2. PolarsProcessor によるテクニカル指標（RSI, MACD, 移動平均等）のベクトル計算...")
    df_metrics = PolarsProcessor.calc_batch_technicals_vectorized(data_map, latest_only=True)
    
    # 簡易銘柄名・財務の付与（実機ではDB/EDINETから結合）
    master_info = {
        "7203": {"name": "トヨタ自動車", "sector": "輸送用機器", "market": "Prime", "per": 10.5, "pbr": 1.05, "roe": 14.2, "equity_ratio": 55.4},
        "9984": {"name": "ソフトバンクグループ", "sector": "情報・通信業", "market": "Prime", "per": 18.2, "pbr": 1.35, "roe": 8.5, "equity_ratio": 32.0},
    }

    metrics_dicts = df_metrics.to_dicts()
    for row in metrics_dicts:
        code = row["code"]
        if code in master_info:
            row.update(master_info[code])

    print("\n📋 3. 高密度銘柄カルテ (StockDossier) の生成...")
    dossiers = [StockDossierBuilder.from_row(r) for r in metrics_dicts]

    for d in dossiers:
        print("\n" + "=" * 60)
        print(f"🏷️  【銘柄カルテ】 {d['name']} ({d['code']})")
        print("=" * 60)
        price = d['technicals'].get('price') or 0.0
        print(f"現在値: {price:,.1f} 円")
        print(f"着目トリガー: {', '.join(d['trigger_reasons']) or '特になし'}")
        print("\n[テクニカル]")
        for k, v in d['technicals'].items():
            print(f"  - {k}: {v}")
        print("\n[ファンダメンタルズ]")
        for k, v in d['fundamentals'].items():
            print(f"  - {k}: {v}")

        print("\n🤖 4. AI エージェントへ渡される入力プロンプト抜粋:")
        prompt = PromptBuilder.build_dossier_analysis_prompt(d)
        print("-" * 40)
        print(prompt[:600] + "\n... (略) ...\n")
        print("-" * 40)


if __name__ == "__main__":
    run_live_demo()
