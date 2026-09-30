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


def test_mask_api_key_and_authentication_error(monkeypatch):
    from src.fetcher.edinet_fetcher import EdinetAuthenticationError, mask_api_key

    # 1. mask_api_key の URL / ヘッダー内キー伏字化検証
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-06-25&type=2&Subscription-Key=abc123def456xyz"
    masked = mask_api_key(url)
    assert "abc123def456xyz" not in masked
    assert "Subscription-Key=***" in masked

    header_text = "{'Ocp-Apim-Subscription-Key': 'secretkey999'}"
    masked_header = mask_api_key(header_text)
    assert "secretkey999" not in masked_header
    assert "***" in masked_header

    # 2. 401/403 時に EdinetAuthenticationError が送出されること
    monkeypatch.setenv("EDINET_API_KEY", "invalid_test_key")
    ef = EdinetFetcher(config={})
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"metadata": {"status": "401", "message": "Unauthorized"}}'
        mock_resp.json.return_value = {"metadata": {"status": "401", "message": "Unauthorized"}}
        mock_get.return_value = mock_resp

        with pytest.raises(EdinetAuthenticationError, match="認証に失敗しました"):
            ef.fetch_documents_by_date("2026-06-25")

    # 3. EDINET API v2 の実応答: HTTP 200 かつ top-level StatusCode: 401 で EdinetAuthenticationError が送出されること
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"StatusCode": 401, "message": "Access denied due to invalid subscription key. Make sure to provide a valid key for an active subscription."}'
        mock_resp.json.return_value = {
            "StatusCode": 401,
            "message": "Access denied due to invalid subscription key. Make sure to provide a valid key for an active subscription."
        }
        mock_get.return_value = mock_resp

        with pytest.raises(EdinetAuthenticationError, match="認証に失敗しました"):
            ef.fetch_documents_by_date("2026-06-25")

    # 4. download_xbrl における HTTP 200 エラー JSON レスポンスの検知
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {
            "StatusCode": 401,
            "message": "Access denied due to invalid subscription key."
        }
        mock_get.return_value = mock_resp

        with pytest.raises(EdinetAuthenticationError, match="認証に失敗しました"):
            ef.download_xbrl("S100TEST", "/tmp/dummy_save_dir")


def test_edinet_fetcher_transient_retry_and_recovery(monkeypatch):
    """429 や 503 等の一時的エラーに対してリトライを実行し、回復時は正常にデータを取得できること"""
    ef = EdinetFetcher(config={})
    monkeypatch.setattr("time.sleep", lambda s: None)  # テスト高速化のため sleep をスキップ

    with patch("requests.get") as mock_get:
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = '{"message": "Rate limit exceeded"}'
        resp_429.json.return_value = {"message": "Rate limit exceeded"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.text = '{"results": [{"docID": "S100TEST"}]}'
        resp_200.json.return_value = {"results": [{"docID": "S100TEST"}]}

        mock_get.side_effect = [resp_429, resp_200]

        res = ef.fetch_documents_by_date("2026-06-25")
        assert len(res.get("results", [])) == 1
        assert mock_get.call_count == 2


def test_download_xbrl_cleanup_on_failure(tmp_path, monkeypatch):
    """ダウンロード中または認証検証で失敗した場合、破損/不完全な zip ファイルがディスクに残らないこと"""
    ef = EdinetFetcher(config={})
    save_dir = str(tmp_path / "xbrl_downloads")

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/octet-stream"}
        # 不完全なチャンクを返し、途中で例外を発生させる
        mock_resp.iter_content.side_effect = RuntimeError("Network dropped during stream")
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Failed to download XBRL"):
            ef.download_xbrl("S100FAIL", save_dir)

        # ファイルが残留していないことを確認
        assert not (tmp_path / "xbrl_downloads" / "S100FAIL.zip").exists()


def test_xbrl_parser_corrupted_zip(tmp_path):
    """破損した ZIP ファイル（BadZipFile）を渡してもクラッシュせず空辞書を安全に返すこと"""
    from src.fetcher.xbrl_parser import XbrlParser

    parser = XbrlParser()
    bad_zip = tmp_path / "corrupted.zip"
    bad_zip.write_bytes(b"This is definitely not a zip file content.")

    res = parser.parse_zip(str(bad_zip))
    assert res == {}




# --- market_fetcher.py Tests ---


def test_market_fetcher_basic():
    mf = MarketFetcher()
    assert mf is not None
