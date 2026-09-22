import pytest
import polars as pl
from src.calc.engines.polars_engine import PolarsEngine

@pytest.fixture
def sample_indicators_df():
    """スコアリング計算用の基礎データ"""
    return pl.DataFrame({
        "code": ["1001", "1002", "1003"],
        "sector": ["Tech", "Tech", "Retail"],
        "price": [100.0, 110.0, 50.0],
        "roe": [15.0, 5.0, 20.0],
        "per": [10.0, 30.0, 12.0],
        "rsi_14": [60.0, 40.0, 70.0],
        "macd_hist": [1.0, -1.0, 0.5],
        "market_cap": [1000.0, 5000.0, 100.0],
        "entry_date": ["2026-04-20", "2026-04-20", "2026-04-20"]
    })

@pytest.fixture
def engine_config():
    return {
        "strategies": {
            "Balanced": {
                "base_score": 50.0,
                "points": {
                    "roe": 10.0,
                    "dividend_yield": 5.0
                },
                "thresholds": {
                    "roe": 10.0
                },
                "metrics_metadata": {
                    "roe": {"category": "quality", "direction": "higher"}
                }
            }
        }
    }

def test_polars_engine_calculate_scores_basic(sample_indicators_df, engine_config):
    """正常系: 基礎的なスコアリングとランキングが実行されること"""
    engine = PolarsEngine(engine_config, strategy_name="Balanced")
    result_df = engine.calculate_scores(sample_indicators_df)
    
    assert isinstance(result_df, pl.DataFrame)
    assert not result_df.is_empty()
    assert "quant_score" in result_df.columns
    assert "rank" in result_df.columns
    
    # 1001 は ROE 15 > 10 なので加点され、1002 (ROE 5 < 10) より高くなるはず
    score_1001 = result_df.filter(pl.col("code") == "1001")["quant_score"][0]
    score_1002 = result_df.filter(pl.col("code") == "1002")["quant_score"][0]
    assert score_1001 > score_1002

def test_polars_engine_empty_df(engine_config):
    """異常系: 空の DataFrame 入力時に空の DataFrame を返すこと"""
    engine = PolarsEngine(engine_config)
    result = engine.calculate_scores(pl.DataFrame())
    assert result.is_empty()

def test_polars_engine_sector_ranking(sample_indicators_df, engine_config):
    """正常系: セクター内でのランキングが正しく計算されること"""
    engine = PolarsEngine(engine_config, strategy_name="Balanced")
    result_df = engine.calculate_scores(sample_indicators_df)
    
    # Tech セクターの 1001 と 1002 を比較
    tech_df = result_df.filter(pl.col("sector") == "Tech").sort("rank")
    assert tech_df[0, "code"] == "1001"
    assert tech_df[0, "rank"] == 1
    assert tech_df[1, "rank"] == 2
    
    # Retail セクターは 1003 一つだけなので rank 1 になるはず
    retail_df = result_df.filter(pl.col("sector") == "Retail")
    assert retail_df[0, "rank"] == 1

def test_polars_engine_advanced_scoring(engine_config):
    """正常系: ステータスボーナス、配当性向ペナルティ、型キャストの検証"""
    # 1. ステータスボーナスの追加設定
    config = engine_config.copy()
    config["strategies"]["Balanced"]["base_score"] = 10.0  # 大幅に下げて合計が 100 に達するのを防ぐ
    config["strategies"]["Balanced"]["status_bonuses"] = {
        "is_top_pick": {"1": 10.0, "0": -5.0},
        "delisting_risk": {"true": -50.0}
    }
    config["strategies"]["Balanced"]["thresholds"]["dividend_yield"] = 2.0
    
    # 2. テストデータの作成
    df = pl.DataFrame({
        "code": ["2001", "2002", "2003", "2004"],
        "sector": ["Tech", "Tech", "Tech", "Tech"],
        "is_top_pick": ["1", "0", "1", "0"],
        "delisting_risk": ["false", "false", "true", "false"],
        "roe": [0.0, 0.0, 15.0, 5.0],      # 2001/2002 を 0 にして基本点のみにする
        "dividend_yield": [0.0, 0.0, 3.0, 4.0],
        "payout_ratio": [40.0, 40.0, 40.0, 120.0],
        "rsi_14": [40.0, 40.0, 30.0, 50.001],
        "market_cap": [100.0, 100.0, 100.0, 100.0]
    })
    
    engine = PolarsEngine(config, strategy_name="Balanced")
    result_df = engine.calculate_scores(df)
    
    # 2001: top_pick=1 (+10)
    # 2002: top_pick=0 (-5)
    # 2003: delisting=true (-50)
    # 2004: payout=120 (yield points * 0.5 penalty) & rsi=50.001 (score cap 50)
    
    scores = result_df.select(["code", "quant_score"]).to_dicts()
    score_map = {r["code"]: r["quant_score"] for r in scores}
    
    # 2001 (top_pick=1: +10.0) vs 2002 (top_pick=0: -5.0)
    # 合計スコアが 100 でクリップされるため、差分が 15 未満になる場合があるが、優位性は保持されるべき。
    assert score_map["2001"] > score_map["2002"]
    assert score_map["2003"] < 70.0  # 大幅減点（通常スコアより 50 点以上低い）
    assert score_map["2004"] <= 50.0 # RSIデッドキャップ

def test_polars_engine_filter_and_rank_native(engine_config):
    """正常系: ネイティブフィルタリング機能の検証"""
    engine = PolarsEngine(engine_config, strategy_name="Balanced")
    
    df = pl.DataFrame({
        "code": ["3001", "3002", "3003"],
        "roe": [15.0, 5.0, 12.0],
        "quant_score": [80.0, 70.0, 90.0]
    })
    
    # 3. フィルタリングとランキングの検証
    # Balanced 戦略では roe >= 10.0 が閾値
    # thresholds["roe"] = 10.0 を明示的にセット
    engine.strategy_config["thresholds"] = {"roe": 10.0}
    filtered_df = engine.filter_and_rank_native(df, strategy_name="Balanced")
    
    # 3002 (ROE 5.0) が除外されるはず
    assert filtered_df.height == 2
    assert "3002" not in filtered_df["code"]
    # スコア順に並んでいるはず
    assert filtered_df[0, "code"] == "3003"
    assert filtered_df[1, "code"] == "3001"

def test_polars_engine_filter_candidates():
    """一次足切り（機械的スクリーニング）機能の検証"""
    df = pl.DataFrame({
        "code": ["1001", "1002", "1003", "1004"],
        "price": [1500.0, 30.0, 2000.0, 1000.0],         # 1002 はボロ株 (<50)
        "volume": [5000.0, 10000.0, 500.0, 2000.0],       # 1003 は出来高過疎 (<1000)
        "equity_ratio": [45.0, 20.0, 50.0, 5.0],          # 1004 は債務超過・低自己資本 (<10)
        "quant_score": [80.0, 90.0, 85.0, 75.0]
    })

    candidates = PolarsEngine.filter_candidates(
        df,
        min_price=50.0,
        min_volume=1000.0,
        min_equity_ratio=10.0,
        max_candidates=10
    )

    # 1002, 1003, 1004 は除外され、1001 のみが残る
    assert candidates.height == 1
    assert candidates[0, "code"] == "1001"
