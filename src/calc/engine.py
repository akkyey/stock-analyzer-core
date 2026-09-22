"""ScoringEngine: 戦略パターンに基づくスコアリングオーケストレータ

責務:
- 投資戦略クラス（BaseStrategy継承クラス）の登録と管理
- 各銘柄データに対するスコアリング処理のディスパッチ
- スコア計算後のフィルタリングおよびランキング実行
"""

from logging import getLogger
from typing import Any, ClassVar

import pandas as pd

from src.calc.strategies.base import BaseStrategy
from src.calc.strategies.generic import GenericStrategy


class ScoringEngine:
    """各戦略クラスにスコアリング処理を振り分ける中央エンジン。

    Attributes:
        config (Dict[str, Any]): システム設定。
        _strategy_cache (Dict[str, BaseStrategy]): 生成済み戦略インスタンスのキャッシュ。
    """

    # Strategy Registry
    STRATEGY_REGISTRY: ClassVar[dict[str, type[BaseStrategy]]] = {}
    GENERIC_STRATEGIES: ClassVar[set[str]] = set()

    def __init__(self, config: dict[str, Any]):
        """ScoringEngine を初期化する。

        Args:
            config (Dict[str, Any]): アプリケーション全体の設定。
        """
        self.config = config
        self.logger = getLogger(__name__)
        self._strategy_cache: dict[str, BaseStrategy] = {}

        # [Fix] Auto-register strategies defined in config as Generic
        if "strategies" in config:
            for name in config["strategies"]:
                self.GENERIC_STRATEGIES.add(name)

    def get_strategy(self, strategy_name: str) -> BaseStrategy:
        """指定した戦略名のインスタンスを取得（または生成）する。

        Args:
            strategy_name (str): 戦略名。

        Returns:
            BaseStrategy: 戦略インスタンス。
        """
        if strategy_name in self._strategy_cache:
            return self._strategy_cache[strategy_name]

        if strategy_name in self.STRATEGY_REGISTRY:
            strategy_class = self.STRATEGY_REGISTRY[strategy_name]
            strategy = strategy_class(self.config)
        elif strategy_name in self.GENERIC_STRATEGIES:
            strategy = GenericStrategy(self.config, strategy_name)
        else:
            # 未知の戦略の場合は GenericStrategy を使用
            self.logger.warning(
                f"Unknown strategy '{strategy_name}', using GenericStrategy"
            )
            strategy = GenericStrategy(self.config, strategy_name)

        strategy.logger = self.logger
        self._strategy_cache[strategy_name] = strategy
        return strategy

    def calculate_score(
        self,
        data: pd.DataFrame,
        strategy_name: str | None = None,
        legacy_engine: bool = False,
    ) -> pd.DataFrame:
        """指定された戦略を用いて、データのスコアを算出する。

        Args:
            data (pd.DataFrame): 銘柄データの DataFrame。
            strategy_name (Optional[str], optional): 戦略名。省略時は設定ファイルのデフォルトを使用。
            legacy_engine (bool): True の場合、従来の逐次処理エンジンを使用。

        Returns:
            pd.DataFrame: スコア計算結果が付与された DataFrame。
        """
        import time

        start_t = time.perf_counter()

        if strategy_name is None:
            # [v2.0 Fix] デフォルトを Balanced Strategy に変更 (value_strict は配点が高すぎるため)
            strategy_name = self.config.get("current_strategy", "Balanced Strategy")

        strategy = self.get_strategy(strategy_name)

        try:
            if legacy_engine:
                self.logger.info(f"🐢 Using Legacy Engine (V1) for {strategy_name}")
                # Ensure data is Pandas for legacy
                if not isinstance(data, pd.DataFrame):
                    data = pd.DataFrame(data.to_dicts()).set_index("code")
                result = strategy.calculate_score(data)
            elif self.config.get("use_polars"):
                self.logger.info(
                    f"🚀 Using Ultra-Fast Engine (Polars/V4) for {strategy_name}"
                )
                import polars as pl
                from src.calc.engines.polars_engine import PolarsEngine

                engine = PolarsEngine(self.config, strategy_name=strategy_name)

                # [v4.6] Optimized: Use Polars directly if provided
                if isinstance(data, pl.DataFrame):
                    pl_data = data
                    is_polars_input = True
                else:
                    # Convert to Polars
                    pl_data = pl.from_pandas(data.reset_index())
                    is_polars_input = False

                pl_result = engine.calculate_scores(pl_data)

                if is_polars_input:
                    # [v4.6] Return Polars directly to keep pipeline efficient
                    result = pl_result
                else:
                    # [v4.1] Convert back to Pandas for legacy callers
                    result_pd = pd.DataFrame(pl_result.to_dicts())
                    if not result_pd.empty and "code" in result_pd.columns:
                        result_pd = result_pd.set_index("code")
                    result = result_pd.reindex(data.index)
            else:
                self.logger.info(f"⚡ Using Fast Engine (V2) for {strategy_name}")
                # Ensure data is Pandas for V2
                if not isinstance(data, pd.DataFrame):
                    data = pd.DataFrame(data.to_dicts()).set_index("code")
                # [v2.0] MultiIndex Vectorization Engine
                if hasattr(strategy, "calculate_score_v2"):
                    result = strategy.calculate_score_v2(data)
                else:
                    self.logger.warning(
                        f"Strategy {strategy_name} does not support V2. Falling back to V1."
                    )
                    result = strategy.calculate_score(data)

            elapsed = time.perf_counter() - start_t
            self.logger.info(
                f"⏱️ Scoring completed in {elapsed:.4f}s ({'Legacy' if legacy_engine else 'Polars' if self.config.get('use_polars') else 'V2'})"
            )

            # [v12.0 Cleanup / v4.6] Merge scores back
            if self.config.get("use_polars") and isinstance(result, pl.DataFrame):
                # result is already the full merged DataFrame in Polars
                return result

            merged = data.copy()
            for col in result.columns:
                merged[col] = result[col]
            return merged

        except Exception as e:
            self.logger.error(
                f"Error in calculate_score with {strategy_name}: {e}", exc_info=True
            )
            if self.config.get("use_polars"):
                import polars as pl
                if isinstance(data, pl.DataFrame):
                    # [v17.1] Safe fallback for Polars
                    fill_exprs = [pl.lit(0.0).alias(c) for c in [
                        "quant_score", "score_value", "score_growth", "score_quality", "score_trend", "score_penalty"
                    ]]
                    fallback = data.with_columns(fill_exprs)
                    fallback = fallback.with_columns([pl.lit(strategy_name).alias("strategy_name")])
                    return fallback

            fallback = data.copy()
            for col in [
                "quant_score",
                "score_value",
                "score_growth",
                "score_quality",
                "score_trend",
                "score_penalty",
            ]:
                fallback[col] = 0.0
            fallback["strategy_name"] = strategy_name
            return fallback

    def register_strategy(self, name: str, strategy_class: type[BaseStrategy]) -> None:
        """
        Register a new strategy class.

        Args:
            name: Name to register the strategy under
            strategy_class: Strategy class (must inherit from BaseStrategy)
        """
        if not issubclass(strategy_class, BaseStrategy):
            raise ValueError(f"{strategy_class} must inherit from BaseStrategy")

        self.STRATEGY_REGISTRY[name] = strategy_class
        # Clear cache to ensure new strategy is used
        if name in self._strategy_cache:
            del self._strategy_cache[name]

        self.logger.info(f"Registered strategy: {name}")

    def list_strategies(self) -> list:
        """List all available strategy names."""
        all_strategies = set(self.STRATEGY_REGISTRY.keys()) | self.GENERIC_STRATEGIES
        return sorted(all_strategies)

    # ============================================================
    # [v12.0] AnalysisEngine からの移植: フィルタリングとランキング
    # ============================================================
    def filter_and_rank(
        self, df: pd.DataFrame, strategy_name: str, bypass_filter: bool = False
    ) -> pd.DataFrame:
        """スコア計算済みのデータをフィルタリングし、ランキング用にソートする。

        Args:
            df (pd.DataFrame): スコア計算済みの銘柄データ。
            strategy_name (str): 適用する戦略名。
            bypass_filter (bool): True の場合、スコアや財務条件によるフィルタリングをスキップする。

        Returns:
            pd.DataFrame: フィルタリング・ソート済みの銘柄データ。
        """
        if bypass_filter:
            self.logger.debug(f"Bypassing filters for {strategy_name}")
            return df.sort_values(["quant_score", "code"], ascending=[False, True])

        min_score = self.config.get("filter", {}).get("min_quant_score", 0)

        # 1. 戦略固有のフィルター
        strategy_cfg = self.config.get("strategies", {}).get(strategy_name, {})
        hard_filters = strategy_cfg.get("min_requirements", {})

        # 2. グローバルフィルター（戦略固有がない場合）
        if not hard_filters:
            hard_filters = self.config.get("hard_filters", {})

        # 3. ベーススコアフィルタリング
        before_score = len(df)
        candidates = df[df["quant_score"] >= min_score].copy()
        self.logger.debug(
            f"Score Filter (>= {min_score}): "
            f"{len(candidates)} (Dropped {before_score - len(candidates)})"
        )

        # 4. ハードフィルター適用
        for key, val in hard_filters.items():
            # [v23.2] 指標ベース名の解決 (roe -> roe_target, roe_calc 等への対応)
            col = key.replace("_max", "").replace("min_", "")
            col_name = col

            # カラムが見つからない場合のフォールバック検索
            if col not in candidates.columns:
                # 類推検索
                for c in candidates.columns:
                    if c.startswith(col) or col.startswith(c):
                        col_name = c
                        break
                else:
                    self.logger.warning(
                        f"⚠️ Filter '{key}' ignored: Column '{col}' missing in data. "
                        f"Available: {list(candidates.columns)[:5]}..."
                    )
                    continue

            before_filter = len(candidates)
            try:
                # 欠損値を除去してから比較
                candidates = candidates[candidates[col_name].notna()]

                # 数値比較のために float 変換を試みる
                target_series = pd.to_numeric(candidates[col_name], errors="coerce")

                if key.endswith("_max"):
                    candidates = candidates[target_series <= float(val)]
                else:
                    candidates = candidates[target_series >= float(val)]
            except Exception as e:
                self.logger.error(f"Error applying filter {key}: {e}")
                continue

            self.logger.debug(
                f"Filter {key} ({col}) against {val}: "
                f"{len(candidates)} (Dropped {before_filter - len(candidates)})"
            )

        # 5. ソート
        candidates = candidates.sort_values(
            ["quant_score", "code"], ascending=[False, True]
        )
        return candidates
