import pandas as pd
import polars as pl
import pytest

from src.fetcher.polars_processor import PolarsProcessor
from tests.helpers.stubs import DataGenerator


def test_polars_processor_batch_technicals():
    """正常系: 複数銘柄のベクトル演算が正しく動作することを確認"""
    # 120日分のデータを準備 (ma75, rsi14 に十分な期間)
    hist_map = {
        "1001": DataGenerator.generate_history("1001", 120),
        "1002": DataGenerator.generate_history("1002", 120),
    }

    # 実行
    result_df = PolarsProcessor.calc_batch_technicals_vectorized(
        hist_map, latest_only=True
    )

    # 検証
    assert isinstance(result_df, pl.DataFrame)
    assert len(result_df) == 2
    assert "code" in result_df.columns
    assert "price" in result_df.columns  # rename Close -> price
    assert "ma25" in result_df.columns
    assert "ma75" in result_df.columns
    assert "rsi_14" in result_df.columns
    assert "macd_hist" in result_df.columns

    # 指標が NULL でないことを確認 (120日あれば算出可能なはず)
    row_1001 = result_df.filter(pl.col("code") == "1001")
    assert row_1001["ma75"][0] is not None
    assert row_1001["rsi_14"][0] is not None


def test_polars_processor_empty_input():
    """準正常系: 空入力時に空の DataFrame を返すこと"""
    result = PolarsProcessor.calc_batch_technicals_vectorized({})
    assert result.is_empty()


def test_polars_processor_invalid_data():
    """異常系: 無効なデータが含まれる場合もクラッシュせず処理を続けること"""
    hist_map = {
        "1001": pd.DataFrame(),  # 空
        "1002": DataGenerator.generate_history("1002", 120),
    }
    result = PolarsProcessor.calc_batch_technicals_vectorized(hist_map)
    assert len(result) == 1
    assert result[0, "code"] == "1002"


def test_polars_processor_missing_code_column():
    """異常系: 'code' カラムが欠落している場合に ValueError を投げること"""
    # 直接 pl.DataFrame を作成することで、変換エラーを回避してカラムチェックに到達させる
    df_no_code = pl.DataFrame(
        {
            "Close": [100.0, 110.0],
            "Date": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
        }
    )
    hist_map = {"1001": df_no_code}
    with pytest.raises(ValueError, match="Column 'code' is missing"):
        PolarsProcessor.calc_batch_technicals_vectorized(hist_map)


def test_polars_processor_price_mapping():
    """正常系: 'Close' がなく 'price' がある場合に自動でマッピングされること"""
    df_price = pd.DataFrame(
        {
            "code": ["3001", "3001"],
            "price": [100.0, 105.0],
            "Date": pd.to_datetime(["2026-04-01", "2026-04-02"]),
        }
    )
    hist_map = {"3001": df_price}
    result = PolarsProcessor.calc_batch_technicals_vectorized(hist_map)
    assert "price" in result.columns
    # 内部的に指標計算に使用されているはず


def test_polars_processor_deprecated_wrapper():
    """正常系: 非推奨の単一銘柄ラッパーが正しく動作すること"""
    df = DataGenerator.generate_history("4001", 120)
    # 非推奨ラッパーは dict を返す仕様。コードは内部で "_legacy_wrapper" になる
    result = PolarsProcessor.calc_technicals_vectorized(df)
    assert isinstance(result, dict)
    assert result["code"] == "_legacy_wrapper"


def test_polars_processor_datetime_fallback():
    """正常系: 日付形式が特殊な場合もフォールバックしてパースされること"""
    df = pl.DataFrame(
        {
            "code": ["5001", "5001"],
            "Close": [100.0, 101.0],
            "Date": ["1900-01-01", "1900-01-02"],
        }
    )  # String のまま渡して内部で変換させる

    hist_map = {"5001": df}
    result = PolarsProcessor.calc_batch_technicals_vectorized(hist_map)
    assert not result.is_empty()
    # entry_date (元Date/index) が時系列型あるいは適切にパースされていること
    assert "entry_date" in result.columns


def test_polars_processor_batch_exception_raises(monkeypatch):
    """異常系: calc_from_polars で例外が発生した際、握りつぶして空DFを返さず例外を再送出すること (指摘8)"""
    def _mock_calc_failure(*args, **kwargs):
        raise RuntimeError("Fatal calculation failure inside vectorized engine")

    monkeypatch.setattr(PolarsProcessor, "calc_from_polars", _mock_calc_failure)

    hist_map = {
        "1001": DataGenerator.generate_history("1001", 10),
    }
    with pytest.raises(RuntimeError, match="Fatal calculation failure inside vectorized engine"):
        PolarsProcessor.calc_batch_technicals_vectorized(hist_map)

