from typing import Any

from .base import BaseStrategy
from .generic import GenericStrategy


# Factory function
def get_strategy(strategy_name: str, config: dict[str, Any]) -> BaseStrategy:
    """Factory to create strategy instances."""
    # Default to Generic
    return GenericStrategy(config, strategy_name)
