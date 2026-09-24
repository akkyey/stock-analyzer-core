"""FinancialRepairService の単体テスト (境界値・比率スケーリング・冪等性検証)"""

import polars as pl
import pytest

from src.services.financial_repair import FinancialRepairService


def test_financial_repair_equity_ratio_idempotency_and_scaling():
    """自己資本比率のスケーリング（<=1.0 -> *100）と2回適用時の冪等性を検証する。"""
    # 銀行株や低自己資本株：
    # 5% (0.05) -> 5.0%
    # すでに % 単位の 5.0% -> 5.0% のまま維持（500% に暴走しないこと）
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
    assert repaired_1["equity_ratio"][0] == 5.0
    assert repaired_1["equity_ratio"][1] == 5.0
    assert repaired_1["equity_ratio"][2] == 35.0

    # 2回目の適用（冪等性の実証）
    repaired_2 = FinancialRepairService.repair(repaired_1)
    assert repaired_2["equity_ratio"][0] == 5.0
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
    # 0.99 は 99.0% にスケーリング
    assert repaired["equity_ratio"][1] == 99.0
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
