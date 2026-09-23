from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import polars as pl


class StubDuckRepository:
    """DuckDB への依存を排除した高速スタブリポジトリ"""

    def __init__(self):
        self.load_stocks = MagicMock()
        self.load_metrics = MagicMock()
        self.save_metrics = MagicMock()
        self.save_ranking = MagicMock()


class StubOrchestratorContext:
    """オーケストレーション層のユニットテスト用スタブコンテキスト"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {"strategies": {"Balanced Strategy": {}}}
        self.logger = MagicMock()
        self.duck_repo = StubDuckRepository()
        self.limit = None
        self.report_url = None
        self.temp_data_map = {}

    def log_info(self, msg):
        self.logger.info(msg)

    def log_error(self, msg):
        self.logger.error(msg)

    def log_warn(self, msg):
        self.logger.warning(msg)


class DataGenerator:
    """高精度な検証用データを生成するユーティリティ"""

    @staticmethod
    def generate_history(code: str, days: int = 120) -> pd.DataFrame:
        """テクニカル指標算出（MA75等）に十分な期間の合成データを生成"""
        start_date = datetime(2026, 1, 1)
        dates = [start_date + timedelta(days=i) for i in range(days)]

        # 銘柄特性に応じた価格推移
        if code == "1001":  # 上昇
            base = 1000.0
            prices = [base + (i * 2.0) + (np.sin(i * 0.1) * 5) for i in range(days)]
        elif code == "1002":  # 下降
            base = 2000.0
            prices = [base - (i * 1.0) + (np.cos(i * 0.1) * 8) for i in range(days)]
        else:  # フラット
            base = 1500.0
            prices = [base + (np.random.normal(0, 5)) for i in range(days)]

        df = pd.DataFrame(
            {
                "code": [code] * days,
                "Date": pd.to_datetime(dates),
                "Close": prices,
                "Volume": [10000 + i * 100 for i in range(days)],
            }
        )
        return df

    @staticmethod
    def get_dummy_stocks_df() -> pl.DataFrame:
        """銘柄マスタのダミーデータ"""
        return pl.DataFrame(
            {
                "code": ["1001", "1002"],
                "name": ["Stock Up", "Stock Down"],
                "sector": ["Tech", "Retail"],
            }
        )
