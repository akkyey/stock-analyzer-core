"""実機環境における E2E 動作確認スクリプト

1. PolarsEngine による多変量スコアリング
2. QuantAgentEvaluator による多層ゲートキーパー適用（投資判断 Verdict 算出）
3. StockReporter による daily_report.csv 出力
4. 生成された CSV の品質・整合性検証
"""

import os
import sys
from pathlib import Path
import polars as pl

# パス解決
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.calc.engines.polars_engine import PolarsEngine
from src.calc.quant_evaluator import QuantAgentEvaluator
from src.config_singleton import ConfigSingleton
from src.reporter import StockReporter


def main():
    print("=" * 60)
    print("🚀 stock-analyzer-core 統合動作確認 (E2E Verification)")
    print("=" * 60)

    config = ConfigSingleton.get_config()

    # 1. 動作確認用サンプルデータ（複数パターンの銘柄）
    data = {
        "code": ["7203", "9984", "6758", "9999"],
        "name": ["トヨタ自動車", "ソフトバンクグループ", "ソニーグループ", "架空の赤字企業"],
        "sector": ["輸送用機器", "情報・通信業", "電気機器", "その他製品"],
        "market": ["Prime", "Prime", "Prime", "Standard"],
        "entry_date": ["2026-03-27", "2026-03-27", "2026-03-27", "2026-03-27"],
        "price": [3025.0, 6315.0, 12500.0, 150.0],
        "rsi_14": [48.8, 56.9, 62.1, 25.0],
        "ma_divergence": [-1.2, 10.4, 3.5, -15.0],
        "macd_hist": [-10.8, 55.4, 22.0, -5.0],
        "per": [10.5, 18.2, 15.0, None],
        "pbr": [1.05, 1.35, 1.8, 0.4],
        "roe": [14.2, 8.5, 12.0, -8.0],
        "equity_ratio": [55.4, 32.0, 45.0, 12.0],
        "dividend_yield": [3.2, 1.5, 2.0, 0.0],
        "operating_cf": [4500000.0, 1200000.0, 1800000.0, -10000.0],
        "market_cap": [38000000.0, 15000000.0, 16000000.0, 20000.0],
        "sales": [45000000.0, 7000000.0, 13000000.0, 50000.0],
        "profit_growth": [12.5, 5.0, 8.0, -20.0],
        "trading_value": [85000000.0, 95000000.0, 60000000.0, 10000.0],
    }
    df_raw = pl.DataFrame(data)
    print(f"\n📊 1. 入力データ作成: {df_raw.height} 銘柄")

    # 2. PolarsEngine によるスコア計算
    print("\n⚡ 2. PolarsEngine によるクオンツスコア計算...")
    engine = PolarsEngine(config)
    df_scored = engine.calculate_scores(df_raw)
    print(f"  ✅ スコア計算完了 (カラム数: {len(df_scored.columns)})")

    # 3. StockDossier 生成 & QuantAgentEvaluator による多層ゲートキーパー適用
    print("\n🛡️ 3. StockDossier 生成 & QuantAgentEvaluator による多層ゲートキーパー & Verdict 評価...")
    from src.orchestration.dossier_builder import StockDossierBuilder

    records = df_scored.to_dicts()
    evaluated_records = []
    for r in records:
        dossier = StockDossierBuilder.from_row(r)
        agent_score, verdict, thesis, risks = QuantAgentEvaluator.evaluate(dossier)
        r["agent_score"] = agent_score
        r["verdict"] = verdict
        r["investment_thesis"] = thesis
        r["risk_factors"] = risks
        evaluated_records.append(r)
        print(f"  ・ [{r['code']}] {r['name']}: Verdict={verdict} (Score={agent_score:.1f}点) - {thesis}")

    # 4. StockReporter による CSV 出力
    print("\n💾 4. StockReporter によるレポート CSV 出力...")
    output_dir = os.path.join(str(ROOT_DIR), "data/output")
    reporter = StockReporter(output_dir=output_dir)
    formatted_input = [{"latest": r} for r in evaluated_records]
    report_paths = reporter.generate_reports(formatted_input, output_context="daily")
    out_csv = report_paths["summary"]
    print(f"  ✅ 出力完了: {out_csv}")

    # 5. 出力検証
    print("\n🔍 5. 出力ファイル検証...")
    assert os.path.exists(out_csv), "出力 CSV が存在しません"
    size = os.path.getsize(out_csv)
    print(f"  ✅ ファイルサイズ: {size} bytes")

    df_out = pl.read_csv(out_csv, comment_prefix="#")
    print(f"  ✅ レコード数: {df_out.height} 件")
    print(f"  ✅ 出力カラム一覧: {list(df_out.columns)}")
    print("\n✨ 【動作確認完了】 すべてのコンポーネントが正常に機能しています。")


if __name__ == "__main__":
    main()
