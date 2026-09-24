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


def test_aggregate_timeseries_metrics_no_price_double_multiplication():
    """売買代金に株価が二重に掛けられず、薄商い銘柄（50万円/日）が正しく除外されること"""
    # 50万円/日の薄商い銘柄（株価 500円）
    df_ts = pl.DataFrame(
        {
            "code": ["9999"] * 5,
            "entry_date": ["2026-03-27", "2026-03-26", "2026-03-25", "2026-03-24", "2026-03-23"],
            "price": [500.0] * 5,
            "trading_value": [500_000.0] * 5,  # 50万円
        }
    )
    res = PreFilter.aggregate_timeseries_metrics(df_ts)
    # 株価が二重に掛けられて 2.5億円 にならず、50万円（500_000.0）であること
    assert res.filter(pl.col("code") == "9999")["avg_trading_value_20d"][0] == 500_000.0

    df_cand = pl.DataFrame(
        {
            "code": ["9999"],
            "name": ["薄商い株"],
            "price": [500.0],
            "equity_ratio": [60.0],
            "sales": [5000.0],
            "operating_cf": [200.0],
        }
    )
    passed, rejected = PreFilter.apply_filter(df_cand, df_liquidity=res)
    assert passed.is_empty()
    assert len(rejected) == 1
    assert rejected["filter_reason"][0] == "極小流動性トラップ"


def test_pre_filter_no_code_right_column():
    """PreFilter 適用後の適格群および除外群に code_right 列が一切混ざらないこと"""
    df_cand = pl.DataFrame(
        {
            "code": ["1001", "1002"],
            "name": ["A社", "B社"],
            "price": [1000.0, 500.0],
            "equity_ratio": [50.0, 40.0],
            "sales": [2000.0, 1000.0],
            "operating_cf": [100.0, 50.0],
        }
    )
    df_liq = pl.DataFrame(
        {
            "code": ["1001", "1002", "1003"],
            "avg_trading_value_20d": [50_000_000.0, 10_000_000.0, 5_000_000.0],
            "zero_volume_days_5d": [0, 0, 3],
        }
    )
    passed, rejected = PreFilter.apply_filter(df_cand, df_liquidity=df_liq)
    assert "code_right" not in passed.columns
    assert "code_right" not in rejected.columns


def test_pre_filter_missing_fundamentals():
    """自己資本比率等の財務データが欠損している銘柄が『重要指標未開示/算出不能』で隔離されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["1006"],
            "name": ["財務未開示株"],
            "sector": ["情報・通信業"],
            "market": ["Growth"],
            "price": [1200.0],
            "equity_ratio": [None],  # 財務データ未開示
            "sales": [None],
            "operating_cf": [None],
            "avg_trading_value_20d": [50_000_000.0],
            "zero_volume_days_5d": [0],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "重要指標未開示/算出不能"


def test_pre_filter_stale_trade_date():
    """最終取引日が直近5営業日より古い取引停止銘柄が『データ鮮度不足 (取引停止)』で隔離されることを検証"""
    dates_active = ["2026-09-24", "2026-09-23", "2026-09-22", "2026-09-21", "2026-09-20"]
    df_ts = pl.DataFrame(
        {
            "code": ["1001"] * 5 + ["1002"],
            "entry_date": dates_active + ["2026-08-24"],  # 1002は1ヶ月前
            "trading_value": [50_000_000.0] * 6,
            "price": [1000.0] * 5 + [500.0],
        }
    )
    res = PreFilter.aggregate_timeseries_metrics(df_ts)

    df_cand = pl.DataFrame(
        {
            "code": ["1001", "1002"],
            "name": ["稼働株", "停止株"],
            "price": [1000.0, 500.0],
            "equity_ratio": [50.0, 50.0],
            "sales": [2000.0, 1000.0],
            "operating_cf": [100.0, 50.0],
        }
    )
    passed, rejected = PreFilter.apply_filter(df_cand, df_liquidity=res)
    assert len(passed) == 1
    assert passed["code"][0] == "1001"
    rej_1002 = rejected.filter(pl.col("code") == "1002")
    assert len(rej_1002) == 1
    assert rej_1002["filter_reason"][0] == "データ鮮度不足 (取引停止)"


def test_pre_filter_missing_market_price():
    """株価データが存在しない銘柄が『市場データ取得不能』として隔離されることを検証"""
    df = pl.DataFrame(
        {
            "code": ["3593"],
            "name": ["ホギメディカル"],
            "sector": ["繊維製品"],
            "market": ["Prime"],
            "price": [None],  # 株価欠損
            "equity_ratio": [80.0],
            "sales": [30000.0],
            "operating_cf": [4000.0],
            "avg_trading_value_20d": [None],
            "zero_volume_days_5d": [None],
        }
    )
    result = PreFilter.evaluate(df)
    assert result.passed_df.is_empty()
    assert len(result.rejected_df) == 1
    assert result.rejected_df["filter_reason"][0] == "市場データ取得不能"
    assert "市場価格データ欠損" in result.rejected_df["filter_detail"][0]



