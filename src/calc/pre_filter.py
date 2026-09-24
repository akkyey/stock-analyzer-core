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

from dataclasses import dataclass
from typing import Any, Optional

import polars as pl


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
        tv_col = pl.col("trading_value") if "trading_value" in df_timeseries.columns else pl.lit(None).cast(pl.Float64)
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
            .with_columns(
                pl.when(zero_cond)
                .then(1)
                .otherwise(0)
                .alias("_is_zero")
            )
            .group_by("code")
            .agg(pl.col("_is_zero").sum().alias("zero_volume_days_5d"))
        )

        # 各銘柄の直近データ日付（データ鮮度判定用: 市場全体の直近5営業日以内に取引があるか）
        market_recent_dates = [str(d) for d in all_dates[:5]]
        df_latest_trade = (
            df_calc.group_by("code")
            .agg(pl.col("entry_date").cast(pl.String).max().alias("latest_trade_date"))
            .with_columns(
                pl.col("latest_trade_date").is_in(market_recent_dates).alias("is_recent_trade")
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

        # 設定値の反映
        min_tv_20d = cls.MIN_TRADING_VALUE_20D
        min_price = cls.MIN_PRICE
        min_eq_ratio = cls.MIN_EQUITY_RATIO
        min_op_cf_margin = cls.MIN_OP_CF_MARGIN

        if config and "hard_filters" in config:
            hf = config["hard_filters"]
            if "min_trading_value" in hf:
                min_tv_20d = float(hf["min_trading_value"])

        # 流動性指標の結合
        df = df_candidates
        if df_liquidity is not None and not df_liquidity.is_empty():
            cols_to_drop = [
                c
                for c in [
                    "avg_trading_value_20d",
                    "zero_volume_days_5d",
                    "latest_trade_date",
                    "is_recent_trade",
                ]
                if c in df.columns
            ]
            if cols_to_drop:
                df = df.drop(cols_to_drop)
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

        passed_rows = []
        rejected_rows = []

        # Python辞書走査による厳格・明示的な理由付与
        records = df.to_dicts()
        for row in records:
            price = row.get("price")
            equity_ratio = row.get("equity_ratio")
            operating_cf = row.get("operating_cf")
            sales = row.get("sales")
            avg_tv = row.get("avg_trading_value_20d")
            zero_days = row.get("zero_volume_days_5d")
            latest_trade_date = row.get("latest_trade_date")
            is_recent_trade = row.get("is_recent_trade")
            sector = str(row.get("sector", "Other"))

            # 1. データ鮮度不足判定（最新市場日より大幅に古い取引停止銘柄）
            if is_recent_trade is False and latest_trade_date is not None:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "データ鮮度不足 (取引停止)",
                        "filter_detail": f"最終取引日 ({latest_trade_date}) が直近5営業日範囲外",
                    }
                )
                continue

            # 2. 売買不能判定（直近5営業日出来高ゼロ）
            if zero_days is not None and zero_days > 0:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "商い不成立",
                        "filter_detail": f"直近5営業日以内に出来高ゼロ日あり ({zero_days}日)",
                    }
                )
                continue

            # 3. 極小流動性トラップ判定（20日平均売買代金 < 3,000万円）
            if avg_tv is not None and avg_tv < min_tv_20d:
                tv_man = avg_tv / 10_000.0
                limit_man = min_tv_20d / 10_000.0
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "極小流動性トラップ",
                        "filter_detail": f"20日平均売買代金不足 ({tv_man:,.0f}万円 < {limit_man:,.0f}万円)",
                    }
                )
                continue

            # 4. 超低位ボロ株判定（株価 < 50円）
            if price is not None and price < min_price:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "超低位ボロ株",
                        "filter_detail": f"株価基準未満 ({price:,.0f}円 < {min_price:,.0f}円)",
                    }
                )
                continue

            # 5. 株価データ欠損判定（市場データ取得不能）
            if price is None:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "市場データ取得不能",
                        "filter_detail": "市場価格データ欠損 (OHLCV未取得)",
                    }
                )
                continue

            # 6. 重要財務指標未開示 / 算出不能判定（自己資本比率等の必須財務データ欠損）
            if equity_ratio is None:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "重要指標未開示/算出不能",
                        "filter_detail": "自己資本比率等の財務諸表データ未開示または欠損",
                    }
                )
                continue

            # 7. 構造的破綻（債務超過判定）
            if equity_ratio <= min_eq_ratio:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "構造的破綻 (債務超過)",
                        "filter_detail": f"純資産マイナス / 自己資本比率 ({equity_ratio:.1f}% <= {min_eq_ratio:.1f}%)",
                    }
                )
                continue

            # 8. 致命的キャッシュ枯渇判定（営業CFマージン < -10%）
            # 金融・保険セクターは構造上免除
            if sector not in cls.CF_EXEMPT_SECTORS:
                if operating_cf is not None and sales is not None and sales > 0:
                    cf_margin = operating_cf / sales
                    if cf_margin < min_op_cf_margin:
                        margin_pct = cf_margin * 100.0
                        limit_pct = min_op_cf_margin * 100.0
                        rejected_rows.append(
                            {
                                **row,
                                "filter_reason": "致命的キャッシュ枯渇",
                                "filter_detail": f"営業CFマージン大幅赤字 ({margin_pct:.1f}% < {limit_pct:.1f}%)",
                            }
                        )
                        continue

            # すべての足切りをクリアした銘柄
            passed_rows.append(row)

        if passed_rows:
            passed_codes = [r["code"] for r in passed_rows]
            passed_df = df.filter(pl.col("code").is_in(passed_codes))
        else:
            passed_df = df.clear()

        if rejected_rows:
            reason_df = pl.DataFrame(
                [
                    {
                        "code": r["code"],
                        "filter_reason": r["filter_reason"],
                        "filter_detail": r["filter_detail"],
                    }
                    for r in rejected_rows
                ],
                schema={
                    "code": pl.String,
                    "filter_reason": pl.String,
                    "filter_detail": pl.String,
                },
            )
            rejected_df = df.join(reason_df, on="code", how="inner")
        else:
            rejected_df = pl.DataFrame(
                schema={
                    **df.schema,
                    "filter_reason": pl.String,
                    "filter_detail": pl.String,
                }
            )

        return PreFilterResult(passed_df=passed_df, rejected_df=rejected_df)

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

