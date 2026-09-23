"""QuantAgentEvaluator の単体テスト (Phase 3 検証)"""

import pytest

from src.calc.quant_evaluator import QuantAgentEvaluator


def test_dead_stock_penalty():
    """死に株（RSI 50.0 かつ MA25乖離率 0.0）は一律 45.0点 PASS になること"""
    dossier = {
        "name": "テスト死に株",
        "code": "9999",
        "fundamentals": {
            "roe": 20.0,
            "pbr": 0.5,
            "per": 5.0,
            "equity_ratio": 80.0,
            "dividend_yield": 4.0,
        },
        "technicals": {
            "rsi_14": 50.0,
            "ma25_divergence": 0.0,
            "macd_hist": 0.0,
            "macd_status": "Neutral",
        },
    }
    score, verdict, thesis, risks = QuantAgentEvaluator.evaluate(dossier)
    assert score == 45.0
    assert verdict == "PASS"
    assert thesis == ""
    assert risks == []


def test_macd_none_neutral():
    """macd_hist が None の場合、Neutral として 3.0点が付与され、異常終了や 0.0点にならないこと"""
    dossier = {
        "name": "テスト通常株",
        "code": "1001",
        "fundamentals": {
            "roe": 15.0,
            "pbr": 1.0,
            "per": 10.0,
            "equity_ratio": 60.0,
            "dividend_yield": 2.0,
        },
        "technicals": {
            "rsi_14": 45.0,
            "ma25_divergence": 1.0,
            "macd_hist": None,
            "macd_status": "",
        },
    }
    score, verdict, thesis, risks = QuantAgentEvaluator.evaluate(dossier)
    assert score > 50.0  # スコアが健全に算出されること


def test_rsi_gap_smooth_transition():
    """RSI 65.0 から 75.0 への遷移においてスコアが逆転・破綻せず滑らかに減少すること"""
    base_dossier = {
        "name": "テスト過熱株",
        "code": "1002",
        "fundamentals": {"roe": 10.0, "pbr": 1.5, "per": 12.0, "equity_ratio": 50.0},
        "technicals": {
            "ma25_divergence": 2.0,
            "macd_hist": 0.1,
            "macd_status": "Bullish",
        },
    }

    # RSI 65, 70, 75
    dossier_65 = dict(
        base_dossier, technicals=dict(base_dossier["technicals"], rsi_14=65.0)
    )
    dossier_70 = dict(
        base_dossier, technicals=dict(base_dossier["technicals"], rsi_14=70.0)
    )
    dossier_75 = dict(
        base_dossier, technicals=dict(base_dossier["technicals"], rsi_14=75.0)
    )

    s65, _, _, _ = QuantAgentEvaluator.evaluate(dossier_65)
    s70, _, _, _ = QuantAgentEvaluator.evaluate(dossier_70)
    s75, _, _, _ = QuantAgentEvaluator.evaluate(dossier_75)

    assert s65 > s70 > s75, f"RSIスコアのグラデーション期待値: {s65} > {s70} > {s75}"


def test_ma_divergence_crash_penalty():
    """25日乖離率が -20% 未満の大暴落銘柄はペナルティ減点されること"""
    dossier_normal = {
        "name": "通常リバウンド",
        "code": "1003",
        "fundamentals": {"roe": 10.0, "pbr": 1.0, "per": 10.0, "equity_ratio": 50.0},
        "technicals": {
            "rsi_14": 30.0,
            "ma25_divergence": -15.0,
            "macd_hist": -0.5,
            "macd_status": "Bearish",
        },
    }
    dossier_crash = {
        "name": "大暴落株",
        "code": "1004",
        "fundamentals": {"roe": 10.0, "pbr": 1.0, "per": 10.0, "equity_ratio": 50.0},
        "technicals": {
            "rsi_14": 30.0,
            "ma25_divergence": -35.0,
            "macd_hist": -0.5,
            "macd_status": "Bearish",
        },
    }
    s_norm, _, _, _ = QuantAgentEvaluator.evaluate(dossier_normal)
    s_crash, _, _, _ = QuantAgentEvaluator.evaluate(dossier_crash)

    assert s_crash < s_norm


def test_one_off_profit_trap_suppression():
    """本業赤字なのに特別利益で低PER・高ROEになっている銘柄（千趣会タイプ）はPER配点が抑制されること"""
    dossier_trap = {
        "name": "千趣会タイプ",
        "code": "8165",
        "fundamentals": {
            "roe": 23.1,
            "pbr": 0.35,
            "per": 1.5,
            "equity_ratio": 65.2,
            "dividend_yield": 0.0,
            "operating_income": -2588000000.0,  # 本業赤字
            "net_profit": 3940000000.0,  # 資産売却等による最終黒字
        },
        "technicals": {
            "rsi_14": 37.0,
            "ma25_divergence": -1.1,
            "macd_hist": 0.5,
            "macd_status": "Bullish (Above Signal)",
        },
    }
    score, verdict, _, _ = QuantAgentEvaluator.evaluate(dossier_trap)
    # 本来満点(12点)なら80点超STRONG_BUYになるが、PER0点抑制かつゲートキーパーによりWATCHに制限されること
    assert score < 80.0
    assert verdict == "WATCH"


def test_negative_roe_verdict_cap():
    """実績ROEがマイナスの銘柄は、高スコアであっても Verdict が最大 WATCH に制限されること"""
    # 1. 総合評価関数 evaluate での検証
    dossier_neg_roe = {
        "name": "赤字回復過渡期銘柄",
        "code": "4331",
        "fundamentals": {
            "roe": -0.4,
            "pbr": 0.5,
            "per": 5.0,
            "equity_ratio": 70.0,
            "dividend_yield": 4.5,
        },
        "technicals": {
            "rsi_14": 25.0,
            "ma25_divergence": -5.0,
            "macd_hist": 2.0,
            "macd_status": "Bullish (Above Signal)",
        },
    }
    score, verdict, _, _ = QuantAgentEvaluator.evaluate(dossier_neg_roe)
    assert verdict in ["WATCH", "PASS"]
    assert verdict != "BUY"
    assert verdict != "STRONG_BUY"

    # 2. _determine_verdict ゲートキーパー単体での上限キャップ検証 (仮にスコアが75.0や85.0でもWATCHに落とされること)
    assert (
        QuantAgentEvaluator._determine_verdict(
            score=75.0, roe=-0.5, macd_status="Bullish", ma_div=0.0
        )
        == "WATCH"
    )
    assert (
        QuantAgentEvaluator._determine_verdict(
            score=85.0, roe=-1.2, macd_status="Bullish", ma_div=0.0
        )
        == "WATCH"
    )


def test_bearish_momentum_gatekeeper():
    """MACDが Bearish の銘柄は STRONG_BUY を禁止し、さらに-10%超の下降トレンドは WATCH に制限されること"""
    dossier_bearish = {
        "name": "モメンタム下落株",
        "code": "415A",
        "fundamentals": {
            "roe": 38.5,
            "pbr": 1.56,
            "per": 3.9,
            "equity_ratio": 69.8,
            "dividend_yield": 3.0,
        },
        "technicals": {
            "rsi_14": 45.3,
            "ma25_divergence": -4.1,
            "macd_hist": -0.5,
            "macd_status": "Bearish (Below Signal)",
        },
    }
    _, verdict, _, _ = QuantAgentEvaluator.evaluate(dossier_bearish)
    assert verdict != "STRONG_BUY"

    # さらに深い下落トレンド (-15%) の場合は WATCH へ制限
    dossier_deep_bearish = dict(
        dossier_bearish,
        technicals=dict(dossier_bearish["technicals"], ma25_divergence=-15.0),
    )
    _, verdict_deep, _, _ = QuantAgentEvaluator.evaluate(dossier_deep_bearish)
    assert verdict_deep == "WATCH"
