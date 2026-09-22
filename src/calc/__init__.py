import pandas as pd

from .base import BaseCalculator
from .engine import ScoringEngine


class Calculator(BaseCalculator):
    """
    Main Calculator Interface.
    Uses ScoringEngine for all scoring logic.
    """

    def __init__(self, config):
        super().__init__(config)
        self.engine = ScoringEngine(config)

    def calc_quant_score(self, data, strategy_name=None):
        """
        [v8.0] Override to delegate to ScoringEngine strategies.
        Returns the scalar or series 'quant_score'.
        """
        res = self.calc_v2_score(data, strategy_name=strategy_name)
        if isinstance(data, pd.DataFrame):
            return res["quant_score"]
        return res.get("quant_score", 0.0)

    def calc_v2_score(self, data, style="value_balanced", strategy_name=None):
        """
        [v8.0] Calculates score using the new Strategy Registry.
        """
        if not strategy_name:
            strategy_name = self.config.get("current_strategy", "value_strict")

        strategy = self.engine.get_strategy(strategy_name)

        if isinstance(data, pd.DataFrame):
            return strategy.calculate_score(data)
        # Scalar Mode
        elif hasattr(strategy, "calculate_score_scalar"):
            return strategy.calculate_score_scalar(data)
        else:
            # Fallback wrapper
            df = pd.DataFrame([data])
            res = strategy.calculate_score(df)
            return res.iloc[0].to_dict()
