"""src/validation_engine.py, src/reporter.py の包括的テスト"""

import os
import time
from pathlib import Path

import pandas as pd
import pytest

from src.reporter import StockReporter
from src.validation_engine import ValidationEngine

# --- ValidationEngine Tests ---


def test_validation_engine_basic():
    ve = ValidationEngine(debug_mode=True)

    policy_bank = ve.get_policy("銀行業")
    assert isinstance(policy_bank, dict)

    excludes = ve.get_ai_excludes("銀行業")
    assert isinstance(excludes, list)

    exemptions = ve.get_score_exemptions("銀行業")
    assert isinstance(exemptions, list)


def test_validation_engine_validate_polars():
    import polars as pl

    ve = ValidationEngine(debug_mode=True)

    df_pl = pl.DataFrame(
        {
            "code": ["7203", "8306"],
            "sector": ["輸送用機器", "銀行業"],
            "price": [2500.0, 1000.0],
            "per": [10.0, None],
            "pbr": [1.0, 0.8],
            "operating_cf": [100.0, 50.0],
            "sales_growth": [0.05, 0.02],
            "operating_margin": [0.10, 0.15],
            "equity_ratio": [0.50, 0.08],
        }
    )

    res = ve.validate_batch_polars(df_pl, strategy="value")
    assert res is not None


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
