# src/calc/engines/polars_engine.py
"""
PolarsEngine: Native Polars implementation of Scoring Logic.
[v26.21] End-to-End Polars support.
Provides high-performance scoring using Polars Expressions.
Synced with GenericStrategy.py calculate_score_v2 (v2026-03).
"""
import polars as pl
from typing import Any, Optional
from logging import getLogger
from src.constants import LOWER_IS_BETTER_DEFAULTS, METRIC_CATEGORY

logger = getLogger(__name__)


class PolarsEngine:
    """Polars Expressions を使用した高速スコアリングエンジン。"""

    # [v13] 外部契約カラム（最低限含まれるべき計算結果）
    CONTRACT_COLUMNS = ["code", "entry_date", "quant_score", "strategy_name", "rank"]

    # [v13] 内部パージリスト（外部に漏洩させてはならない中間計算プレフィックス）
    INTERNAL_PURGE_PREFIXES = [
        "score_", "fund_norm", "trend_norm", "raw_with_noise", "cnt", "raw_score"
    ]

    @classmethod
    def filter_candidates(
        cls,
        df: pl.DataFrame,
        min_price: float = 50.0,
        min_volume: float = 1000.0,
        min_equity_ratio: float = 10.0,
        max_candidates: Optional[int] = None,
    ) -> pl.DataFrame:
        """[Agentic Pipeline] 機械的な一次足切りスクリーニングを行い、健全な母集団を抽出する。
        
        スクリプトの責務として、エージェントが評価するに値する健全性と流動性を担保する。
        - 最低株価（ボロ株・整理銘柄の除外）
        - 最低出来高（極端な流動性過疎の除外）
        - 財務健全性の下限（債務超過・極端な低自己資本比率の除外）
        """
        if df.is_empty():
            return df

        ldf = df.lazy()

        # 1. 流動性フィルター
        if "price" in df.columns:
            ldf = ldf.filter((pl.col("price") >= min_price) | pl.col("price").is_null())
        if "volume" in df.columns:
            ldf = ldf.filter((pl.col("volume") >= min_volume) | pl.col("volume").is_null())

        # 2. 財務安全性フィルター（極端な財務危機銘柄を一次排除）
        if "equity_ratio" in df.columns:
            ldf = ldf.filter(
                (pl.col("equity_ratio") >= min_equity_ratio) | pl.col("equity_ratio").is_null()
            )

        # 3. 並び替え（quant_score があれば降順）
        if "quant_score" in df.columns:
            ldf = ldf.sort(by="quant_score", descending=True, nulls_last=True)

        if max_candidates is not None and max_candidates > 0:
            ldf = ldf.limit(max_candidates)

        return ldf.collect()

    def __init__(self, config: dict[str, Any], strategy_name: str = "value_balanced"):
        self.logger = logger
        self.config = config
        self.strategy_name = strategy_name
        self.strategy_config = config.get("strategies", {}).get(strategy_name, {})
        self.thresholds: dict[str, Any] = {}
        self._current_cols: set[str] = set()
        self._load_thresholds()
        self.theory_max = self._calculate_theoretical_max()

    def _calculate_theoretical_max(self) -> dict[str, float]:
        """[v2026-04] 各カテゴリごとの理論最大値を算出する。"""
        if not self.strategy_config:
            return {"fund": 100.0, "trend": 45.0}
            
        # 1. ファンダメンタルズ最大値
        max_fund = float(self.strategy_config.get("base_score", 50.0))
        points_cfg = self.strategy_config.get("points", {})
        for weight in points_cfg.values():
            if weight > 0:
                max_fund += float(weight)
                
        status_bonuses = self.strategy_config.get("status_bonuses", {})
        for bonus_map in status_bonuses.values():
            max_fund += max((float(pts) for pts in bonus_map.values()), default=0.0)
            
        # 2. テクニカル最大値 (MACD: 10, RSI: 15, TrendUp: 10, Sector: 10)
        max_trend = 45.0
        
        return {"fund": max_fund, "trend": max_trend}

    def _load_thresholds(self):
        """thresholds.yaml から閾値設定を読み込む。"""
        import yaml
        from src.constants import THRESHOLDS_PATH

        try:
            with open(THRESHOLDS_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                self.thresholds = data.get("thresholds", {})
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to load thresholds from {THRESHOLDS_PATH}: {e}")
            self.thresholds = {}

    def filter_and_rank_native(
        self, df: pl.DataFrame, strategy_name: str, bypass_filter: bool = False
    ) -> pl.DataFrame:
        """[v5.0] Polars ネイティブなフィルタリングとランキング。"""
        if df.is_empty():
            return df

        # [v5.1] 自身の戦略設定から直接 thresholds を取得
        cfg = self.strategy_config.get("thresholds", {})
        if not cfg or bypass_filter:
            return df.sort("quant_score", descending=True)

        filters = []
        for metric, val in cfg.items():
            if metric in df.columns:
                is_lower = self._determine_direction(metric, None)
                if is_lower:
                    filters.append(pl.col(metric) <= val)
                else:
                    filters.append(pl.col(metric) >= val)

        if filters:
            mask = filters[0]
            for f in filters[1:]:
                mask = mask & f
            df = df.filter(mask)

        return df.sort("quant_score", descending=True)

    def _build_linear_score_expr(
        self, col_name: str, min_val: float, target_val: float, points: float
    ) -> pl.Expr:
        """[v2026-03] 線形評価（Grade Scoring）用の Polars Expression を構築する。"""
        is_lower_better = min_val > target_val
        val_col = pl.col(col_name)

        if is_lower_better:
            expr = (min_val - val_col) / (min_val - target_val)
        else:
            expr = (val_col - min_val) / (target_val - min_val)

        return (expr.clip(0, 1).fill_null(0.0)) * points

    def calculate_scores(self, df: pl.DataFrame, strat_cfg: dict = None) -> pl.DataFrame:
        """[v2026-04] Polars Lazy Frame を使用した 5レイヤー評価システム。
        再帰加算を完全に廃止し、水平集計 (sum_horizontal) によるフラットなプランでハングを解消する。
        """
        if df.is_empty():
            return df

        if strat_cfg is None:
            strat_cfg = self.config.get("strategies", {}).get(self.strategy_name, {})
        
        self.strategy_config = strat_cfg
        self.logger.info(f"🚀 PolarsEngine: Start calculating {len(df)} rows for '{self.strategy_name}'")

        if not strat_cfg:
            return df.with_columns([
                pl.lit(0.0).alias("quant_score"),
                pl.lit(self.strategy_name).alias("strategy_name"),
            ])

        # 0. 初期前処理
        df = self._ensure_numeric(df, strat_cfg)
        ldf = df.lazy()

        base_score = float(strat_cfg.get("base_score", 50.0))
        style_name = strat_cfg.get("default_style", "value_balanced")
        weights = strat_cfg.get(
            "weights",
            self.config.get("scoring_v2", {})
            .get("styles", {})
            .get(style_name, {"weight_fund": 0.7, "weight_tech": 0.3}),
        )
        wf, wt = float(weights.get("weight_fund", 0.7)), float(weights.get("weight_tech", 0.3))
        noise_expr = (pl.col("code").hash(seed=42) % 10).cast(pl.Float64) * 0.0001
        
        # 1. カテゴリ別 Expression コンテナ
        category_map: dict[str, list[pl.Expr]] = {
            "value": [], "growth": [], "quality": [], "base": [pl.lit(base_score)],
            "penalty": [], "trend": [pl.lit(50.0)], "final_adjustment": [],
        }
        metadata_map = strat_cfg.get("metrics_metadata", {})
        points_cfg = strat_cfg.get("points", {})

        # -- Fundamental Metrics 収集 --
        linear_candidates = {
            "roe": (5.0, 15.0), "per": (15.0, 10.0), "payout_ratio": (30.0, 100.0),
            "dividend_yield": (2.0, 4.0), "sales_growth": (5.0, 20.0), "profit_growth": (5.0, 30.0),
        }

        for metric, weight in points_cfg.items():
            if metric not in df.columns:
                continue
            th = self._get_threshold(metric)
            if th is None:
                continue

            meta = metadata_map.get(metric, {})
            is_lower_better = self._determine_direction(metric, meta.get("direction"))
            pts_val = float(weight)

            val_col = pl.col(metric)
            condition = (val_col <= th) if is_lower_better else (val_col >= th)
            condition = (val_col.is_not_null()) & condition

            if metric in linear_candidates:
                m_min, m_target = linear_candidates[metric]
                pts_expr = self._build_linear_score_expr(metric, m_min, m_target, pts_val)
            else:
                pts_expr = pl.when(condition).then(pl.lit(pts_val)).otherwise(0.0)

            # 配当性向との複合評価
            if metric == "dividend_yield" and "payout_ratio" in df.columns:
                payout = pl.col("payout_ratio").fill_null(100.0)
                pts_expr = pl.when(condition & (payout < 100.0)).then(pts_expr) \
                             .when(condition & (payout >= 100.0)).then(pts_expr * 0.5) \
                             .otherwise(0.0)

            cat = meta.get("category") or METRIC_CATEGORY.get(metric, "quality")
            if cat in category_map:
                category_map[cat].append(pts_expr.fill_null(0.0))

        # -- Technical & Status Bonuses 収集 --
        tp = self.config.get("scoring_v2", {}).get("tech_points", {})
        if "macd_hist" in df.columns:
            m_hist = pl.col("macd_hist").fill_null(0.0)
            category_map["trend"].append(
                pl.when(m_hist > 0).then(5.0 + (m_hist * 2.0).clip(0, 5.0))
                .when(m_hist < 0).then(-5.0 + (m_hist * 2.0).clip(-5.0, 0))
                .otherwise(0.0)
            )
        if "rsi_14" in df.columns:
            category_map["trend"].append(((50.0 - pl.col("rsi_14").fill_null(50.0)) * 0.5).clip(-15.0, 15.0))

        # ステータスボーナス一括抽出
        bonus_growth_list, bonus_penalty_list, bonus_all_list = [], [], []
        status_bonuses = strat_cfg.get("status_bonuses", {})
        for col_name, bonus_map in status_bonuses.items():
            if col_name not in df.columns: continue
            for val_key, pts in bonus_map.items():
                v_str, pts_f = str(val_key), float(pts)
                cond = (pl.col(col_name).cast(pl.Utf8) == v_str) | (pl.col(col_name).cast(pl.Utf8) == v_str.replace(".0", ""))
                expr = pl.when(cond).then(pts_f).otherwise(0.0)
                bonus_all_list.append(expr)
                if pts_f > 0: bonus_growth_list.append(expr)
                elif pts_f < 0: bonus_penalty_list.append(expr.abs())

        # 2. パイプライン・チェーン定義
        # -- Layer 1: 水平集計 --
        ldf = ldf.with_columns([
            pl.sum_horizontal(category_map["base"]).fill_null(0.0).alias("score_base"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["value"]).fill_null(0.0).alias("score_value"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["growth"] + bonus_growth_list).fill_null(0.0).alias("score_growth"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["quality"]).fill_null(0.0).alias("score_quality"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["penalty"] + bonus_penalty_list).fill_null(0.0).alias("score_penalty"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["trend"]).fill_null(0.0).alias("score_trend"),
            pl.sum_horizontal([pl.lit(0.0)] + category_map["final_adjustment"] + bonus_all_list).fill_null(0.0).alias("score_final_adj"),
        ])
    
        # -- Layer 2: 正規化スケーリング --
        ldf = ldf.with_columns([
            ((pl.col("score_base") + pl.col("score_value") + pl.col("score_growth") + pl.col("score_quality"))
             / pl.lit(max(self.theory_max["fund"], 1.0)) * 100.0).fill_null(0.0).clip(0, 100).alias("fund_norm"),
            (pl.col("score_trend") / pl.lit(max(self.theory_max["trend"], 1.0)) * 100.0).fill_null(0.0).clip(0, 100).alias("trend_norm")
        ])
    
        # -- Layer 3: リスク調整 & サイズ係数 --
        m_cap = pl.col("market_cap").fill_null(0.0) if "market_cap" in df.columns else pl.lit(100.0)
        size_mult = pl.when(m_cap >= 100.0).then(1.0).when(m_cap >= 30.0).then(0.8).otherwise(0.5)
        
        ldf = ldf.with_columns([
            (((pl.col("fund_norm") * wf) + (pl.col("trend_norm") * wt) + pl.col("score_final_adj")) * size_mult).fill_null(0.0).alias("raw_score")
        ])
    
        # -- Layer 4: ランキング & 相対評価 --
        final_ldf = ldf.with_columns([
            pl.col("raw_score").clip(0, 100).alias("quant_score_normalized")
        ]).with_columns([
            (pl.col("quant_score_normalized").fill_null(0.0) + noise_expr).alias("raw_with_noise")
        ]).with_columns([
            pl.col("raw_with_noise").rank(descending=True).over("sector").alias("rank"),
            pl.len().over("sector").alias("cnt")
        ]).with_columns([
            pl.col("quant_score_normalized").fill_null(0.0).round(2).alias("quant_score"),
            pl.lit(self.strategy_name).alias("strategy_name")
        ])

        # -- Layer 5: ハードキャップ制限 --
        if "rsi_14" in df.columns:
            is_dead = ((pl.col("rsi_14") - 50.0).abs() < 0.1).fill_null(False)
            final_ldf = final_ldf.with_columns([
                pl.when(is_dead).then(pl.col("quant_score").clip(0, 50.0)).otherwise(pl.col("quant_score")).alias("quant_score")
            ])

        # -- Layer 6: スキーマ・契約プロトコル (v13: Negative-based Isolation) --
        # 内部計算用のゴミカラムのみを除去し、入力属性（sector等）と契約カラム（rank等）を維持する。
        all_cols = final_ldf.collect_schema().names()
        output_cols = [
            c for c in all_cols 
            if not any(c.startswith(p) for p in self.INTERNAL_PURGE_PREFIXES)
            or c in self.CONTRACT_COLUMNS
        ]
        
        final_ldf = final_ldf.select(output_cols)
        
        # 監査ログ: 予定通りパージされたカラムを可視化
        purged = set(all_cols) - set(output_cols)
        if purged:
            self.logger.debug(f"Protocol: Purged internal calculation clutter: {purged}")

        # 3. 実行
        try:
            return final_ldf.collect()
        except Exception as e:
            self.logger.error(f"❌ PolarsEngine collection failed: {e}")
            raise

    def _ensure_numeric(self, df: pl.DataFrame, strat_cfg: dict) -> pl.DataFrame:
        targets = list(strat_cfg.get("points", {}).keys()) + ["macd_hist", "rsi_14", "market_cap", "payout_ratio"]
        cast_exprs = []
        for col in df.columns:
            if col in targets:
                if df.schema[col] == pl.Utf8:
                    cast_exprs.append(pl.col(col).str.replace_all("[%,]", "").cast(pl.Float64, strict=False).fill_null(0.0))
                else:
                    cast_exprs.append(pl.col(col).cast(pl.Float64).fill_null(0.0))
        return df.with_columns(cast_exprs) if cast_exprs else df

    def _get_threshold(self, metric: str) -> float | None:
        strat_thresholds = self.strategy_config.get("thresholds", {})
        if metric in strat_thresholds: return float(strat_thresholds[metric])
        if metric in self.thresholds: return float(self.thresholds[metric])
        for cat_vals in self.thresholds.values():
            if isinstance(cat_vals, dict) and metric in cat_vals: return float(cat_vals[metric])
        return None

    def _determine_direction(self, metric: str, direction_meta: str | None) -> bool:
        if direction_meta == "lower": return True
        if direction_meta == "higher": return False
        return metric in LOWER_IS_BETTER_DEFAULTS or metric.endswith("_max")
