"""PreFilter 単体テストモジュール

第1層（Pre-Filter: 事前足切り）の各除外ルールおよび免除ルールを検証する。
"""

import polars as pl

from src.calc.pre_filter import PreFilter


def test_pre_filter_zero_volume():
    """直近5営業日に出来高ゼロ日がある銘柄が『商い不成立』で除外されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1001"],
            "name": ["商いゼロ株"],
            "sector": ["情報・通信業"],
            "market": ["Standard"],
            "price": [500.0],
            "equity_ratio": [50.0],
            "sales": [1000.0],
            "operating_cf": [100.0],
            "avg_trading_value_20d": [50_000_000.0],
            "zero_volume_days_5d": [1],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "商い不成立"


def test_pre_filter_low_liquidity():
    """20日平均売買代金が3,000万円未満の銘柄が『極小流動性トラップ』で除外されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1002"],
            "name": ["低流動性株"],
            "sector": ["卸売業"],
            "market": ["Standard"],
            "price": [300.0],
            "equity_ratio": [40.0],
            "sales": [1000.0],
            "operating_cf": [100.0],
            "avg_trading_value_20d": [25_000_000.0],  # 2,500万円 < 3,000万円
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "極小流動性トラップ"


def test_pre_filter_penny_stock():
    """株価が50円未満の銘柄が『超低位ボロ株』で除外されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1003"],
            "name": ["ボロ株"],
            "sector": ["サービス業"],
            "market": ["Standard"],
            "price": [48.0],  # 48円 < 50円
            "equity_ratio": [30.0],
            "sales": [1000.0],
            "operating_cf": [100.0],
            "avg_trading_value_20d": [50_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "超低位ボロ株"


def test_pre_filter_insolvency():
    """自己資本比率 <= 0% の銘柄が『構造的破綻 (債務超過)』で除外されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1004"],
            "name": ["債務超過株"],
            "sector": ["製造業"],
            "market": ["Standard"],
            "price": [200.0],
            "equity_ratio": [-2.5],  # 債務超過
            "sales": [1000.0],
            "operating_cf": [100.0],
            "avg_trading_value_20d": [50_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "構造的破綻 (債務超過)"


def test_pre_filter_cash_drain():
    """営業CFマージン < -10% の銘柄が『致命的キャッシュ枯渇』で除外されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1005"],
            "name": ["キャッシュ枯渇株"],
            "sector": ["小売業"],
            "market": ["Prime"],
            "price": [1500.0],
            "equity_ratio": [45.0],
            "sales": [10_000.0],
            "operating_cf": [-1_500.0],  # 営業CFマージン = -15% < -10%
            "avg_trading_value_20d": [80_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "致命的キャッシュ枯渇"


def test_pre_filter_bank_exemption():
    """銀行業などの金融セクターは営業CFマージンによる除外が免除され、通過することを検証"""
    df = pl.DataFrame(
        {
            "code": ["8306"],
            "name": ["大手メガバンク"],
            "sector": ["銀行業"],
            "market": ["Prime"],
            "price": [1800.0],
            "equity_ratio": [5.2],
            "sales": [50_000.0],
            "operating_cf": [-10_000.0],  # 営業CFマージン赤字だが金融業のため免除
            "avg_trading_value_20d": [500_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert len(result.passed_df) == 1
    assert result.rejected_df.is_empty()


def test_pre_filter_passed_stock():
    """すべての条件をクリアした優良株が正常に通過することを検証"""
    df = pl.DataFrame(
        {
            "code": ["2763"],
            "name": ["エフティグループ"],
            "sector": ["卸売業"],
            "market": ["Standard"],
            "price": [1270.0],
            "equity_ratio": [77.8],
            "sales": [40_000.0],
            "operating_cf": [5_000.0],
            "avg_trading_value_20d": [65_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert len(result.passed_df) == 1
    assert result.rejected_df.is_empty()


def test_aggregate_timeseries_metrics():
    """時系列データからの20日平均売買代金および5日以内ゼロ日数の集計を検証"""
    df_ts = pl.DataFrame(
        {
            "code": ["1001", "1001", "1001", "1002", "1002"],
            "entry_date": [
                "2026-03-27",
                "2026-03-26",
                "2026-03-25",
                "2026-03-27",
                "2026-03-26",
            ],
            "trading_value": [
                50_000_000.0,
                0.0,
                40_000_000.0,
                10_000_000.0,
                20_000_000.0,
            ],
        }
    )
    res = PreFilter.aggregate_timeseries_metrics(df_ts)
    row_1001 = res.filter(pl.col("code") == "1001")
    assert row_1001["zero_volume_days_5d"][0] == 1
    assert row_1001["avg_trading_value_20d"][0] == 30_000_000.0

    row_1002 = res.filter(pl.col("code") == "1002")
    assert row_1002["zero_volume_days_5d"][0] == 0
    assert row_1002["avg_trading_value_20d"][0] == 15_000_000.0
