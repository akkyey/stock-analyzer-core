"""カスタマイズ機能・拡張性・正規化計算・パス解決・Stage-and-Sync 総合テスト

設計書 (customization_architecture_design.md & colab_execution_architecture_and_guide_design.md) に
規定されたすべての制約と動作を厳密に検証する。
"""

import os
import tempfile
from pathlib import Path

import polars as pl
import pytest

# =====================================================================
# 1. Zero-Break 保証 & 正規化経路の機械的検算テスト
# =====================================================================
from polars.testing import assert_frame_equal

from scripts.generate_golden_baseline import build_test_dataset
from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator
from src.utils.colab_sync import ColabSyncManager
from src.utils.path_resolver import PathResolver


def test_zero_break_bypass_exact_match():
    """設定なしと balanced プリセットで 1 ビットの狂いもなく完全一致することを検証"""
    df_candidates, df_liquidity = build_test_dataset()
    p_none, _ = PreFilter.apply_filter(df_candidates, df_liquidity, config=None)
    p_balanced, _ = PreFilter.apply_filter(
        df_candidates,
        df_liquidity,
        config={"strategy_preset": "balanced", "scoring_multipliers": {}},
    )

    assert_frame_equal(p_none, p_balanced)

    for row in p_none.to_dicts():
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
        score1, verdict1 = QuantEvaluator.evaluate(dossier, config=None)
        score2, verdict2 = QuantEvaluator.evaluate(
            dossier,
            config={"strategy_preset": "balanced", "scoring_multipliers": {}},
        )

        assert score1 == score2
        assert verdict1 == verdict2


def test_normalization_scaling_invariance():
    """全乗数を一律 2.0 に設定した場合、正規化経路（除算・乗算）を経由しても基準と同一になることを検証"""
    dossier = {
        "fundamentals": {
            "per": 12.0,
            "pbr": 1.1,
            "roe": 18.0,
            "equity_ratio": 75.0,
            "dividend_yield": 3.5,
            "operating_margin": 12.5,
            "operating_income": 10_000_000.0,
            "net_profit": 7_000_000.0,
        },
        "technicals": {
            "price": 2000.0,
            "rsi_14": 55.0,
            "ma25_divergence": 3.0,
            "macd_hist": 0.5,
            "macd_status": "Bullish",
        },
    }

    base_score, base_verdict = QuantEvaluator.evaluate(dossier, config=None)

    # 全乗数を一律 2.0 に設定 (比率は 1:1:1:1:1 のまま正規化パスを通る)
    double_multipliers = {k: 2.0 for k in QuantEvaluator.KNOWN_SCORING_KEYS}
    norm_score, norm_verdict = QuantEvaluator.evaluate(
        dossier, config={"scoring_multipliers": double_multipliers}
    )

    # 丸め後のスコアおよび判定が一致すること
    assert abs(norm_score - base_score) <= 0.1
    assert norm_verdict == base_verdict


# =====================================================================
# 2. Pre-Filter 親和的拡張テスト (閾値変更・市場足切り・未知キー警告)
# =====================================================================


def test_pre_filter_target_markets_filtering():
    """target_markets を指定した際に対象外市場が正しく足切りされることを検証"""
    df_candidates, df_liquidity = build_test_dataset()

    # Prime のみ指定
    prime_cfg = {"hard_filters": {"target_markets": ["Prime"]}}
    passed_df, rejected_df = PreFilter.apply_filter(
        df_candidates, df_liquidity, config=prime_cfg
    )

    # 通過銘柄の市場がすべて Prime であること
    passed_markets = passed_df["market"].unique().to_list()
    assert passed_markets == ["Prime"]

    # 除外理由に「対象外市場」が存在すること
    reasons = rejected_df["filter_reason"].to_list()
    assert "対象外市場" in reasons


def test_pre_filter_custom_thresholds():
    """売買代金や株価の閾値変更が正しく機能することを検証"""
    df_candidates, df_liquidity = build_test_dataset()

    # 最低株価を 1,000 円に厳格化
    strict_price_cfg = {"hard_filters": {"min_price": 1000.0}}
    passed_df, rejected_df = PreFilter.apply_filter(
        df_candidates, df_liquidity, config=strict_price_cfg
    )

    for row in passed_df.to_dicts():
        assert row["price"] >= 1000.0

    # 売買代金を 1,000 万円に緩和 (3001 の 1,500万円が通過可能になる)
    relaxed_tv_cfg = {"hard_filters": {"min_trading_value": 10_000_000.0}}
    passed_rel, _ = PreFilter.apply_filter(
        df_candidates, df_liquidity, config=relaxed_tv_cfg
    )
    passed_codes = passed_rel["code"].to_list()
    assert "3001" in passed_codes


def test_pre_filter_unknown_key_tolerance(caplog):
    """hard_filters に未知キーがあってもクラッシュせず警告ログが出力されることを検証"""
    df_candidates, df_liquidity = build_test_dataset()
    typo_cfg = {"hard_filters": {"min_trading_values": 50_000_000.0}}

    with caplog.at_level("WARNING"):
        passed_df, _ = PreFilter.apply_filter(
            df_candidates, df_liquidity, config=typo_cfg
        )

    assert "⚠️ hard_filters に未知の設定キーが含まれています" in caplog.text
    assert not passed_df.is_empty()


# =====================================================================
# 3. 戦略プリセット特性テスト (配点シフトの検証)
# =====================================================================


def test_preset_characteristics():
    """各プリセットが戦略意図通りにスコアを変動させることを検証"""
    # 高配当銘柄
    div_stock = {
        "fundamentals": {
            "per": 15.0,
            "pbr": 1.2,
            "roe": 10.0,
            "equity_ratio": 70.0,
            "dividend_yield": 5.5,  # 高配当
            "operating_margin": 10.0,
        },
        "technicals": {
            "price": 1500.0,
            "rsi_14": 55.0,
            "ma25_divergence": 2.0,
            "macd_hist": 0.2,
            "macd_status": "Bullish",
        },
    }

    score_bal, _ = QuantEvaluator.evaluate(
        div_stock, config={"strategy_preset": "balanced"}
    )
    score_div, _ = QuantEvaluator.evaluate(
        div_stock, config={"strategy_preset": "dividend_focus"}
    )

    # dividend_focus では配当利回りの比重が 2.5倍になるためスコアが上昇する
    assert score_div > score_bal

    # 高成長グロース銘柄
    growth_stock = {
        "fundamentals": {
            "per": 40.0,
            "pbr": 5.0,
            "roe": 30.0,  # 圧倒的ROE
            "equity_ratio": 60.0,
            "dividend_yield": 0.0,
            "operating_margin": 20.0,
        },
        "technicals": {
            "price": 3000.0,
            "rsi_14": 65.0,
            "ma25_divergence": 5.0,
            "macd_hist": 0.5,
            "macd_status": "Bullish",
        },
    }

    score_growth_bal, _ = QuantEvaluator.evaluate(
        growth_stock, config={"strategy_preset": "balanced"}
    )
    score_growth_q, _ = QuantEvaluator.evaluate(
        growth_stock, config={"strategy_preset": "growth_quality"}
    )

    # growth_quality ではROEの比重が 2.5倍になり割安度ペナルティが軽くなるためスコアが上昇する
    assert score_growth_q > score_growth_bal


def test_quant_evaluator_safety_guards(caplog):
    """負の乗数や合計0以下の指定に対して安全ガードが機能することを検証"""
    import logging

    dossier = {
        "fundamentals": {
            "per": 10.0,
            "pbr": 1.0,
            "roe": 10.0,
            "equity_ratio": 50.0,
            "dividend_yield": 2.0,
        },
        "technicals": {
            "price": 1000.0,
            "rsi_14": 55.0,
            "ma25_divergence": 2.0,
            "macd_hist": 0.1,
            "macd_status": "Bullish",
        },
    }

    # 1. 負の乗数
    neg_cfg = {"scoring_multipliers": {"profitability_multiplier": -1.5}}
    with caplog.at_level(logging.WARNING, logger="src.calc.quant_evaluator"):
        s1, _ = QuantEvaluator.evaluate(dossier, config=neg_cfg)
    assert any("負の値" in record.message for record in caplog.records)
    assert s1 >= QuantEvaluator.MIN_SCORE

    # 2. 全乗数0 (ゼロ除算ガード)
    zero_cfg = {
        "scoring_multipliers": {k: 0.0 for k in QuantEvaluator.KNOWN_SCORING_KEYS}
    }
    with caplog.at_level(logging.WARNING, logger="src.calc.quant_evaluator"):
        s2, _ = QuantEvaluator.evaluate(dossier, config=zero_cfg)
    assert any("全乗数の合計配点が0以下" in record.message for record in caplog.records)
    assert s2 >= QuantEvaluator.MIN_SCORE


# =====================================================================
# 4. PathResolver & Stage-and-Sync 単体テスト
# =====================================================================


def test_path_resolver_env_injection(monkeypatch):
    """STOCK_ANALYZER_BASE_DIR によるパス動的解決を検証"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("STOCK_ANALYZER_BASE_DIR", tmp_dir)

        base = PathResolver.get_base_dir()
        assert str(base) == str(Path(tmp_dir).resolve())

        cache_dir = PathResolver.get_cache_dir()
        assert cache_dir == base / "cache"
        assert cache_dir.exists()

        out_dir = PathResolver.get_output_dir()
        assert out_dir == base / "output"
        assert out_dir.exists()

        duck_path = PathResolver.get_duckdb_path()
        assert duck_path == base / "cache" / "stock_analyzer.duckdb"


def test_colab_sync_manager_three_tier_recovery():
    """ColabSyncManager の 3段構え自動復元 (メイン -> .bak -> 新規初期化) を検証"""
    import duckdb

    with (
        tempfile.TemporaryDirectory() as drive_temp,
        tempfile.TemporaryDirectory() as work_temp,
    ):
        drive_dir = Path(drive_temp)
        work_dir = Path(work_temp)

        drive_cache = drive_dir / "cache"
        drive_cache.mkdir(parents=True)

        main_db = drive_cache / "stock_analyzer.duckdb"
        bak_db = drive_cache / "stock_analyzer.duckdb.bak"

        # Case 1: 健全なメイン DB が存在する場合
        conn = duckdb.connect(str(main_db))
        conn.execute("CREATE TABLE test_main (id INT);")
        conn.close()

        res1 = ColabSyncManager.pull_database(drive_dir, work_dir)
        assert res1.exists()
        assert ColabSyncManager.is_duckdb_healthy(res1)

        # Case 2: メイン DB が破損しており、健全な .bak が存在する場合
        # メインを破損ファイル（不正バイト）にする
        with open(main_db, "wb") as f:
            f.write(b"CORRUPTED_DATA_HEADER_INVALID")

        # .bak を健全な DB として作成
        conn_bak = duckdb.connect(str(bak_db))
        conn_bak.execute("CREATE TABLE test_bak (id INT);")
        conn_bak.close()

        res2 = ColabSyncManager.pull_database(drive_dir, work_dir)
        assert res2.exists()
        assert ColabSyncManager.is_duckdb_healthy(res2)

        # Case 3: 両方とも破損している場合 (新規初期化フォールバック)
        with open(bak_db, "wb") as f:
            f.write(b"CORRUPTED_BAK_INVALID")

        # 作業層に健全な DB (Case 2 で復元済み) があれば、消さずに継続利用する
        res3 = ColabSyncManager.pull_database(drive_dir, work_dir)
        assert ColabSyncManager.is_duckdb_healthy(res3)

        # 作業層にも健全な DB が無ければ、新規初期化用のパスが返る
        res3.unlink()
        res4 = ColabSyncManager.pull_database(drive_dir, work_dir)
        assert not res4.exists() or res4.stat().st_size == 0
