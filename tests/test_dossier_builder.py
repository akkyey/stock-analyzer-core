import polars as pl
import pytest

from src.orchestration.dossier_builder import StockDossierBuilder


def test_dossier_builder_single_row():
    """単一レコードからの StockDossier 構築テスト"""
    row = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "Prime",
        "price": 2850.0,
        "rsi_14": 28.5,
        "volume_ratio": 2.2,
        "ma_divergence": -5.5,
        "macd": 10.0,
        "macd_signal": 5.0,
        "trend_signal": 3,
        "market_cap": 350000.0,
        "per": 10.5,
        "pbr": 1.1,
        "roe": 14.2,
        "dividend_yield": 2.8,
        "equity_ratio": 55.4,
        "operating_margin": 10.2,
        "sales": 450000.0,
        "operating_income": 45000.0,
        "net_profit": 35000.0,
        "profit_growth_raw": 15.3,
        "is_turnaround": False,
    }

    dossier = StockDossierBuilder.from_row(row)

    assert dossier["code"] == "7203"
    assert dossier["name"] == "トヨタ自動車"
    assert dossier["fundamentals"]["per"] == 10.5
    assert dossier["technicals"]["rsi_14"] == 28.5
    assert dossier["technicals"]["macd_status"] == "Bullish (Cross Above)"

    triggers = dossier["trigger_reasons"]
    assert any("RSI売られすぎ" in t for t in triggers)
    assert any("出来高急増" in t for t in triggers)
    assert any("高ROE" in t for t in triggers)
    assert any("健全財務" in t for t in triggers)


def test_dossier_builder_dataframe():
    """DataFrame からの複数 StockDossier 一括生成テスト"""
    df = pl.DataFrame(
        {
            "code": ["7203", "9984"],
            "name": ["Toyota", "Softbank"],
            "price": [2800.0, 8000.0],
            "rsi_14": [25.0, 75.0],
            "roe": [15.0, 5.0],
            "volume_ratio": [1.0, 2.5],
        }
    )

    dossiers = StockDossierBuilder.from_dataframe(df, limit=10)
    assert len(dossiers) == 2
    assert dossiers[0]["code"] == "7203"
    assert dossiers[1]["code"] == "9984"


def test_dossier_markdown_summary():
    """Markdown サマリテキスト生成テスト"""
    row = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "Prime",
        "price": 2850.0,
        "rsi_14": 28.5,
        "volume_ratio": 2.2,
        "per": 10.5,
        "roe": 14.2,
    }
    dossier = StockDossierBuilder.from_row(row)
    md = StockDossierBuilder.to_markdown_summary(dossier)
    assert "### 銘柄: トヨタ自動車 (7203)" in md
    assert "RSI(14)=28.5" in md
