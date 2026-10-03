"""第1層（Pre-Filter: 事前足切り）判定モジュール

責務:
クオンツスコアリングの計算リソースを割く前に、制度的・構造的に「投資対象になり得ない」地雷銘柄を
機械的に即座に除外（事前足切り）する。

除外基準（正統ルール）:
1. 売買不能（商い不成立）: 直近5営業日以内に出来高・売買代金ゼロの日が1日でもある銘柄
2. 極小流動性トラップ: 直近20営業日の1日平均売買代金が 3,000万円未満の銘柄
3. 構造的破綻（債務超過）: 直近開示で自己資本比率 <= 0% の銘柄（資本欠損）
4. 致命的キャッシュ枯渇: 営業CFマージン（営業CF / 売上高）< -10% の銘柄（金融等免除業種除く）
5. 超低位ボロ株: 株価 < 50円の銘柄

回避すべきアンチパターン（誤爆ルール）の非採用:
- 単年度減収（シクリカル株）、無配（グロース株）、直近急落（リバウンド候補）、
  単一バリュエーション（高PER株）での事前足切りは絶対に行わない。
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

import polars as pl

logger = logging.getLogger(__name__)


@dataclass
class PreFilterResult:
    """第1層フィルタリング結果を保持するコンテナ。"""

    passed_df: pl.DataFrame
    rejected_df: pl.DataFrame


class PreFilter:
    """第1層（Pre-Filter: 事前足切り）実行クラス。"""

    # 定数定義
    MIN_TRADING_VALUE_20D: float = 30_000_000.0  # 20営業日平均売買代金 3,000万円
    MIN_PRICE: float = 50.0  # 最低株価 50円
    MIN_EQUITY_RATIO: float = 0.0  # 自己資本比率 0%超（債務超過排除）
    MIN_OP_CF_MARGIN: float = -0.10  # 営業CFマージン -10%（致命的キャッシュ枯渇）

    # 既知の hard_filters キー定義（タイポ・未知キー検知用）
    KNOWN_HF_KEYS: set[str] = {
        "min_trading_value",
        "min_price",
        "min_equity_ratio",
        "min_op_cf_margin",
        "target_markets",
    }

    # 営業CFマージン評価の除外セクター（金融・保険など業態構造上の免除）
    CF_EXEMPT_SECTORS: set[str] = {
        "銀行業",
        "保険業",
        "証券、商品先物取引業",
        "その他金融業",
    }

    @classmethod
    def aggregate_timeseries_metrics(
        cls,
        df_timeseries: pl.DataFrame,
        target_date: Optional[str] = None,
    ) -> pl.DataFrame:
        """時系列市場データから直近5営業日および直近20営業日の流動性指標を集計する。

        Args:
            df_timeseries (pl.DataFrame): 時系列データ（code, entry_date, trading_value, price を含む）
            target_date (Optional[str]): 基準日（指定がない場合は有効データ最新日）

        Returns:
            pl.DataFrame: code, avg_trading_value_20d, zero_volume_days_5d を含む集計結果
        """
        if df_timeseries.is_empty():
            return pl.DataFrame(
                schema={
                    "code": pl.String,
                    "avg_trading_value_20d": pl.Float64,
                    "zero_volume_days_5d": pl.Int64,
                }
            )

        # 売買代金（円）の算出 (trading_value は既に円単位の売買代金。欠損時のみ volume * price)
        tv_col = (
            pl.col("trading_value")
            if "trading_value" in df_timeseries.columns
            else pl.lit(None).cast(pl.Float64)
        )
        if "volume" in df_timeseries.columns and "price" in df_timeseries.columns:
            vp_fallback = (pl.col("volume") * pl.col("price")).cast(pl.Float64)
        else:
            vp_fallback = pl.lit(0.0).cast(pl.Float64)

        df_calc = df_timeseries.with_columns(
            pl.coalesce([tv_col, vp_fallback]).fill_null(0.0).alias("_trading_yen")
        )

        # 全日付を降順ソート
        all_dates = (
            df_calc.select("entry_date")
            .unique()
            .sort("entry_date", descending=True)
            .to_series()
            .to_list()
        )

        # target_date が明示指定されている場合はその日付以降を採用
        if target_date and target_date in all_dates:
            valid_dates = all_dates[all_dates.index(target_date) :]
        else:
            # 最新日の出来高充填率（出来高 > 0 の割合）を確認
            # 大規模母集団（100件以上）かつ最新日の出来高充填率が極端に低い（50%未満の未確定日）場合のみ最新日をスキップ
            valid_dates = all_dates
            if len(df_calc) >= 100 and len(all_dates) >= 2:
                latest_date = all_dates[0]
                latest_records = df_calc.filter(pl.col("entry_date") == latest_date)
                valid_count = float((latest_records["_trading_yen"] > 0).sum())
                fill_rate = valid_count * 100.0 / len(latest_records)
                if fill_rate < 60.0:
                    valid_dates = all_dates[1:]

        dates_20 = valid_dates[:20]
        dates_5 = valid_dates[:5]

        # 20営業日平均売買代金の算出（円単位）
        df_20 = (
            df_calc.filter(pl.col("entry_date").is_in(dates_20))
            .group_by("code")
            .agg(
                pl.col("_trading_yen")
                .fill_null(0.0)
                .mean()
                .alias("avg_trading_value_20d")
            )
        )

        # 直近5営業日における出来高ゼロ日数のカウント (trading_value <= 0 または volume <= 0 または NULL)
        zero_cond = pl.col("_trading_yen") <= 0
        if "volume" in df_calc.columns:
            zero_cond = zero_cond | (pl.col("volume") <= 0) | pl.col("volume").is_null()

        df_5 = (
            df_calc.filter(pl.col("entry_date").is_in(dates_5))
            .with_columns(pl.when(zero_cond).then(1).otherwise(0).alias("_is_zero"))
            .group_by("code")
            .agg(pl.col("_is_zero").sum().alias("zero_volume_days_5d"))
        )

        # 各銘柄の直近データ日付（データ鮮度判定用: 市場全体の直近5営業日以内に取引があるか）
        market_recent_dates = [str(d) for d in all_dates[:5]]
        df_latest_trade = (
            df_calc.group_by("code")
            .agg(pl.col("entry_date").cast(pl.String).max().alias("latest_trade_date"))
            .with_columns(
                pl.col("latest_trade_date")
                .is_in(market_recent_dates)
                .alias("is_recent_trade")
            )
        )

        df_res = (
            df_20.join(df_5, on="code", how="full", coalesce=True)
            .join(df_latest_trade, on="code", how="left")
            .with_columns(
                [
                    pl.col("avg_trading_value_20d").fill_null(0.0),
                    pl.col("zero_volume_days_5d").fill_null(5),
                    pl.col("is_recent_trade").fill_null(False),
                ]
            )
        )
        if "code_right" in df_res.columns:
            df_res = df_res.drop("code_right")
        return df_res

    @classmethod
    def evaluate(
        cls,
        df_candidates: pl.DataFrame,
        df_liquidity: Optional[pl.DataFrame] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> PreFilterResult:
        """候補銘柄群に対し、第1層の地雷除外ルールを適用して適格群と除外群に完全分離する。

        Args:
            df_candidates (pl.DataFrame): 最新スナップショット指標データ
            df_liquidity (Optional[pl.DataFrame]): 事前集計済みの流動性指標（ない場合は df_candidates 内のカラムを参照）
            config (Optional[dict[str, Any]]): カスタム閾値設定

        Returns:
            PreFilterResult: passed_df (通過銘柄) と rejected_df (除外銘柄・理由付き)
        """
        if df_candidates.is_empty():
            empty_rejected = pl.DataFrame(
                schema={
                    "code": pl.String,
                    "name": pl.String,
                    "sector": pl.String,
                    "market": pl.String,
                    "filter_reason": pl.String,
                    "filter_detail": pl.String,
                }
            )
            return PreFilterResult(passed_df=df_candidates, rejected_df=empty_rejected)

        th = cls._thresholds(config)
        df, has_freshness = cls._attach_liquidity(df_candidates, df_liquidity)

        # Python辞書走査による厳格・明示的な理由付与
        passed_codes: list[Any] = []
        rejections: list[dict[str, Any]] = []
        for row in df.to_dicts():
            rejection = cls._first_rejection(row, th, has_freshness)
            if rejection is None:
                passed_codes.append(row["code"])
            else:
                reason, detail = rejection
                rejections.append(
                    {"code": row["code"], "filter_reason": reason, "filter_detail": detail}
                )

        passed_df = df.filter(pl.col("code").is_in(passed_codes)) if passed_codes else df.clear()
        reason_schema = {"filter_reason": pl.String, "filter_detail": pl.String}
        if rejections:
            reason_df = pl.DataFrame(rejections, schema={"code": pl.String, **reason_schema})
            rejected_df = df.join(reason_df, on="code", how="inner")
        else:
            rejected_df = pl.DataFrame(schema={**df.schema, **reason_schema})

        return PreFilterResult(passed_df=passed_df, rejected_df=rejected_df)

    @classmethod
    def _thresholds(cls, config: Optional[dict[str, Any]]) -> dict[str, Any]:
        """設定値の反映（親和的フォールバック・未知キー検知）"""
        hf = (config or {}).get("hard_filters", {})
        unknown_keys = set(hf.keys()) - cls.KNOWN_HF_KEYS
        if unknown_keys:
            logger.warning(
                f"⚠️ hard_filters に未知の設定キーが含まれています (無視されます): {unknown_keys}"
            )
        return {
            "min_tv_20d": float(hf.get("min_trading_value", cls.MIN_TRADING_VALUE_20D)),
            "min_price": float(hf.get("min_price", cls.MIN_PRICE)),
            "min_eq_ratio": float(hf.get("min_equity_ratio", cls.MIN_EQUITY_RATIO)),
            "min_op_cf_margin": float(hf.get("min_op_cf_margin", cls.MIN_OP_CF_MARGIN)),
            "target_markets": hf.get("target_markets", None),
        }

    LIQUIDITY_COLUMNS = (
        "avg_trading_value_20d",
        "zero_volume_days_5d",
        "latest_trade_date",
        "is_recent_trade",
    )

    @classmethod
    def _attach_liquidity(
        cls, df: pl.DataFrame, df_liquidity: Optional[pl.DataFrame]
    ) -> tuple[pl.DataFrame, bool]:
        """流動性指標を結合し、無い列を既定値で補う。(結果, 鮮度を判定できるか) を返す。"""
        if df_liquidity is not None and not df_liquidity.is_empty():
            df = df.drop([c for c in cls.LIQUIDITY_COLUMNS if c in df.columns])
            df = df.join(df_liquidity, on="code", how="left")
            if "code_right" in df.columns:
                df = df.drop("code_right")

        # 存在しない場合のデフォルトカラム作成
        if "avg_trading_value_20d" not in df.columns:
            tv_expr = pl.lit(0.0)
            if "trading_value" in df.columns:
                tv_expr = pl.col("trading_value").fill_null(0.0)
            if "volume" in df.columns and "price" in df.columns:
                vp_expr = pl.col("volume").fill_null(0.0) * pl.col("price").fill_null(0.0)
                # trading_value が未設定(0またはnull)の場合は volume * price を採用
                tv_expr = pl.when(tv_expr > 0.0).then(tv_expr).otherwise(vp_expr)
            df = df.with_columns(tv_expr.alias("avg_trading_value_20d"))

        if "zero_volume_days_5d" not in df.columns:
            df = df.with_columns(pl.lit(0).alias("zero_volume_days_5d"))

        # 時系列の集計 (最終取引日) が無い場合は、鮮度を判定できないため鮮度の足切りを行わない
        # (以前は固定の日付を入れて判定を素通りさせていた)
        has_freshness = "latest_trade_date" in df.columns
        if not has_freshness:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias("latest_trade_date"))

        if "is_recent_trade" not in df.columns:
            df = df.with_columns(pl.lit(True).alias("is_recent_trade"))
        return df, has_freshness

    @classmethod
    def _first_rejection(
        cls, row: dict[str, Any], th: dict[str, Any], has_freshness: bool
    ) -> Optional[tuple[str, str]]:
        """足切りルールを順に当て、最初に該当した (除外理由, 詳細) を返す。該当なしは None。"""
        rules = (
            cls._rule_delisted,
            cls._rule_no_price,
            cls._rule_freshness if has_freshness else None,
            cls._rule_zero_volume,
            cls._rule_liquidity,
            cls._rule_low_price,
            cls._rule_insolvent,
            cls._rule_undisclosed,
            cls._rule_cash_burn,
            cls._rule_market,
        )
        for rule in rules:
            if rule is not None:
                rejection = rule(row, th)
                if rejection is not None:
                    return rejection
        return None

    # --- 足切りルール (この順に判定する) ------------------------------------------------

    @staticmethod
    def _rule_delisted(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 0. 上場廃止 (JPX 一覧から消えた銘柄。銘柄マスタの月次更新で status='delisted' になる)
        if row.get("status") == "delisted":
            return "上場廃止", "JPX 上場銘柄一覧に掲載がありません (上場廃止・整理等)"
        return None

    @staticmethod
    def _rule_no_price(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 1. 株価データ欠損判定（市場データ取得不能: OHLCV未取得銘柄を最優先隔離）
        if row.get("price") is not None:
            return None
        if str(row.get("exclusion_reason") or "").startswith("市場データ提供なし"):
            return (
                "市場データ取得不能",
                "Yahoo Finance に株価データが提供されていません (PRO Market 等。30日ごとに再確認)",
            )
        return "市場データ取得不能", "市場価格データ欠損 (OHLCV未取得)"

    @staticmethod
    def _rule_freshness(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 2. 取引日・市場データ欠損判定（最終取引日が存在しない、または取引停止）
        latest_trade_date = row.get("latest_trade_date")
        if latest_trade_date is None:
            return "データ鮮度不足 (取引停止)", "最終取引日データ欠損"
        if row.get("is_recent_trade") is False:
            return (
                "データ鮮度不足 (取引停止)",
                f"最終取引日 ({latest_trade_date}) が直近5営業日範囲外",
            )
        return None

    @staticmethod
    def _rule_zero_volume(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 3. 売買不能判定（直近5営業日出来高データ欠損、または出来高ゼロ日あり）
        zero_days = row.get("zero_volume_days_5d")
        if zero_days is None:
            return "商い不成立", "出来高時系列データ欠損"
        if zero_days > 0:
            return "商い不成立", f"直近5営業日以内に出来高ゼロ日あり ({zero_days}日)"
        return None

    @staticmethod
    def _rule_liquidity(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 4. 極小流動性トラップ判定（売買代金欠損、または20日平均売買代金 < 3,000万円）
        avg_tv, min_tv = row.get("avg_trading_value_20d"), th["min_tv_20d"]
        if avg_tv is None:
            return "極小流動性トラップ", "売買代金データ欠損/算出不能"
        if avg_tv < min_tv:
            return (
                "極小流動性トラップ",
                f"20日平均売買代金不足 ({avg_tv / 10_000.0:,.0f}万円 < {min_tv / 10_000.0:,.0f}万円)",
            )
        return None

    @staticmethod
    def _rule_low_price(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 5. 超低位ボロ株判定（株価 < 50円）
        price, min_price = row.get("price"), th["min_price"]
        if price < min_price:
            return "超低位ボロ株", f"株価基準未満 ({price:,.0f}円 < {min_price:,.0f}円)"
        return None

    @staticmethod
    def _rule_insolvent(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 6. 構造的破綻（債務超過判定）: 純資産マイナス または 自己資本比率 <= 0
        net_assets, equity_ratio = row.get("net_assets"), row.get("equity_ratio")
        min_eq = th["min_eq_ratio"]
        if net_assets is not None and net_assets <= 0:
            return "構造的破綻 (債務超過)", f"純資産マイナス ({net_assets:,.0f}円)"
        if equity_ratio is not None and equity_ratio <= min_eq:
            return (
                "構造的破綻 (債務超過)",
                f"自己資本比率マイナス/ゼロ ({equity_ratio:.1f}% <= {min_eq:.1f}%)",
            )
        return None

    @staticmethod
    def _rule_undisclosed(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 7. 重要財務指標未開示 / 算出不能判定（自己資本比率等の必須財務データ欠損）
        if row.get("equity_ratio") is None:
            return "重要指標未開示/算出不能", "自己資本比率等の財務諸表データ未開示または欠損"
        return None

    @classmethod
    def _rule_cash_burn(cls, row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 8. 致命的キャッシュ枯渇判定（営業CFマージン < -10%）。金融・保険セクターは構造上免除
        if str(row.get("sector", "Other")) in cls.CF_EXEMPT_SECTORS:
            return None
        operating_cf, sales = row.get("operating_cf"), row.get("sales")
        if operating_cf is None or sales is None or sales <= 0:
            return None
        cf_margin, limit = operating_cf / sales, th["min_op_cf_margin"]
        if cf_margin < limit:
            return (
                "致命的キャッシュ枯渇",
                f"営業CFマージン大幅赤字 ({cf_margin * 100.0:.1f}% < {limit * 100.0:.1f}%)",
            )
        return None

    @staticmethod
    def _rule_market(row: dict, th: dict) -> Optional[tuple[str, str]]:
        # 9. 対象外市場判定 (設定で target_markets が明示指定された場合のみ除外)
        target_markets = th["target_markets"]
        if target_markets is None:
            return None
        market = str(row.get("market", ""))
        if market not in target_markets:
            return (
                "対象外市場",
                f"指定対象市場 ({target_markets}) に含まれない市場 ({market})",
            )
        return None

    @classmethod
    def apply_filter(
        cls,
        df_candidates: pl.DataFrame,
        df_liquidity: Optional[pl.DataFrame] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """candidates に対し事前足切りを行い、(passed_df, rejected_df) を返すコンビニエンスメソッド。"""
        res = cls.evaluate(df_candidates, df_liquidity, config)
        return res.passed_df, res.rejected_df
