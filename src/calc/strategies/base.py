# src/calc/strategies/base.py
"""
BaseStrategy: Abstract Base Class for Investment Strategies
[v8.0] Enhanced with common utility methods from BaseCalculator.
"""
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any

import numpy as np
import pandas as pd

from src.constants import LOWER_IS_BETTER_DEFAULTS
from src.utils import safe_float_or_none


class BaseStrategy(ABC):
    """
    Abstract Base Class for Investment Strategies.
    Provides common utility methods and defines the interface.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.v2_config = config.get("scoring_v2", {})
        self.logger = getLogger(__name__)
        self.lower_is_better = config.get("scoring", {}).get(
            "lower_is_better", LOWER_IS_BETTER_DEFAULTS
        )

    @abstractmethod
    def calculate_score(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate scores for the given dataset.
        Returns DataFrame with 'quant_score' and breakdown columns.
        """
        pass

    def _get_scoring_config(self, strategy_name: str | None = None) -> dict[str, Any]:
        """Helper to get points/threshold from config."""
        target = (
            strategy_name
            if strategy_name
            else self.config.get("current_strategy", "Balanced Strategy")
        )
        strategies = self.config.get("strategies", {})
        return strategies.get(target, {})

    def _safe_float(self, value) -> float | None:
        """Convert value to float safely (Scalar version) - delegated to utils."""
        return safe_float_or_none(value)

    def _evaluate_metric_vectorized(
        self, metric: str, vals: pd.Series, th: float
    ) -> pd.Series:
        """Evaluate metric condition (Vectorized)."""
        if metric == "rsi_oversold":
            return vals <= th
        elif metric == "rsi_overbought":
            return vals >= th
        elif metric in self.lower_is_better or metric.endswith("_max"):
            return (vals <= th) & (vals > 0)
        else:
            return vals >= th

    def _evaluate_metric_scalar(self, metric: str, v: float, th: float) -> bool:
        """Evaluate metric condition (Scalar)."""
        if metric == "rsi_oversold":
            return v <= th
        elif metric == "rsi_overbought":
            return v >= th
        elif metric in self.lower_is_better or metric.endswith("_max"):
            return v <= th
        return v >= th

    def _calc_dividend_points_vectorized(
        self, df: pd.DataFrame, condition: pd.Series, pts: int
    ) -> np.ndarray:
        """Calculate dividend points with quality adjustments (Vectorized)."""
        pts_series = np.where(condition, pts, 0)

        if "operating_cf" in df.columns:
            op_cf = pd.to_numeric(df["operating_cf"], errors="coerce")
            pts_series = np.where((op_cf < 0) & (pts_series > 0), 0, pts_series)

        if "payout_ratio" in df.columns:
            payout = pd.to_numeric(df["payout_ratio"], errors="coerce")
            pts_series = np.where(
                (payout > 100) & (pts_series > 0), pts_series - 20, pts_series
            )

        return pts_series

    def _calc_dividend_points_scalar(self, row: dict, pts: int) -> int:
        """Calculate dividend points with quality adjustments (Scalar)."""
        pts_to_add = pts
        op_cf = self._safe_float(row.get("operating_cf"))
        if op_cf is not None and op_cf < 0:
            return 0

        payout = self._safe_float(row.get("payout_ratio"))
        if payout is not None and payout > 100:
            pts_to_add -= 20
        return pts_to_add
