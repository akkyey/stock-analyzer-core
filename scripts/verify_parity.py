"""等価性（パリティ）検証 CLI スクリプト

改修前のゴールデンベースライン（tests/fixtures/golden_parity_baseline.json）と
現行コードの実行結果を突合し、完全等価であることを視覚的に確認・報告する。

使用方法:
    .venv/bin/python scripts/verify_parity.py
"""

import json
import sys
from pathlib import Path

project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from scripts.generate_golden_baseline import build_test_dataset
from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator


def run_verification(config=None, label="Default (config=None)"):
    print("\n=======================================================")
    print(f" 🔍 検証実行: {label}")
    print("=======================================================")

    baseline_path = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "golden_parity_baseline.json"
    )
    if not baseline_path.exists():
        print(f"❌ ゴールデンベースラインが見つかりません: {baseline_path}")
        return False

    with open(baseline_path, "r", encoding="utf-8") as f:
        golden = json.load(f)

    df_candidates, df_liquidity = build_test_dataset()
    passed_df, rejected_df = PreFilter.apply_filter(
        df_candidates, df_liquidity, config=config
    )

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

    # 突合・検証
    all_ok = True

    # 1. 件数チェック
    p_ok = len(passed_df) == golden["passed_count"]
    r_ok = len(rejected_df) == golden["rejected_count"]
    print(
        f"・通過銘柄件数: {len(passed_df)} 件 (基準: {golden['passed_count']}) -> {'✅ 一致' if p_ok else '❌ 不一致'}"
    )
    print(
        f"・除外銘柄件数: {len(rejected_df)} 件 (基準: {golden['rejected_count']}) -> {'✅ 一致' if r_ok else '❌ 不一致'}"
    )
    if not (p_ok and r_ok):
        all_ok = False

    # 2. スコア・判定の突合
    score_diffs = []
    golden_eval_map = {item["code"]: item for item in golden["evaluated_results"]}
    for cur in eval_results:
        code = cur["code"]
        if code not in golden_eval_map:
            score_diffs.append(f"新規銘柄混入: {code}")
            continue
        g = golden_eval_map[code]
        if cur["score"] != g["score"] or cur["verdict"] != g["verdict"]:
            score_diffs.append(
                f"[{code}] {cur['name']}: 現行 {cur['score']}点({cur['verdict']}) != 基準 {g['score']}点({g['verdict']})"
            )

    if not score_diffs and len(eval_results) == len(golden["evaluated_results"]):
        print(
            f"・クオンツスコア & Verdict: 全 {len(eval_results)} 件完全一致 (1ビットの誤差なし) -> ✅ PASS"
        )
    else:
        print("・クオンツスコア & Verdict: 不一致あり -> ❌ FAIL")
        for d in score_diffs:
            print(f"   ⚠️ {d}")
        all_ok = False

    # 3. 除外理由の突合
    reject_diffs = []
    golden_rej_map = {item["code"]: item for item in golden["rejected_results"]}
    for cur in rejected_summary:
        code = cur["code"]
        if code not in golden_rej_map:
            reject_diffs.append(f"新規除外混入: {code}")
            continue
        g = golden_rej_map[code]
        if cur["reason"] != g["reason"] or cur["detail"] != g["detail"]:
            reject_diffs.append(
                f"[{code}] {cur['name']}: 現行 [{cur['reason']}: {cur['detail']}] != 基準 [{g['reason']}: {g['detail']}]"
            )

    if not reject_diffs and len(rejected_summary) == len(golden["rejected_results"]):
        print(f"・PreFilter 除外理由: 全 {len(rejected_summary)} 件完全一致 -> ✅ PASS")
    else:
        print("・PreFilter 除外理由: 不一致あり -> ❌ FAIL")
        for d in reject_diffs:
            print(f"   ⚠️ {d}")
        all_ok = False

    if all_ok:
        print("🎉 判定結果: 【100% 完全等価 (PERFECT EQUIVALENCE)】")
    else:
        print("💥 判定結果: 【差異検知 (REGRESSION DETECTED)】")

    return all_ok


def main():
    print("=================================================================")
    print(" 🛡️  Stock Analyzer Core: 改造前後パリティ（等価性）総合検証")
    print("=================================================================")

    # Case 1: 従来通りの呼び出し (config=None)
    ok1 = run_verification(config=None, label="Case 1: 従来呼び出し (config=None)")

    # Case 2: デフォルト設定の明示指定 (Zero-Breakバイパス検証)
    default_config = {
        "hard_filters": {
            "min_trading_value": 30_000_000.0,
            "min_price": 50.0,
        },
        "strategy_preset": "balanced",
        "scoring_multipliers": {},
    }
    ok2 = run_verification(
        config=default_config,
        label="Case 2: デフォルト設定明示 (balanced / multipliers={})",
    )

    if ok1 and ok2:
        print("\n=================================================================")
        print(" ✅ 総合判定: すべてのケースで改造前と 100% の等価性が実証されました。")
        print("=================================================================\n")
        sys.exit(0)
    else:
        print("\n=================================================================")
        print(" ❌ 総合判定: 一部またはすべてのケースで差異が発生しています。")
        print("=================================================================\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
