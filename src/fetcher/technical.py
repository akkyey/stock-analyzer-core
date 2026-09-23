from logging import getLogger

import numpy as np
import pandas as pd

logger = getLogger(__name__)


def calc_technical_indicators(hist):
    """テクニカル指標の計算 (MACD, RSI, Moving Averages)"""
    if hist.empty or len(hist) < 30:
        return {}

    try:
        close = hist["Close"]

        # 1. 各指標の計算
        latest_macd_hist, _macd_hist = _calc_macd(close)
        latest_rsi, _rsi = _calc_rsi(close)

        # 2. シグナルの計算
        sig_ma, sig_price = _calc_ma_signals(close)
        sig_macd = 1 if (pd.notna(latest_macd_hist) and latest_macd_hist > 0) else 0
        sig_rsi = 1 if (pd.notna(latest_rsi) and latest_rsi > 50) else 0

        # 3. 高度な指標と Bollinger Bands [v23.1]
        # [v23.1] Polars による高速ベクトル演算を優先
        from src.fetcher.polars_processor import PolarsProcessor

        pl_results = PolarsProcessor.calc_technicals_vectorized(hist)

        adv = _calc_advanced_technicals(hist, close)
        # Polars の結果があれば優先（上書き）
        if pl_results:
            adv.update(
                {
                    "ma_divergence": pl_results.get(
                        "ma_divergence", adv.get("ma_divergence")
                    ),
                }
            )

        real_vol = _calc_volatility(close)

        # Bollinger Bands
        if pl_results and pl_results.get("bb_p1sig") is not None:
            bb_p1, bb_p2, bb_m1, bb_m2 = (
                pl_results["bb_p1sig"],
                pl_results["bb_p2sig"],
                pl_results["bb_m1sig"],
                pl_results["bb_m2sig"],
            )
        else:
            bb_p1, bb_p2, bb_m1, bb_m2 = _calc_bollinger_bands(close)

        # 5. 総合スコアと判定
        trend_score = sig_ma + sig_price + sig_macd + sig_rsi
        trend_up = 1.0 if trend_score >= 3 else 0.0

        return {
            "macd_hist": latest_macd_hist,
            "rsi_14": latest_rsi,
            "trend_up": trend_up,
            "trend_score": trend_score,
            **adv,
            "real_volatility": real_vol,
            "bb_p1sig": bb_p1,
            "bb_p2sig": bb_p2,
            "bb_m1sig": bb_m1,
            "bb_m2sig": bb_m2,
        }
    except Exception as e:
        logger.warning(f"Error calculating technicals: {e}")
        return {}


def _calc_bollinger_bands(
    close: pd.Series, window: int = 25
) -> tuple[float, float, float, float]:
    """ボリンジャーバンドの計算 (±1σ, ±2σ)

    Returns:
        tuple: (p1sig, p2sig, m1sig, m2sig) の最新値
    """
    if len(close) < window:
        return 0.0, 0.0, 0.0, 0.0

    ma = close.rolling(window=window).mean()
    std = close.rolling(window=window).std()

    p1 = ma + std
    p2 = ma + 2 * std
    m1 = ma - std
    m2 = ma - 2 * std

    return (
        float(p1.iloc[-1]),
        float(p2.iloc[-1]),
        float(m1.iloc[-1]),
        float(m2.iloc[-1]),
    )


def _calc_macd(close: pd.Series) -> tuple[float, pd.Series]:
    """MACDの計算"""
    exp12 = close.ewm(span=12, adjust=False).mean()
    exp26 = close.ewm(span=26, adjust=False).mean()
    macd = exp12 - exp26
    signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - signal
    return macd_hist.iloc[-1], macd_hist


def _calc_rsi(close: pd.Series) -> tuple[float, pd.Series]:
    """RSIの計算 (ゼロ除算・変動なし時の50.0保護付)"""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50.0)
    return rsi.iloc[-1], rsi


def _calc_ma_signals(close: pd.Series) -> tuple[int, int]:
    """MAベースのトレンドシグナル計算"""
    ma25_s = close.rolling(window=25).mean()
    ma25 = ma25_s.iloc[-1]
    ma75 = close.rolling(window=75).mean().iloc[-1]
    sig_ma = 1 if (pd.notna(ma25) and pd.notna(ma75) and ma25 > ma75) else 0
    sig_price = 1 if (pd.notna(ma25) and close.iloc[-1] > ma25) else 0
    return sig_ma, sig_price


def _calc_advanced_technicals(hist: pd.DataFrame, close: pd.Series) -> dict:
    """乖離率や出来高比率などの高度な指標"""
    ma25_latest = close.rolling(window=25).mean().iloc[-1]

    ma_divergence = None
    if pd.notna(ma25_latest) and close.iloc[-1] != 0:
        ma_divergence = ((close.iloc[-1] - ma25_latest) / ma25_latest) * 100

    volume_ratio = None
    if "Volume" in hist.columns:
        vol = hist["Volume"]
        avg_vol = vol.rolling(window=25).mean().iloc[-1]
        if pd.notna(avg_vol) and avg_vol > 0:
            volume_ratio = vol.iloc[-1] / avg_vol

    return {"ma_divergence": ma_divergence, "volume_ratio": volume_ratio}


def _calc_volatility(close: pd.Series) -> float | None:
    """ヒストリカル・ボラティリティの計算"""
    if len(close) < 20:
        return None
    log_returns = np.log(close / close.shift(1)).dropna()
    if log_returns.empty:
        return None
    return float(log_returns.std() * np.sqrt(252) * 100)
