"""src/calc/ モジュール群の包括的テスト"""

import pytest
import polars as pl
import pandas as pd

from src.calc.engine import ScoringEngine
from src.calc.engines.polars_engine import PolarsEngine
from src.calc.strategies.generic import GenericStrategy
from src.calc.base import BaseCalculator


# --- PolarsEngine Tests ---

def test_polars_engine_filter_candidates():
    df = pl.DataFrame({
        "code": ["1001", "1002", "1003"],
        "price": [10.0, 100.0, 200.0],
        "volume": [2000, 500, 5000],
        "equity_ratio": [30.0, 40.0, 5.0],
    })

    filtered = PolarsEngine.filter_candidates(
        df, min_price=50.0, min_volume=1000.0, min_equity_ratio=10.0
    )
    assert isinstance(filtered, pl.DataFrame)


def test_polars_engine_calculate_scores():
    df = pl.DataFrame({
        "code": ["7203", "6758"],
        "sector": ["輸送用機器", "電気機器"],
        "price": [2500.0, 12000.0],
        "volume": [10000.0, 5000.0],
        "per": [10.0, 15.0],
        "pbr": [1.0, 1.5],
        "roe": [12.0, 10.0],
        "equity_ratio": [50.0, 60.0],
        "rsi_14": [45.0, 55.0],
        "macd_hist": [0.5, -0.2],
        "ma25_divergence": [1.0, -1.0],
    })

    config = {
        "strategies": {
            "value": {
                "weights": {"quality": 0.5, "trend": 0.5},
                "points": {"per": 10.0, "pbr": 10.0},
            }
        }
    }

    pe = PolarsEngine(config, strategy_name="value")
    scored = pe.calculate_scores(df)
    assert isinstance(scored, pl.DataFrame)


# --- ScoringEngine Tests ---

def test_scoring_engine_basic():
    config = {
        "strategies": {
            "value": {
                "weights": {"quality": 0.5, "trend": 0.5},
            }
        }
    }

    se = ScoringEngine(config)
    strat = se.get_strategy("value")
    assert isinstance(strat, GenericStrategy)


def test_scoring_engine_calculate_score():
    config = {
        "strategies": {
            "value": {
                "weights": {"quality": 0.5, "trend": 0.5},
            }
        }
    }

    se = ScoringEngine(config)
    df_pd = pd.DataFrame({
        "code": ["7203"],
        "price": [2500.0],
        "per": [10.0],
        "pbr": [1.0],
        "rsi_14": [50.0],
    })

    scored = se.calculate_score(df_pd, strategy_name="value")
    assert isinstance(scored, pd.DataFrame)


# --- BaseCalculator Tests ---

def test_base_calculator():
    calc = BaseCalculator(config={})
    assert calc is not None
