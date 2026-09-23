"""src/fetcher/ モジュール群の包括的テスト"""

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.fetcher.edinet_fetcher import EdinetFetcher
from src.fetcher.jpx import JPXFetcher
from src.fetcher.market_fetcher import MarketFetcher
from src.fetcher.polars_processor import PolarsProcessor
from src.fetcher.technical import (
    _calc_advanced_technicals,
    _calc_bollinger_bands,
    _calc_ma_signals,
    _calc_macd,
    _calc_rsi,
    _calc_volatility,
    calc_technical_indicators,
)

# --- technical.py Tests ---


def test_calc_technical_indicators_empty():
    res = calc_technical_indicators(pd.DataFrame())
    assert res == {}

    short_df = pd.DataFrame({"Close": [100.0] * 10})
    res_short = calc_technical_indicators(short_df)
    assert res_short == {}


def test_calc_technical_indicators_valid():
    dates = pd.date_range("2026-01-01", periods=50)
    prices = [100.0 + i * 0.5 for i in range(50)]
    volumes = [1000 + i * 10 for i in range(50)]
    hist = pd.DataFrame(
        {
            "code": ["7203"] * 50,
            "Date": dates,
            "Close": prices,
            "Volume": volumes,
        }
    )

    res = calc_technical_indicators(hist)
    assert "macd_hist" in res
    assert "rsi_14" in res
    assert "trend_up" in res
    assert "bb_p1sig" in res


def test_technical_sub_functions():
    prices = pd.Series([100.0 + i for i in range(40)])

    # BB
    p1, p2, m1, m2 = _calc_bollinger_bands(prices)
    assert p1 > m1

    # MACD
    m_hist_val, m_hist_series = _calc_macd(prices)
    assert isinstance(m_hist_val, float)

    # RSI
    rsi_val, rsi_series = _calc_rsi(prices)
    assert isinstance(rsi_val, float)

    # MA Signals
    sig_ma, sig_price = _calc_ma_signals(prices)
    assert sig_ma in (0, 1)

    # Volatility
    vol = _calc_volatility(prices)
    assert isinstance(vol, float)


# --- polars_processor.py Tests ---


def test_polars_processor_vectorized():
    df_in = pd.DataFrame(
        {
            "code": ["7203"] * 35,
            "Date": pd.date_range("2026-01-01", periods=35),
            "price": [100.0 + i for i in range(35)],
            "Volume": [1000] * 35,
        }
    )

    res = PolarsProcessor.calc_technicals_vectorized(df_in)
    assert isinstance(res, dict)

    hist_map = {"7203": df_in}
    res_batch = PolarsProcessor.calc_batch_technicals_vectorized(hist_map)
    assert not res_batch.is_empty()


# --- jpx.py Tests ---


@patch("requests.get")
def test_jpx_fetcher(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"fake excel content"
    mock_get.return_value = mock_resp

    jpx = JPXFetcher()
    assert jpx is not None


# --- edinet_fetcher.py Tests ---


def test_edinet_fetcher_basic(tmp_path):
    ef = EdinetFetcher(
        config={"paths": {"edinet_code_csv": str(tmp_path / "dummy.csv")}}
    )
    assert ef is not None


# --- market_fetcher.py Tests ---


def test_market_fetcher_basic():
    mf = MarketFetcher()
    assert mf is not None
