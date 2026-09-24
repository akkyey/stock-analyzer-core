"""
GenericStrategy (軽量互換スタブ)

[移行メモ]:
旧来の複雑な多次元加点・減点・セクター正規化ロジック (660行) は、
新アーキテクチャ（エージェント協調型カルテ生成 + PolarsEngine 一次足切り）への移行に伴い、
以下のアーカイブへ完全退避されました:
`archive/legacy_strategies/generic_original.py`
"""

from typing import Any

import pandas as pd

from .base import BaseStrategy


class GenericStrategy(BaseStrategy):
    """旧スコアリング戦略の互換スタブ。実評価は PolarsEngine および AI エージェントが担う。"""

    def __init__(self, config: dict[str, Any], strategy_name: str = "generic"):
        super().__init__(config)
        self.strategy_name = strategy_name

    def calculate_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """後方互換用スコア計算（スコアカラムを維持して返却）"""
        res = df.copy()
        if "score" not in res.columns:
            res["score"] = 0.0
        return res

    def calculate_score_v2(self, df: pd.DataFrame) -> pd.DataFrame:
        """後方互換用スコア計算 v2"""
        return self.calculate_score(df)
