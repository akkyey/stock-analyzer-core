"""src/reporter.py の包括的テスト"""

import os
from pathlib import Path

import pandas as pd
import pytest

from src.reporter import StockReporter

# --- StockReporter Tests ---


def test_stock_reporter_generate_reports(tmp_path):
    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))

    row_data = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "プライム",
        "close": 2500.0,
        "per": 10.5,
        "pbr": 1.1,
        "rsi_14": 45.0,
        "quant_score": 85.0,
        "strategy_name": "value",
    }

    results = [
        {
            "latest": row_data,
            "data": row_data,
        }
    ]

    paths = reporter.generate_reports(results, output_context="daily")
    assert isinstance(paths, dict)

    summary_csv = paths["summary"]
    assert summary_csv.name == "daily_report_test.csv"
    assert summary_csv.exists()

    df_sum = pd.read_csv(summary_csv, comment="#")
    assert "PER_Src" in df_sum.columns
    assert "PER" in df_sum.columns
    assert "AI_Rating" not in df_sum.columns  # AI評価列が除去されていること
    assert df_sum.iloc[0]["PER_Src"] == "yf"
    assert df_sum.iloc[0]["PER"] == 10.5


def test_stock_reporter_fallback_metrics(tmp_path):
    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))

    row_data = {
        "code": "6758",
        "name": "ソニーグループ",
        "sector": "電気機器",
        "market": "プライム",
        "close": 10000.0,
        "eps": 500.0,
        "bps": 5000.0,
        "dps": 100.0,
        "shares_outstanding": 1000000,
        "quant_score": 90.0,
        "strategy_name": "growth",
    }

    results = [{"latest": row_data, "data": row_data}]
    paths = reporter.generate_reports(results, output_context="daily")

    df_sum = pd.read_csv(paths["summary"], comment="#")
    row = df_sum.iloc[0]

    assert row["PER_Src"] == "calc"
    assert row["PER"] == 20.0
    assert row["PBR_Src"] == "calc"
    assert row["PBR"] == 2.0
    assert row["Div_Yield_Src"] == "calc"
    assert row["Div_Yield"] == 1.0
    assert row["ROE_Src"] == "calc"
    assert row["ROE"] == 10.0
    assert row["Market_Cap_Src"] == "calc"
    assert row["Market_Cap"] == 10000000000


def test_stock_reporter_backup_rotation(tmp_path):
    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))

    row_data = {
        "code": "7203",
        "name": "トヨタ",
        "quant_score": 80.0,
    }
    results = [{"latest": row_data, "data": row_data}]

    paths1 = reporter.generate_reports(results, output_context="daily")
    assert paths1["summary"].exists()

    paths2 = reporter.generate_reports(results, output_context="daily")
    assert paths2["summary"].exists()

    files = list(tmp_path.glob("daily_report_test*.csv"))
    assert len(files) >= 2


def test_stock_reporter_empty(tmp_path):
    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))
    paths = reporter.generate_reports([], output_context="empty_test")
    assert isinstance(paths, dict)


def _metrics(tmp_path, row: dict) -> dict:
    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))
    return reporter._resolve_metrics_with_fallback(row, row)


def test_percent_values_are_not_rescaled(tmp_path):
    """保存値は既にパーセント表記。1% 未満の配当利回り・ROE を 100 倍にしない"""
    m = _metrics(
        tmp_path,
        {"close": 5000.0, "dividend_yield": 0.77, "roe": 0.5, "per": 10.0, "pbr": 1.0},
    )
    assert m["div_yield"] == (0.77, "yf")
    assert m["roe"] == (0.5, "yf")

    # 負の ROE や 1 以上の値も変えない
    m = _metrics(tmp_path, {"close": 100.0, "dividend_yield": 3.4, "roe": -0.4})
    assert m["div_yield"] == (3.4, "yf")
    assert m["roe"] == (-0.4, "yf")


def test_missing_data_is_hyphen_not_placeholder(tmp_path):
    """データが無い指標は仮の値 (配当 2.25% / 業種別 PER・PBR / 推定株数 / ROE 8.5%) ではなく '-'"""
    m = _metrics(
        tmp_path, {"close": 1000.0, "sector": "情報・通信業", "operating_margin": 12.0}
    )
    for key in ("per", "pbr", "div_yield", "market_cap", "roe"):
        assert m[key] == ("-", "-"), key


def test_real_calculations_are_still_used(tmp_path):
    """元データから計算できる指標は従来どおり calc で出力する (デグレード防止)"""
    m = _metrics(
        tmp_path,
        {"close": 10000.0, "eps": 500.0, "bps": 5000.0, "dps": 100.0, "shares_outstanding": 1_000_000},
    )
    assert m["per"] == (20.0, "calc")
    assert m["pbr"] == (2.0, "calc")
    assert m["div_yield"] == (1.0, "calc")
    assert m["roe"] == (10.0, "calc")
    assert m["market_cap"] == (10_000_000_000, "calc")

    # DPS=0 (無配) は仮の値ではなく実際の 0.0%
    m = _metrics(tmp_path, {"close": 1000.0, "dps": 0.0})
    assert m["div_yield"] == (0.0, "calc")


def test_hyphen_values_survive_csv_roundtrip(tmp_path):
    """'-' を含むレポートを、Step 4 と同じ読み込み方 (null_values) で問題なく読める"""
    import polars as pl

    os.environ["STOCK_ENV"] = "test"
    reporter = StockReporter(output_dir=str(tmp_path))
    row = {"code": "1234", "name": "テスト", "sector": "卸売業", "market": "Prime",
           "close": 1000.0, "quant_score": 50.0, "verdict": "Grade C"}
    paths = reporter.generate_reports([{"latest": row, "data": row}], output_context="daily")
    df = pl.read_csv(paths["summary"], null_values=["-", "None", "null", ""])
    assert str(df["Code"][0]) == "1234"
    assert df["Verdict"][0] == "Grade C"
    assert df["Div_Yield"].null_count() == 1
    assert df["Div_Yield_Src"][0] is None  # "-" は欠損として読まれる
