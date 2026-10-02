"""FinancialRepairService の単体テスト (境界値・比率スケーリング・冪等性検証)"""

from datetime import date

import polars as pl
import pytest

from src.services.financial_repair import FinancialRepairService


def test_financial_repair_equity_ratio_idempotency_without_scaling():
    """自己資本比率は % 表記のまま扱い (1 未満でも 100 倍しない)、2 回適用しても変わらない。

    財務データ (シード・EDINET 取り込み) は常に % 表記で保存される。1% 未満の実在値
    (債務超過寸前の企業など) を 100 倍すると、健全な企業として扱ってしまう。
    """
    df = pl.DataFrame(
        {
            "code": ["8306", "8316", "9984"],
            "equity_ratio": [0.05, 5.0, 35.0],
            "operating_income": [1000.0, 2000.0, 3000.0],
            "net_profit": [800.0, 1500.0, 2500.0],
        }
    )

    repaired_1 = FinancialRepairService.repair(df)
    # 1回目の検証
    assert repaired_1["equity_ratio"][0] == 0.05
    assert repaired_1["equity_ratio"][1] == 5.0
    assert repaired_1["equity_ratio"][2] == 35.0

    # 2回目の適用（冪等性の実証）
    repaired_2 = FinancialRepairService.repair(repaired_1)
    assert repaired_2["equity_ratio"][0] == 0.05
    assert repaired_2["equity_ratio"][1] == 5.0
    assert repaired_2["equity_ratio"][2] == 35.0


def test_financial_repair_boundary_values():
    """自己資本比率の 1.0 (1.0% 表記) / 0.0 (債務超過) 付近の境界値テスト。"""
    df = pl.DataFrame(
        {
            "code": ["1001", "1002", "1003", "1004"],
            "equity_ratio": [1.0, 0.99, 0.0, -10.0],
            "current_ratio": [1.5, 0.8, 150.0, None],
            "debt_equity_ratio": [1.2, 0.5, 80.0, None],
        }
    )

    repaired = FinancialRepairService.repair(df)
    # 1.0 は 1% 表記とみなして 1.0 のまま維持
    assert repaired["equity_ratio"][0] == 1.0
    # 0.99 は 0.99% のまま (100 倍しない)
    assert repaired["equity_ratio"][1] == 0.99
    # 0.0 はそのまま 0.0
    assert repaired["equity_ratio"][2] == 0.0
    # 負の比率はクリップされて 0.0
    assert repaired["equity_ratio"][3] == 0.0

    # 指摘4: 自己資本比率以外の倍率指標（current_ratio, debt_equity_ratio）は100倍されない
    assert repaired["current_ratio"][0] == 1.5
    assert repaired["current_ratio"][1] == 0.8
    assert repaired["debt_equity_ratio"][0] == 1.2


def test_financial_repair_turnaround_detection():
    """営業利益赤字・最終黒字銘柄のターンアラウンド判定とフラグ検証。"""
    df = pl.DataFrame(
        {
            "code": ["8165", "9999"],
            "prev_net_profit": [-1000.0, 200.0],
            "net_profit": [2000.0, 300.0],
        }
    )

    repaired = FinancialRepairService.repair(df)
    # 前期赤字かつ当期黒字 -> turnaround_black / is_turnaround = 1
    assert repaired["is_turnaround"][0] == 1
    assert repaired["turnaround_status"][0] == "turnaround_black"

    # 通常黒字銘柄
    assert repaired["is_turnaround"][1] == 0
    assert repaired["turnaround_status"][1] == "normal"


def test_financial_repair_direct_equity_ratio_calculation():
    """自己資本比率が未設定でも、total_assetsとnet_assetsから直接計算補完されること"""
    df = pl.DataFrame(
        {
            "code": ["1001", "1002"],
            "equity_ratio": [None, 30.0],
            "total_assets": [10_000_000_000.0, 50_000_000_000.0],
            "net_assets": [4_000_000_000.0, 15_000_000_000.0],
        }
    )

    repaired = FinancialRepairService.repair(df)
    # 40億 / 100億 * 100 = 40.0%
    assert repaired["equity_ratio"][0] == 40.0
    # 既存の 30.0% はそのまま維持
    assert repaired["equity_ratio"][1] == 30.0


def test_financial_repair_dynamic_valuation_metrics():
    """当日株価と財務確定FactからPER/PBR/利回り/時価総額が動的に正しく計算されること"""
    df = pl.DataFrame(
        {
            "code": ["1001"],
            "price": [2000.0],
            "net_profit": [10_000_000.0],
            "net_assets": [100_000_000.0],
            "shares_outstanding": [100_000.0],
            "dps": [60.0],
        }
    )

    repaired = FinancialRepairService.repair(df)
    # EPS = 10,000,000 / 100,000 = 100.0
    assert repaired["eps"][0] == 100.0
    # BPS = 100,000,000 / 100,000 = 1000.0
    assert repaired["bps"][0] == 1000.0
    # PER = 2000.0 / 100.0 = 20.0
    assert repaired["per"][0] == 20.0
    # PBR = 2000.0 / 1000.0 = 2.0
    assert repaired["pbr"][0] == 2.0
    # Dividend Yield = (60.0 / 2000.0) * 100 = 3.0%
    assert repaired["dividend_yield"][0] == 3.0
    # Market Cap = 2000.0 * 100,000 = 200,000,000.0
    assert repaired["market_cap"][0] == 200_000_000.0


def test_financial_repair_nan_inf_safety():
    """NaN や Inf を含む不正な入力値に対して、ゼロ除算や Inf 伝播を起こさず安全に処理されること"""
    df = pl.DataFrame(
        {
            "code": ["1001", "1002", "1003"],
            "price": [1000.0, float("nan"), float("inf")],
            "net_profit": [float("nan"), 1000.0, -500.0],
            "shares_outstanding": [0.0, float("inf"), 100.0],
            "net_assets": [float("inf"), float("nan"), 5000.0],
            "total_assets": [0.0, float("nan"), float("inf")],
            "dps": [float("nan"), -10.0, 50.0],
        }
    )

    repaired = FinancialRepairService.repair(df)
    assert len(repaired) == 3
    # ゼロ除算や inf から PER/PBR が inf にならず None になること
    assert repaired["per"][0] is None
    assert repaired["per"][1] is None
    assert repaired["pbr"][0] is None
    assert repaired["pbr"][1] is None





def test_split_adjustment_applies_only_to_documents_before_split():
    """分割前に提出された書類の 1 株当たり指標だけを、分割比率で補正する"""
    df = pl.DataFrame(
        {
            "code": ["8227", "7946", "1001"],
            "eps": [900.0, 50.0, 10.0],
            "bps": [9000.0, 500.0, 100.0],
            "dps": [300.0, 20.0, 5.0],
            "shares_outstanding": [36_000_000.0, 1_000_000.0, 100.0],
            # 7946 は分割後に提出された書類 (分割後の基準)、8227 は分割前
            "submitted_at": ["2025-05-20 10:00", "2026-06-25 10:00", None],
            "bs_submitted_at": ["2025-05-20 10:00", "2026-06-25 10:00", None],
        }
    )
    splits = pl.DataFrame(
        {
            "code": ["8227", "7946"],
            "split_date": [date(2026, 2, 19), date(2026, 3, 5)],
            "ratio": [3.0, 5.0],
        }
    )
    out = FinancialRepairService.apply_split_adjustment(df, splits).sort("code")
    rows = {r["code"]: r for r in out.to_dicts()}
    assert rows["8227"]["eps"] == 300.0 and rows["8227"]["bps"] == 3000.0
    assert rows["8227"]["dps"] == 100.0 and rows["8227"]["shares_outstanding"] == 108_000_000.0
    assert rows["7946"]["eps"] == 50.0 and rows["7946"]["shares_outstanding"] == 1_000_000.0
    assert rows["1001"]["eps"] == 10.0  # 分割の記録が無い銘柄は変えない


def test_split_adjustment_skips_values_with_unknown_basis():
    """値の基準日が不明な場合は補正しない (分割後の値を二重に割らない)"""
    df = pl.DataFrame({"code": ["8227"], "eps": [300.0], "submitted_at": [None], "bs_submitted_at": [None]})
    splits = pl.DataFrame({"code": ["8227"], "split_date": [date(2026, 2, 19)], "ratio": [3.0]})
    out = FinancialRepairService.apply_split_adjustment(df, splits)
    assert out["eps"][0] == 300.0
