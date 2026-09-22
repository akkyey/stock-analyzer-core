from logging import getLogger

import numpy as np
import pandas as pd

from src.constants import LOWER_IS_BETTER_DEFAULTS
from src.utils import safe_float_or_none


class BaseCalculator:
    def __init__(self, config):
        self.config = config
        self.logger = getLogger(__name__)
        scoring_cfg = self._get_scoring_config()
        self.lower_is_better = scoring_cfg.get(
            "lower_is_better", LOWER_IS_BETTER_DEFAULTS
        )

    def _get_scoring_config(self, strategy_name: str | None = None):
        target = (
            strategy_name
            if strategy_name
            else self.config.get("current_strategy", "value_strict")
        )
        strategies = self.config.get("strategies", {})
        return strategies.get(target, self.config.get("scoring", {}))

    def _safe_float(self, value):
        """値を安全にfloatに変換する (Scalar版) - utils に委譲"""
        return safe_float_or_none(value)

    def _evaluate_metric_vectorized(self, metric: str, vals, th: float):
        """Evaluate metric condition (Vectorized)"""
        if metric == "rsi_oversold":
            return vals <= th
        elif metric == "rsi_overbought":
            return vals >= th
        elif metric in self.lower_is_better:
            return vals <= th
        else:
            return vals >= th

    def _evaluate_metric_scalar(self, metric: str, v: float, th: float) -> bool:
        """Evaluate metric condition (Scalar)"""
        if metric == "rsi_oversold":
            return v <= th
        elif metric == "rsi_overbought":
            return v >= th
        elif metric in self.lower_is_better:
            return v <= th
        return v >= th

    def _calc_dividend_points_vectorized(self, df: pd.DataFrame, condition, pts: int):
        """Calculate dividend points with quality adjustments (Vectorized)"""
        pts_series = np.where(condition, pts, 0)

        # Operating CF check (< 0 -> 0点)
        if "operating_cf" in df.columns:
            op_cf = pd.to_numeric(df["operating_cf"], errors="coerce")
            pts_series = np.where((op_cf < 0) & (pts_series > 0), 0, pts_series)

        # Payout Ratio check (> 100 -> -20点)
        if "payout_ratio" in df.columns:
            payout = pd.to_numeric(df["payout_ratio"], errors="coerce")
            pts_series = np.where(
                (payout > 100) & (pts_series > 0), pts_series - 20, pts_series
            )

        return pts_series

    def _calc_dividend_points_scalar(self, row: dict, pts: int) -> int:
        """Calculate dividend points with quality adjustments (Scalar)"""
        pts_to_add = pts
        op_cf = self._safe_float(row.get("operating_cf"))
        if (op_cf is not None) and (op_cf < 0):
            return 0

        payout = self._safe_float(row.get("payout_ratio"))
        if (payout is not None) and (payout > 100):
            pts_to_add -= 20
        return pts_to_add
