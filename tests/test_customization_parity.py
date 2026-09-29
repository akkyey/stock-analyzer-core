"""カスタマイズ後方互換・等価性（パリティ）検証テスト

改造前（現行）のコードで固定化されたゴールデンベースライン（tests/fixtures/golden_parity_baseline.json）と、
実行結果が 1 ビットの狂いもなく完全一致することを検証する。
"""

import json
from pathlib import Path

import pytest

from scripts.generate_golden_baseline import build_test_dataset
from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator


@pytest.fixture
def golden_baseline():
    baseline_path = (
        Path(__file__).resolve().parent / "fixtures" / "golden_parity_baseline.json"
    )
    with open(baseline_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _run_pipeline_simulation(config=None):
    df_candidates, df_liquidity = build_test_dataset()

    # PreFilter
    passed_df, rejected_df = PreFilter.apply_filter(
        df_candidates, df_liquidity, config=config
    )

    # QuantEvaluator
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
        # config 引数はフェーズ2で追加されるため、受け付ける場合のみ渡す
        import inspect

        sig = inspect.signature(QuantEvaluator.evaluate)
        if "config" in sig.parameters:
            score, verdict = QuantEvaluator.evaluate(dossier, config=config)
        else:
            score, verdict = QuantEvaluator.evaluate(dossier)

        eval_results.append(
            {
                "code": row["code"],
                "name": row["name"],
                "score": score,
                "verdict": verdict,
            }
        )

    eval_results.sort(key=lambda x: x["score"], reverse=True)

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

    return {
        "passed_count": len(passed_df),
        "rejected_count": len(rejected_df),
        "evaluated_results": eval_results,
        "rejected_results": rejected_summary,
    }


def test_parity_with_no_config(golden_baseline):
    """config=None（従来呼び出し）でゴールデンベースラインと完全一致することを検証"""
    current = _run_pipeline_simulation(config=None)

    assert current["passed_count"] == golden_baseline["passed_count"]
    assert current["rejected_count"] == golden_baseline["rejected_count"]
    assert current["evaluated_results"] == golden_baseline["evaluated_results"]
    assert current["rejected_results"] == golden_baseline["rejected_results"]


def test_parity_with_default_balanced_preset(golden_baseline):
    """デフォルトの balanced プリセット指定時もゴールデンベースラインと完全一致することを検証（Zero-Breakバイパス検証）"""
    default_config = {
        "hard_filters": {
            "min_trading_value": 30_000_000.0,
            "min_price": 50.0,
        },
        "strategy_preset": "balanced",
        "scoring_multipliers": {},
    }
    current = _run_pipeline_simulation(config=default_config)

    assert current["passed_count"] == golden_baseline["passed_count"]
    assert current["rejected_count"] == golden_baseline["rejected_count"]
    assert current["evaluated_results"] == golden_baseline["evaluated_results"]
    assert current["rejected_results"] == golden_baseline["rejected_results"]
