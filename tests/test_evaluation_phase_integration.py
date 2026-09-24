"""PolarsProcessor と EvaluationPhase の結合検証テスト (指摘1, 2)

実際の PolarsProcessor 出力を EvaluationPhase に通し、
1. 乖離率配点 (ma_divergence -> ma25_divergence) が 0点にならず正しくスコアに反映されること
2. macd_status ("Bullish"/"Bearish") が自動判定されゲートキーパー3が機能すること
3. 全履歴時系列データからの df_liquidity 集計により、直近5営業日出来高ゼロ株と20日平均売買代金不足株が正しく足切りされること
を包括的に検証する。
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest

from src.fetcher.polars_processor import PolarsProcessor
from src.orchestration.phases.evaluation import EvaluationPhase
from tests.helpers.stubs import StubOrchestratorContext


def test_evaluation_phase_with_real_polars_processor():
    """PolarsProcessor の実出力を通して、乖離率・MACD・流動性足切りが正しく連動することを検証する。"""
    # 1. テストデータの生成 (25営業日分の時系列データ)
    # 銘柄 A (8306): 健全・高流動性、価格上昇トレンド
    # 銘柄 B (9999): 深い下降トレンド (MA乖離率 < -10%、MACD Bearish)
    # 銘柄 C (8165): 直近5営業日出来高ゼロ (売買不能)
    base_date = date(2026, 9, 1)

    records_a = []
    records_b = []
    records_c = []

    for i in range(35):
        d = base_date + timedelta(days=i)
        if d.weekday() >= 5:  # 土日除外
            continue

        # 銘柄 A: 2000円 -> 2500円 上昇、出来高十分
        records_a.append(
            {
                "code": "8306",
                "entry_date": d,
                "price": 2000.0 + i * 15.0,
                "open": 2000.0 + i * 15.0,
                "high": 2010.0 + i * 15.0,
                "low": 1990.0 + i * 15.0,
                "volume": 100000,
                "trading_value": 250000000.0,
            }
        )

        # 銘柄 B: 2000円 -> 1200円 下降、出来高十分
        records_b.append(
            {
                "code": "9999",
                "entry_date": d,
                "price": max(1200.0, 2000.0 - i * 25.0),
                "open": max(1200.0, 2000.0 - i * 25.0),
                "high": max(1210.0, 2010.0 - i * 25.0),
                "low": max(1190.0, 1990.0 - i * 25.0),
                "volume": 100000,
                "trading_value": 150000000.0,
            }
        )

        # 銘柄 C: 直近5営業日出来高ゼロ
        vol_c = 0 if i >= 20 else 5000
        records_c.append(
            {
                "code": "8165",
                "entry_date": d,
                "price": 500.0,
                "open": 500.0,
                "high": 505.0,
                "low": 495.0,
                "volume": vol_c,
                "trading_value": float(vol_c * 500),
            }
        )

    hist_map = {
        "8306": pl.DataFrame(records_a),
        "9999": pl.DataFrame(records_b),
        "8165": pl.DataFrame(records_c),
    }

    # 2. PolarsProcessor による実テクニカル計算 (全履歴)
    processed_df = PolarsProcessor.calc_batch_technicals_vectorized(
        hist_map, latest_only=False
    )

    assert not processed_df.is_empty()
    assert "ma_divergence" in processed_df.columns
    assert "macd_status" in processed_df.columns
    assert "trading_value" in processed_df.columns

    # 3. EvaluationPhase の初期化とスタブ設定
    context = StubOrchestratorContext()

    # 財務データのモック
    mock_funda = pl.DataFrame(
        {
            "code": ["8306", "9999", "8165"],
            "per": [10.0, 15.0, 20.0],
            "pbr": [0.8, 1.2, 1.5],
            "roe": [12.0, 10.0, 8.0],
            "equity_ratio": [5.0, 45.0, 50.0],  # 8306 は 5.0%
            "dividend_yield": [3.5, 2.0, 1.5],
            "operating_margin": [15.0, 5.0, 4.0],
            "operating_income": [1000.0, 500.0, 200.0],
            "net_profit": [800.0, 400.0, 150.0],
        }
    )
    context.funda_repo.load_all.return_value = mock_funda
    context.duck_repo.load_fundamentals = lambda: mock_funda

    phase = EvaluationPhase(context)
    result_df = phase.execute(data_map=hist_map)

    # 4. 検証
    assert result_df is not None
    assert not result_df.is_empty()

    # (A) 銘柄 C (8165) は直近5日ゼロ出来高により PreFilter で除外されていること
    assert "8165" not in result_df["code"].to_list()
    assert context.uncalculable_df is not None
    uncalc_codes = context.uncalculable_df["code"].to_list()
    assert "8165" in uncalc_codes

    # (B) 銘柄 B (9999) は深い下落トレンドのため、スコアにかかわらず最大 WATCH に制限されていること (ゲートキーパー3)
    b_row = result_df.filter(pl.col("code") == "9999").to_dicts()[0]
    assert b_row["verdict"] in ["WATCH", "PASS"]
    assert b_row["verdict"] != "STRONG_BUY"
    assert b_row["verdict"] != "BUY"

    # (C) 銘柄 A (8306) は自己資本比率 5.0% が維持され、乖離率配点も加算されて高いスコア・判定を得ていること
    a_row = result_df.filter(pl.col("code") == "8306").to_dicts()[0]
    assert a_row["equity_ratio"] == 5.0  # 500% ではなく 5.0%
    assert a_row["quant_score"] > 40.0


def test_evaluation_phase_duck_save_failure_raises():
    """save_metrics の失敗時に警告で握りつぶさず、RuntimeError が送出されること (指摘8)"""
    context = StubOrchestratorContext()
    # DuckDB 保存で例外を発生させるモック
    context.duck_repo.save_metrics = MagicMock(side_effect=IOError("Disk full or lock timeout"))

    df_in = pl.DataFrame(
        {
            "code": ["1001"],
            "entry_date": [date(2026, 9, 20)],
            "price": [1000.0],
            "volume": [50000],
            "trading_value": [50000000.0],
        }
    )
    context.temp_data_map = {"1001": df_in}

    phase = EvaluationPhase(context)
    phase._prepare_input_data = MagicMock(return_value=df_in)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Database persistence failed in evaluation phase"):
        phase.execute()

