"""RSI・MACD の計算ロジック正当性検証および異常値チェックアウトの単体テスト"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from src.fetcher.polars_processor import PolarsProcessor
from src.fetcher.technical import (
    _calc_macd,
    _calc_rsi,
    calc_technical_indicators,
)

# --- 1. ゴールデンデータ（決定論的パターン）テスト ---


def test_rsi_golden_patterns():
    """RSI の理論値（単調増加・単調減少・平坦）テスト"""
    # Case A: 完全平坦 (株価変動なし -> RSI = 50.0)
    flat_prices = pd.Series([100.0] * 30)
    rsi_val_flat, _ = _calc_rsi(flat_prices)
    assert abs(rsi_val_flat - 50.0) < 1e-3

    # Case B: 単調増加 (全日上昇 -> RSI 100.0 に収束)
    increasing_prices = pd.Series([100.0 + i for i in range(30)])
    rsi_val_inc, _ = _calc_rsi(increasing_prices)
    assert abs(rsi_val_inc - 100.0) < 1e-3

    # Case C: 単調減少 (全日下落 -> RSI 0.0 に収束)
    decreasing_prices = pd.Series([200.0 - i for i in range(30)])
    rsi_val_dec, _ = _calc_rsi(decreasing_prices)
    assert abs(rsi_val_dec - 0.0) < 1e-3


def test_macd_golden_patterns():
    """MACD の理論値（平坦時 zero）テスト"""
    flat_prices = pd.Series([100.0] * 35)
    macd_hist_flat, _ = _calc_macd(flat_prices)
    assert abs(macd_hist_flat - 0.0) < 1e-3


# --- 2. Polars vs Pandas の同値性（Parity）検証 ---


def test_polars_vs_pandas_parity():
    """Pandas版とPolarsベクター化版の算出結果が一致することを検証"""
    dates = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(40)]
    np.random.seed(42)
    prices = [float(x) for x in (100.0 + np.cumsum(np.random.randn(40)))]

    df_pd = pd.DataFrame(
        {
            "code": ["9999"] * 40,
            "Date": dates,
            "Close": prices,
            "Volume": [1000] * 40,
        }
    )

    # Pandas 版
    pd_res = calc_technical_indicators(df_pd)

    # Polars 版
    pl_res = PolarsProcessor.calc_technicals_vectorized(df_pd)

    # RSI & MACD の一致性チェック (許容範囲内)
    assert pd_res.get("rsi_14") is not None
    assert pl_res.get("rsi_14") is not None
    assert abs(pd_res["rsi_14"] - pl_res["rsi_14"]) < 5.0

    assert pd_res.get("macd_hist") is not None
    assert pl_res.get("macd_hist") is not None
    assert abs(pd_res["macd_hist"] - pl_res["macd_hist"]) < 2.0


# --- 3. 異常値チェックアウト機能のテスト ---


def test_anomaly_data_checkout():
    """異常な株価スパイク・マイナス価格の自動検出・チェックアウトテスト"""
    df_anomaly = pd.DataFrame(
        {
            "code": ["8888"] * 35,
            "Date": [datetime(2026, 1, 1) + timedelta(days=i) for i in range(35)],
            "price": [100.0] * 15 + [-50.0] + [10000.0] + [100.0] * 18,
            "Volume": [1000] * 35,
        }
    )

    # PolarsProcessor のチェックアウトフィルタ適用
    cleaned_df = PolarsProcessor.clean_anomalous_prices(df_anomaly)

    # 負の価格やスパイクが排除または無効化されていること
    prices = cleaned_df["price"].to_list()
    assert any(p is None for p in prices)
