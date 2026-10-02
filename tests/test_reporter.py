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
    # close/eps で再計算できない保存値は stored (取得元は断定しない)
    assert df_sum.iloc[0]["PER_Src"] == "stored"
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
    assert m["div_yield"][0] == 0.77
    assert m["roe"] == (0.5, "stored")

    # 負の ROE や 1 以上の値も変えない
    m = _metrics(tmp_path, {"close": 100.0, "dividend_yield": 3.4, "roe": -0.4})
    assert m["div_yield"][0] == 3.4
    assert m["roe"] == (-0.4, "stored")


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


def test_source_labels_reflect_actual_origin(tmp_path):
    """出所: 株価と財務データから再計算できる保存値は calc、そうでなければ stored (取得元は断定しない)"""
    row = {
        "close": 2000.0,
        "eps": 100.0, "per": 20.0,          # close/eps と一致 → calc
        "bps": 1000.0, "pbr": 2.0,          # close/bps と一致 → calc
        "dps": 40.0, "dividend_yield": 2.0, # dps/close*100 と一致 → calc
        "shares_outstanding": 1_000_000.0, "market_cap": 2_000_000_000.0,
        "roe": 10.0,                        # 再計算の手段が無い保存値 → stored
    }
    m = _metrics(tmp_path, row)
    assert m["per"] == (20.0, "calc")
    assert m["pbr"] == (2.0, "calc")
    assert m["div_yield"] == (2.0, "calc")
    assert m["market_cap"] == (2_000_000_000, "calc")
    assert m["roe"] == (10.0, "stored")

    # 保存値が再計算と食い違う (過去に保存された値のまま使われている) 場合は stored
    row2 = dict(row, per=33.3, dividend_yield=9.9)
    m2 = _metrics(tmp_path, row2)
    assert m2["per"][1] == "stored"
    assert m2["div_yield"][1] == "stored"

    # 取得元を証明できない "yf" / "edinet" は出力しない
    assert all(src in ("calc", "stored", "-") for _, src in m.values())
    assert all(src in ("calc", "stored", "-") for _, src in m2.values())

    # 再計算に必要な入力 (EPS 等) が無い場合は、値があっても calc とは言わない (実データの大半)
    row3 = {"close": 2000.0, "per": 20.0, "pbr": 2.0, "dividend_yield": 2.0, "market_cap": 2e9}
    m3 = _metrics(tmp_path, row3)
    assert {k: v[1] for k, v in m3.items() if k != "roe"} == {
        "per": "stored", "pbr": "stored", "div_yield": "stored", "market_cap": "stored",
    }
