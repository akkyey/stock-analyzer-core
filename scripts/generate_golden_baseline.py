"""ゴールデンベースライン生成スクリプト

改修前の現行ロジックによる実行結果を決定論的データセットに対して生成し、
tests/fixtures/golden_parity_baseline.json に固定化して保存する。
"""

import json
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import polars as pl

from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator


def build_test_dataset() -> tuple[pl.DataFrame, pl.DataFrame]:
    """多角的なテスト銘柄群を構築する"""
    candidates = [
        # 1. 正常・超優良銘柄
        {
            "code": "1001",
            "name": "優良コア株",
            "sector": "情報・通信業",
            "market": "Prime",
            "price": 2000.0,
            "net_assets": 50_000_000_000.0,
            "equity_ratio": 75.0,
            "sales": 80_000_000_000.0,
            "operating_income": 10_000_000_000.0,
            "operating_margin": 12.5,
            "operating_cf": 12_000_000_000.0,
            "net_profit": 7_000_000_000.0,
            "per": 12.0,
            "pbr": 1.1,
            "roe": 18.0,
            "dividend_yield": 3.5,
            "rsi_14": 55.0,
            "ma25_divergence": 3.0,
            "macd_hist": 0.5,
            "macd_status": "Bullish",
        },
        # 2. グロース成長株
        {
            "code": "1002",
            "name": "急成長テック",
            "sector": "サービス業",
            "market": "Growth",
            "price": 3500.0,
            "net_assets": 20_000_000_000.0,
            "equity_ratio": 60.0,
            "sales": 30_000_000_000.0,
            "operating_income": 5_000_000_000.0,
            "operating_margin": 16.7,
            "operating_cf": 4_500_000_000.0,
            "net_profit": 3_500_000_000.0,
            "per": 35.0,
            "pbr": 4.5,
            "roe": 28.0,
            "dividend_yield": 0.5,
            "rsi_14": 68.0,
            "ma25_divergence": 12.0,
            "macd_hist": 1.2,
            "macd_status": "Bullish",
        },
        # 3. ディープバリュー株
        {
            "code": "1003",
            "name": "割安バリュー",
            "sector": "機械",
            "market": "Standard",
            "price": 800.0,
            "net_assets": 30_000_000_000.0,
            "equity_ratio": 82.0,
            "sales": 20_000_000_000.0,
            "operating_income": 2_000_000_000.0,
            "operating_margin": 10.0,
            "operating_cf": 2_500_000_000.0,
            "net_profit": 1_500_000_000.0,
            "per": 5.5,
            "pbr": 0.45,
            "roe": 8.0,
            "dividend_yield": 4.8,
            "rsi_14": 42.0,
            "ma25_divergence": -6.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
        # 4. 高配当ディフェンシブ株
        {
            "code": "1004",
            "name": "高配当インフラ",
            "sector": "電気・ガス業",
            "market": "Prime",
            "price": 1500.0,
            "net_assets": 40_000_000_000.0,
            "equity_ratio": 70.0,
            "sales": 50_000_000_000.0,
            "operating_income": 6_000_000_000.0,
            "operating_margin": 12.0,
            "operating_cf": 7_000_000_000.0,
            "net_profit": 4_000_000_000.0,
            "per": 10.0,
            "pbr": 0.9,
            "roe": 11.0,
            "dividend_yield": 5.2,
            "rsi_14": 48.0,
            "ma25_divergence": -1.0,
            "macd_hist": 0.2,
            "macd_status": "Bullish",
        },
        # 5. バリュートラップ（本業赤字・純利益のみ黒字）
        {
            "code": "2001",
            "name": "特益見かけ黒字",
            "sector": "不動産業",
            "market": "Standard",
            "price": 600.0,
            "net_assets": 10_000_000_000.0,
            "equity_ratio": 40.0,
            "sales": 15_000_000_000.0,
            "operating_income": -500_000_000.0,
            "operating_margin": -3.3,
            "operating_cf": 1_000_000_000.0,
            "net_profit": 2_000_000_000.0,
            "per": 3.5,
            "pbr": 0.5,
            "roe": 15.0,
            "dividend_yield": 2.0,
            "rsi_14": 50.0,
            "ma25_divergence": 2.0,
            "macd_hist": 0.1,
            "macd_status": "Bullish",
        },
        # 6. 実績赤字銘柄（ROE < 0）
        {
            "code": "2002",
            "name": "赤字転落株",
            "sector": "化学",
            "market": "Standard",
            "price": 400.0,
            "net_assets": 8_000_000_000.0,
            "equity_ratio": 35.0,
            "sales": 10_000_000_000.0,
            "operating_income": 300_000_000.0,
            "operating_margin": 3.0,
            "operating_cf": 500_000_000.0,
            "net_profit": -500_000_000.0,
            "per": 14.0,
            "pbr": 0.8,
            "roe": -5.0,
            "dividend_yield": 1.0,
            "rsi_14": 40.0,
            "ma25_divergence": -5.0,
            "macd_hist": -0.2,
            "macd_status": "Bearish",
        },
        # 7. 極小流動性トラップ
        {
            "code": "3001",
            "name": "閑散銘柄",
            "sector": "小売業",
            "market": "Standard",
            "price": 500.0,
            "net_assets": 5_000_000_000.0,
            "equity_ratio": 50.0,
            "sales": 5_000_000_000.0,
            "operating_income": 300_000_000.0,
            "operating_margin": 6.0,
            "operating_cf": 400_000_000.0,
            "net_profit": 200_000_000.0,
            "per": 12.0,
            "pbr": 1.0,
            "roe": 8.0,
            "dividend_yield": 2.0,
            "rsi_14": 52.0,
            "ma25_divergence": 0.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
        # 8. 超低位ボロ株
        {
            "code": "3002",
            "name": "超低位ボロ株",
            "sector": "卸売業",
            "market": "Standard",
            "price": 42.0,
            "net_assets": 2_000_000_000.0,
            "equity_ratio": 30.0,
            "sales": 3_000_000_000.0,
            "operating_income": 100_000_000.0,
            "operating_margin": 3.3,
            "operating_cf": 150_000_000.0,
            "net_profit": 50_000_000.0,
            "per": 20.0,
            "pbr": 0.6,
            "roe": 5.0,
            "dividend_yield": 0.0,
            "rsi_14": 45.0,
            "ma25_divergence": -2.0,
            "macd_hist": -0.1,
            "macd_status": "Bearish",
        },
        # 9. 債務超過
        {
            "code": "3003",
            "name": "債務超過株",
            "sector": "サービス業",
            "market": "Growth",
            "price": 120.0,
            "net_assets": -1_000_000_000.0,
            "equity_ratio": -2.0,
            "sales": 4_000_000_000.0,
            "operating_income": -400_000_000.0,
            "operating_margin": -10.0,
            "operating_cf": -300_000_000.0,
            "net_profit": -800_000_000.0,
            "per": None,
            "pbr": None,
            "roe": None,
            "dividend_yield": 0.0,
            "rsi_14": 30.0,
            "ma25_divergence": -15.0,
            "macd_hist": -0.5,
            "macd_status": "Bearish",
        },
        # 10. 致命的キャッシュ枯渇
        {
            "code": "3004",
            "name": "キャッシュ枯渇株",
            "sector": "輸送用機器",
            "market": "Standard",
            "price": 600.0,
            "net_assets": 10_000_000_000.0,
            "equity_ratio": 45.0,
            "sales": 20_000_000_000.0,
            "operating_income": -1_500_000_000.0,
            "operating_margin": -7.5,
            "operating_cf": -4_000_000_000.0,  # margin = -40/200 = -20% (< -10%)
            "net_profit": -1_800_000_000.0,
            "per": None,
            "pbr": 0.8,
            "roe": -10.0,
            "dividend_yield": 0.0,
            "rsi_14": 38.0,
            "ma25_divergence": -8.0,
            "macd_hist": -0.3,
            "macd_status": "Bearish",
        },
        # 11. 金融業（営業CF免除）
        {
            "code": "3005",
            "name": "免除銀行株",
            "sector": "銀行業",
            "market": "Prime",
            "price": 1800.0,
            "net_assets": 80_000_000_000.0,
            "equity_ratio": 12.0,  # 銀行としては正常
            "sales": 100_000_000_000.0,
            "operating_income": 15_000_000_000.0,
            "operating_margin": 15.0,
            "operating_cf": -25_000_000_000.0,  # 銀行は業態上CFマイナスになりうるが免除
            "net_profit": 11_000_000_000.0,
            "per": 9.0,
            "pbr": 0.7,
            "roe": 12.0,
            "dividend_yield": 4.0,
            "rsi_14": 52.0,
            "ma25_divergence": 1.0,
            "macd_hist": 0.1,
            "macd_status": "Bullish",
        },
        # 12. 商い不成立（出来高ゼロ日あり）
        {
            "code": "3006",
            "name": "商い不成立株",
            "sector": "金属製品",
            "market": "Standard",
            "price": 700.0,
            "net_assets": 8_000_000_000.0,
            "equity_ratio": 55.0,
            "sales": 10_000_000_000.0,
            "operating_income": 800_000_000.0,
            "operating_margin": 8.0,
            "operating_cf": 900_000_000.0,
            "net_profit": 500_000_000.0,
            "per": 11.0,
            "pbr": 0.9,
            "roe": 8.5,
            "dividend_yield": 2.5,
            "rsi_14": 50.0,
            "ma25_divergence": 0.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
        # 13. 取引停止・鮮度不足
        {
            "code": "3007",
            "name": "取引停止株",
            "sector": "食料品",
            "market": "Standard",
            "price": 900.0,
            "net_assets": 12_000_000_000.0,
            "equity_ratio": 65.0,
            "sales": 15_000_000_000.0,
            "operating_income": 1_000_000_000.0,
            "operating_margin": 6.7,
            "operating_cf": 1_200_000_000.0,
            "net_profit": 700_000_000.0,
            "per": 13.0,
            "pbr": 1.0,
            "roe": 9.0,
            "dividend_yield": 2.2,
            "rsi_14": 49.0,
            "ma25_divergence": 0.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
        # 14. 死に株（商い停止判定）
        {
            "code": "4001",
            "name": "死に株",
            "sector": "その他製品",
            "market": "Standard",
            "price": 1000.0,
            "net_assets": 10_000_000_000.0,
            "equity_ratio": 70.0,
            "sales": 12_000_000_000.0,
            "operating_income": 1_000_000_000.0,
            "operating_margin": 8.3,
            "operating_cf": 1_000_000_000.0,
            "net_profit": 600_000_000.0,
            "per": 10.0,
            "pbr": 0.8,
            "roe": 10.0,
            "dividend_yield": 3.0,
            "rsi_14": 50.0,
            "ma25_divergence": 0.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
        # 15. 下降トレンド・モメンタム足切り
        {
            "code": "4002",
            "name": "暴落警戒株",
            "sector": "情報・通信業",
            "market": "Prime",
            "price": 2500.0,
            "net_assets": 30_000_000_000.0,
            "equity_ratio": 75.0,
            "sales": 40_000_000_000.0,
            "operating_income": 6_000_000_000.0,
            "operating_margin": 15.0,
            "operating_cf": 7_000_000_000.0,
            "net_profit": 4_000_000_000.0,
            "per": 12.0,
            "pbr": 1.2,
            "roe": 16.0,
            "dividend_yield": 3.0,
            "rsi_14": 32.0,
            "ma25_divergence": -16.0,
            "macd_hist": -1.5,
            "macd_status": "Bearish",
        },
    ]

    df_candidates = pl.DataFrame(candidates)

    # 流動性指標データフレーム
    liquidity_data = [
        {
            "code": "1001",
            "avg_trading_value_20d": 500_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "1002",
            "avg_trading_value_20d": 800_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "1003",
            "avg_trading_value_20d": 80_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "1004",
            "avg_trading_value_20d": 200_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "2001",
            "avg_trading_value_20d": 50_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "2002",
            "avg_trading_value_20d": 60_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3001",
            "avg_trading_value_20d": 15_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3002",
            "avg_trading_value_20d": 100_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3003",
            "avg_trading_value_20d": 40_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3004",
            "avg_trading_value_20d": 70_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3005",
            "avg_trading_value_20d": 300_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3006",
            "avg_trading_value_20d": 40_000_000.0,
            "zero_volume_days_5d": 1,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "3007",
            "avg_trading_value_20d": 50_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-10",
            "is_recent_trade": False,
        },
        {
            "code": "4001",
            "avg_trading_value_20d": 100_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
        {
            "code": "4002",
            "avg_trading_value_20d": 250_000_000.0,
            "zero_volume_days_5d": 0,
            "latest_trade_date": "2026-03-27",
            "is_recent_trade": True,
        },
    ]

    df_liquidity = pl.DataFrame(liquidity_data)
    return df_candidates, df_liquidity


def generate_baseline():
    df_candidates, df_liquidity = build_test_dataset()

    # 1. PreFilter 実行
    passed_df, rejected_df = PreFilter.apply_filter(df_candidates, df_liquidity)

    # 2. QuantEvaluator 実行 (通過銘柄に対して)
    eval_results = []
    for row in passed_df.to_dicts():
        dossier = {
            "fundamentals": {
                "per": row.get("per"),
                "pbr": row.get("pbr"),
                "roe": row.get("roe"),
                "equity_ratio": row.get("equity_ratio"),
                "dividend_yield": row.get("dividend_yield"),
                "operating_margin": row.get("operating_margin"),
                "operating_income": row.get("operating_income"),
                "net_profit": row.get("net_profit"),
            },
            "technicals": {
                "price": row.get("price"),
                "rsi_14": row.get("rsi_14"),
                "ma25_divergence": row.get("ma25_divergence"),
                "macd_hist": row.get("macd_hist"),
                "macd_status": row.get("macd_status"),
            },
        }
        score, verdict = QuantEvaluator.evaluate(dossier)
        eval_results.append(
            {
                "code": row["code"],
                "name": row["name"],
                "score": score,
                "verdict": verdict,
            }
        )

    # スコア降順ソート
    eval_results.sort(key=lambda x: x["score"], reverse=True)

    # 除外銘柄のリスト
    rejected_summary = [
        {
            "code": r["code"],
            "name": r["name"],
            "reason": r["filter_reason"],
            "detail": r["filter_detail"],
        }
        for r in rejected_df.to_dicts()
    ]
    rejected_summary.sort(key=lambda x: x["code"])

    baseline = {
        "passed_count": len(passed_df),
        "rejected_count": len(rejected_df),
        "evaluated_results": eval_results,
        "rejected_results": rejected_summary,
    }

    out_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "golden_parity_baseline.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)

    print(f"✅ Golden Baseline successfully generated: {out_file}")
    print(f"   Passed Stocks: {len(passed_df)}, Rejected Stocks: {len(rejected_df)}")
    for item in eval_results:
        print(
            f"   [{item['code']}] {item['name']}: {item['score']} pts ({item['verdict']})"
        )


if __name__ == "__main__":
    generate_baseline()
