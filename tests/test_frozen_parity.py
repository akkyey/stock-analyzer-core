from datetime import datetime, timedelta

import pandas as pd
import polars as pl
import pytest

from src.fetcher.polars_processor import PolarsProcessor
from src.orchestration.context import OrchestratorContext
from src.orchestration.phases.evaluation import EvaluationPhase
from src.repositories.fundamentals_repository import FundamentalsRepository
from src.repositories.stock_repository import StockRepository
from src.services.financial_repair import FinancialRepairService


@pytest.fixture
def mock_context():
    # インメモリ DuckDB を使用したテスト用コンテキスト
    ctx = OrchestratorContext(debug_mode=True)
    # テスト前に初期化
    ctx.duck_repo.purge_holiday_data()
    return ctx


def test_rsi_stability_and_null_propagation(mock_context):
    """RSIの算出制約（30日未満はNull）と収束を検証する。"""

    # Case 1: 15日分（30日未満 -> Null）
    data_short = {
        "9991": pd.DataFrame(
            {
                "code": ["9991"] * 15,
                "Date": [datetime(2026, 1, i + 1) for i in range(15)],
                "Close": [100 + i for i in range(15)],
                "Volume": [1000] * 15,
            }
        )
    }

    # Case 2: 40日分（30日以上 -> Valid）
    data_long = {
        "9992": pd.DataFrame(
            {
                "code": ["9992"] * 40,
                "Date": [datetime(2026, 1, 1) + timedelta(days=i) for i in range(40)],
                "Close": [100 + i for i in range(40)],
                "Volume": [1000] * 40,
            }
        )
    }

    res_short = PolarsProcessor.calc_batch_technicals_vectorized(
        data_short, latest_only=True
    )
    res_long = PolarsProcessor.calc_batch_technicals_vectorized(
        data_long, latest_only=True
    )

    # 比較検証
    assert res_short.filter(pl.col("code") == "9991")["rsi_14"][0] is None
    assert res_long.filter(pl.col("code") == "9992")["rsi_14"][0] is not None
    assert res_long.filter(pl.col("code") == "9992")["rsi_14"][0] > 0


def test_holiday_purge_in_processor():
    """PolarsProcessor が計算前に土日を自動除外することを検証する。"""

    # 金曜(2)〜月曜(5)までの連続データ。土日(3,4)を含む。
    dates = [
        datetime(2026, 4, 17),  # Fri
        datetime(2026, 4, 18),  # Sat (to be removed)
        datetime(2026, 4, 19),  # Sun (to be removed)
        datetime(2026, 4, 20),  # Mon
    ]
    df_pandas = pd.DataFrame(
        {
            "code": ["7777"] * 4,
            "Date": dates,
            "Close": [100, 110, 120, 130],
            "Volume": [1000] * 4,
        }
    )

    # 計算前の中間状態を確認するため、内部メソッドを手動テストに近い形で呼ぶか、
    # あるいは全量データを返して件数を見る
    res = PolarsProcessor.calc_batch_technicals_vectorized(
        {"7777": df_pandas}, latest_only=False
    )

    # 4件中、土日が消えて 2件になっているはず
    assert len(res) == 2
    # 日付が金曜と月曜であることを確認
    days = [
        d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        for d in res["entry_date"].to_list()
    ]
    assert "2026-04-17" in days
    assert "2026-04-20" in days
    assert "2026-04-18" not in days


def test_early_memory_join_scoring(mock_context):
    """Early Memory Join により、スコアリング前に財務データが結合されていることを検証する。"""

    # 1. 銘柄マスタと財務データをシード
    stock_repo = mock_context.stock_repo
    funda_repo = mock_context.funda_repo

    stock_repo.upsert(
        [
            {
                "code": "8888",
                "name": "Test Stock",
                "sector": "Information",
                "market": "TSE",
            }
        ]
    )
    funda_repo.upsert(
        [
            {
                "code": "8888",
                "per": 15.0,
                "pbr": 1.2,
                "roe": 10.0,
                "dividend_yield": 3.0,
                "equity_ratio": 50.0,
                "market_cap": 1000000,
                "net_profit": 5000,
                "prev_net_profit": 4000,
                "shares_outstanding": 500,
                "operating_income": 6000,
            }
        ]
    )

    # 2. 市場データ
    data_map = {
        "8888": pd.DataFrame(
            {
                "code": ["8888"] * 40,
                "Date": [datetime(2026, 1, 1) + timedelta(days=i) for i in range(40)],
                "Close": [100 + (i % 5) for i in range(40)],
                "Volume": [1000] * 40,
            }
        )
    }

    # 3. EvaluationPhase 実行
    phase = EvaluationPhase(mock_context)
    final_df = phase.execute(data_map)

    # 4. 検証
    assert final_df is not None
    row = final_df.filter(pl.col("code") == "8888").to_dicts()[0]

    # スコアが Null や 0 ではなく計算されていること（財務データが結合されていた証拠）
    assert row["quant_score"] > 0
    # 財務カラムが保持されていること
    assert row["per"] == 15.0
    assert row["roe"] == 10.0


def test_financial_repair_backtracking():
    """D/E比から自己資本比率が正しく逆算されることを検証。"""

    # 自己資本比率が Null, D/E比が 100.0% (自己資本 = 負債) のケース
    # 100 / (1 + 100/100) = 50.0 (%) になるはず
    df = pl.DataFrame(
        {"code": ["1111"], "equity_ratio": [None], "debt_equity_ratio": [100.0]}
    )

    repaired_df = FinancialRepairService.repair(df)

    assert repaired_df["equity_ratio"][0] == 50.0


def test_financial_repair_per_fallback():
    """PERが欠損している場合に Fact (Price / (NetProfit/Shares)) で補完されることを検証。"""

    # price: 1000, net_profit: 5000, shares: 250 -> EPS: 20 -> PER: 50.0
    df = pl.DataFrame(
        {
            "code": ["2222"],
            "per": [None],
            "price": [1000.0],
            "net_profit": [5000.0],
            "shares_outstanding": [250.0],
        }
    )

    repaired_df = FinancialRepairService.repair(df)

    assert repaired_df["per"][0] == 50.0


def test_financial_repair_clipping():
    """異常値（負の自己資本比率など）がクリッピングされることを検証。"""

    df = pl.DataFrame(
        {
            "code": ["3333"],
            "equity_ratio": [-50.0],  # 債務超過だが比率として0以下はクリップ
            "per": [5000.0],  # 異常に高いPER
        }
    )

    repaired_df = FinancialRepairService.repair(df)

    assert repaired_df["equity_ratio"][0] == 0.0
    assert repaired_df["per"][0] == 1000.0  # Upper bound 1000
