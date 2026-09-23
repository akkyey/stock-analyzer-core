from logging import getLogger
from typing import Any, List

import pandas as pd
import polars as pl

logger = getLogger(__name__)


class PolarsProcessor:
    """Polarsを使用した高速テクニカル指標算出クラス。

    [v23.1] ベクトル演算による高速化と Bollinger Bands 追加。
    [v27.0] 営業日Spine結合によるカレンダー・パディング (#28) およびキャッシュを搭載。
    """

    @staticmethod
    def clean_anomalous_prices(df: pl.DataFrame | pd.DataFrame) -> pl.DataFrame:
        """[Data Anomaly Checkout] 異常株価（<=0）、負の出来高、株式分割未考慮の異常スパイクを自動検知・排除する。"""
        if isinstance(df, pd.DataFrame):
            df_pl = pl.from_pandas(df)
        else:
            df_pl = df

        if df_pl.is_empty():
            return df_pl

        price_col = (
            "price"
            if "price" in df_pl.columns
            else ("Close" if "Close" in df_pl.columns else None)
        )
        if not price_col:
            return df_pl

        # 1. ゼロ以下の価格・負の出来高を Null に置換
        cleaned = df_pl.with_columns(
            pl.when(pl.col(price_col) <= 0)
            .then(None)
            .otherwise(pl.col(price_col))
            .alias(price_col)
        )
        if "Volume" in cleaned.columns:
            cleaned = cleaned.with_columns(
                pl.when(pl.col("Volume") < 0)
                .then(0)
                .otherwise(pl.col("Volume"))
                .alias("Volume")
            )
        if "volume" in cleaned.columns:
            cleaned = cleaned.with_columns(
                pl.when(pl.col("volume") < 0)
                .then(0)
                .otherwise(pl.col("volume"))
                .alias("volume")
            )

        # 2. 前日比異常スパイク (5倍超または 0.2倍未満の異常跳ね上がり・分割漏れ) のチェックアウト
        if "code" in cleaned.columns:
            prev_price = pl.col(price_col).shift(1).over("code")
            ratio = pl.col(price_col) / prev_price
            cleaned = cleaned.with_columns(
                pl.when((ratio > 5.0) | (ratio < 0.2))
                .then(None)
                .otherwise(pl.col(price_col))
                .alias(price_col)
            )

        return cleaned

    @staticmethod
    def pad_calendar(
        df: pl.DataFrame, start_date: Any = None, end_date: Any = None
    ) -> pl.DataFrame:
        """指定された期間の営業日Spineを作成し、データを補完（ffill）する。"""
        if df.is_empty():
            return df

        try:
            import pandas_market_calendars as mcal

            if start_date is None:
                start_date = df.select(pl.col("entry_date").min()).to_series()[0]
            if end_date is None:
                end_date = df.select(pl.col("entry_date").max()).to_series()[0]

            s_date = str(start_date)
            e_date = str(end_date)

            # 1. カレンダー・キャッシュの参照
            from src.repositories.market_data_repository import MarketDataRepository

            repo = MarketDataRepository()
            calendar_dates = repo.get_calendar_dates(s_date, e_date)

            if not calendar_dates:
                # キャッシュミス：外部APIから取得
                logger.info(
                    f"Calendar cache miss for {s_date} to {e_date}. Fetching from mcal..."
                )
                tse = mcal.get_calendar("JPX")
                schedule = tse.schedule(start_date=start_date, end_date=end_date)
                calendar_dates = schedule.index.normalize().date.tolist()
                # キャッシュ保存
                repo.upsert_calendar_dates(calendar_dates)

            trading_days = pl.DataFrame(
                {"entry_date": pl.Series(calendar_dates, dtype=pl.Date)}
            )

            # 2. 全銘柄コードの取得
            codes = df.select("code").unique()

            # 3. Spine (code x trading_days) の生成とデータ結合
            spine = trading_days.join(codes, how="cross")
            df_padded = (
                spine.join(df, on=["entry_date", "code"], how="left")
                .sort(["code", "entry_date"])
                .with_columns(
                    [
                        pl.all()
                        .exclude(["entry_date", "code"])
                        .forward_fill()
                        .over("code")
                    ]
                )
            )
            return df_padded
        except Exception as e:
            logger.warning(f"⚠️ Calendar padding failed, returning original: {e}")
            return df

    @staticmethod
    def _get_stability_count_expr(col: str = "price", window: int = 30) -> pl.Expr:
        """指定されたカラムの有効データ数（非NULL）をカウントするエクスプレッションを返す。"""
        return pl.col(col).is_not_null().cast(pl.Int32).rolling_sum(window).over("code")

    @staticmethod
    def _get_bb_expr(window: int = 25) -> List[pl.Expr]:
        """Bollinger Bands の計算エクスプレッション群を返す。"""
        return [
            pl.col("price").rolling_mean(window).over("code").alias("ma25"),
            pl.col("price").rolling_mean(75).over("code").alias("ma75"),
            pl.col("price").rolling_std(window).over("code").alias("std25"),
        ]

    @staticmethod
    def _get_rsi_expr(window: int = 14, stability_min: int = 30) -> List[pl.Expr]:
        """Wilder's Smoothing に基づく RSI 計算エクスプレッション群を返す。"""
        # 1. 差分と Gain/Loss
        diff = pl.col("price").diff().over("code")
        gain = pl.when(diff > 0).then(diff).otherwise(0.0)
        loss = pl.when(diff < 0).then(diff.abs()).otherwise(0.0)

        # 2. 有効カウント (v26.7 Fix: 明示的なキャスト)
        valid_count = (
            pl.col("price")
            .is_not_null()
            .cast(pl.Int32)
            .rolling_sum(stability_min)
            .over("code")
        )

        # 3. Wilder's Smoothing (EWMA)
        avg_gain = (
            gain.fill_null(0.0).ewm_mean(com=window - 1, adjust=False).over("code")
        )
        avg_loss = (
            loss.fill_null(0.0).ewm_mean(com=window - 1, adjust=False).over("code")
        )

        # 4. RSI 合成
        rsi = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

        # 特殊ケース（loss=0 の場合）および安定性判定の適用
        return [
            pl.when(valid_count.fill_null(0) < stability_min)
            .then(None)
            .when(avg_loss == 0)
            .then(pl.when(avg_gain == 0).then(50.0).otherwise(100.0))
            .otherwise(rsi)
            .alias(f"rsi_{window}")
        ]

    @staticmethod
    def _get_macd_expr() -> List[pl.Expr]:
        """MACD (12, 26, 9) の計算エクスプレッション群を返す。"""
        ema12 = pl.col("price").ewm_mean(span=12, adjust=False).over("code")
        ema26 = pl.col("price").ewm_mean(span=26, adjust=False).over("code")
        macd_expr = ema12 - ema26
        signal_expr = macd_expr.ewm_mean(span=9, adjust=False).over("code")

        return [
            macd_expr.alias("macd"),
            signal_expr.alias("macd_signal"),
            (macd_expr - signal_expr).alias("macd_hist"),
        ]

    @staticmethod
    def calc_batch_technicals_vectorized(
        hist_map: dict[str, pd.DataFrame],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """複数の銘柄データを一括で Polars 演算し、最新レコードの集合を返す。
        [v26.4] Group-by 演算により Python ループを排除し、ShapeError 対策を完備。
        - [x] **Task 1: DuckDBRepository の基盤化 (Structural Hardening)**
        - [x] `_upsert_dataframe` 共通テンプレートメソッドの実装
        - [x] `save_stocks` のテンプレート移行
        - [x] `save_metrics` のテンプレート移行
        - [x] `save_fundamentals` のテンプレート移行
        - [x] `save_analysis_results` のテンプレート移行
        - [x] **Task 2: PolarsProcessor のデータフロー刷新 (V6: Flow Refinement)**
        - [x] `calc_from_polars` 内の早期リターンを `df_pl.clear()` に修正
        - [x] 休日排除（Holiday Purge）ロジックの末尾移動
        - [x] 全指標計算（SSOT）とスコアリングの統合適用
        """
        if not hist_map:
            return pl.DataFrame()

        try:
            # 1. 全銘柄を一つの Polars DataFrame に結合
            df_parts = []
            for code, df_in in hist_map.items():
                # Pandas/Polars 両方に対応
                if isinstance(df_in, pd.DataFrame):
                    if df_in.empty:
                        continue
                    part = pl.from_pandas(df_in.reset_index())
                elif isinstance(df_in, pl.DataFrame):
                    if df_in.is_empty():
                        continue
                    part = df_in
                else:
                    continue

                # [v26.5] Core Fix: Ensure consistent Datetime precision (ns vs us) before concat
                for dt_col in ["Date", "index", "entry_date"]:
                    if dt_col in part.columns:
                        if part.schema[dt_col] == pl.Utf8:
                            part = part.with_columns(
                                pl.col(dt_col).str.to_datetime(strict=False)
                            )
                        try:
                            # Use strict=False to handle slightly malformed strings gracefully
                            part = part.with_columns(
                                pl.col(dt_col).cast(pl.Datetime("ns"), strict=False)
                            )
                        except Exception:
                            part = part.with_columns(
                                pl.col(dt_col).cast(pl.Datetime("us"), strict=False)
                            )

                if "code" not in part.columns:
                    raise ValueError("Column 'code' is missing")

                # [v26.4] Ensure consistent types before concat
                if "Volume" in part.columns:
                    part = part.with_columns(
                        [pl.col("Volume").cast(pl.Int64, strict=False).fill_null(0)]
                    )
                part = part.with_columns([pl.lit(code).alias("code")])
                df_parts.append(part)

            if not df_parts:
                return pl.DataFrame()

            # 結合（列が不足している場合に備えて diagonal を使用）
            df_pl = pl.concat(df_parts, how="diagonal")
            df_pl = PolarsProcessor.clean_anomalous_prices(df_pl)

            return PolarsProcessor.calc_from_polars(df_pl, latest_only)

        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Polars batch preparation failed: {e}")
            return pl.DataFrame()

    # [v26.9] Technical/Output SSOT Schema for daily_metrics
    # プロセッサが保証する範囲を「テクニカル指標」と「実行メタデータ」に限定。
    # 財務項目 (per, roe等) は接合フェーズ (EvaluationPhase) で注入される。
    SSOT_SCHEMA = {
        "code": pl.Utf8,
        "entry_date": pl.Date,
        "price": pl.Float64,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "volume": pl.Int64,
        "adj_close": pl.Float64,
        "volume_ratio": pl.Float64,
        "trading_value": pl.Float64,
        "macd": pl.Float64,
        "macd_signal": pl.Float64,
        "macd_hist": pl.Float64,
        "rsi_14": pl.Float64,
        "ma_divergence": pl.Float64,
        "volatility": pl.Float64,
        "ma25": pl.Float64,
        "ma75": pl.Float64,
        "bb_mid": pl.Float64,
        "bb_sigma": pl.Float64,
        "bb_p1sig": pl.Float64,
        "bb_p2sig": pl.Float64,
        "bb_m1sig": pl.Float64,
        "bb_m2sig": pl.Float64,
        "quant_score": pl.Float64,
        "trend_score": pl.Int32,
        "trend_signal": pl.Int32,
        "trend_up": pl.Float64,
        "fetch_status": pl.Utf8,
        "repair_metadata": pl.Utf8,
    }

    @staticmethod
    def _ensure_resilient_schema(df: pl.DataFrame) -> pl.DataFrame:
        """どのような状態の DataFrame でも、SSOT スキーマに従った構造を保証して返す。"""
        # 1. 存在しないカラムを Null で追加
        missing_cols = [
            pl.lit(None).cast(dtype).alias(col)
            for col, dtype in PolarsProcessor.SSOT_SCHEMA.items()
            if col not in df.columns
        ]
        if missing_cols:
            df = df.with_columns(missing_cols)

        # 2. カラムを SSOT 定義に従った順序と型に固定
        # select() を使用することで、余分な中間カラム (_tmp_* 等) も一括で削除される
        return df.select(
            [
                pl.col(c).cast(PolarsProcessor.SSOT_SCHEMA[c])
                for c in PolarsProcessor.SSOT_SCHEMA.keys()
            ]
        )

    @staticmethod
    def calc_from_polars(df_pl: pl.DataFrame, latest_only: bool = True) -> pl.DataFrame:
        """code カラムが構築済みの Polars DataFrame を直接受け取り、ベクトル演算を行う。"""
        try:
            if df_pl.is_empty():
                return PolarsProcessor._ensure_resilient_schema(df_pl.clear())

            if "code" not in df_pl.columns:
                raise ValueError("DataFrame must contain 'code' column.")

            # [v26.8] 名寄せ・正規化の早期実施 (Early Sanitization)
            # 以降、Close と price 等の表記揺れによる脆さを排除する
            df_pl = df_pl.rename(
                {
                    c: "price"
                    for c in ["Close", "price", "Price"]
                    if c in df_pl.columns and c != "price"
                }
            )

            # 日付の正規化 (entry_date への統合)
            date_col = next(
                (c for c in ["entry_date", "Date", "index"] if c in df_pl.columns), None
            )
            if date_col:
                df_pl = df_pl.with_columns(
                    [
                        pl.col(date_col)
                        .cast(pl.Utf8)
                        .str.slice(0, 10)
                        .str.to_date(strict=False)
                        .alias("entry_date")
                    ]
                )
            else:
                from datetime import date

                df_pl = df_pl.with_columns([pl.lit(date.today()).alias("entry_date")])

            # 2. テクニカル指標の計算
            # 指標計算に必要な最低限の行に絞る
            df_pl = df_pl.filter(
                (pl.col("price").is_not_null()) & (pl.col("price") > 0)
            )
            if df_pl.is_empty():
                return PolarsProcessor._ensure_resilient_schema(df_pl.clear())

            # [SSOT] 基層指標の算出
            df_pl = (
                df_pl.sort(["code", "entry_date"])
                .with_columns(PolarsProcessor._get_bb_expr(window=25))
                .with_columns(
                    [
                        pl.col("ma25").alias("bb_mid"),
                        pl.col("std25").alias("bb_sigma"),
                        (pl.col("ma25") + pl.col("std25")).alias("bb_p1sig"),
                        (pl.col("ma25") + 2 * pl.col("std25")).alias("bb_p2sig"),
                        (pl.col("ma25") - pl.col("std25")).alias("bb_m1sig"),
                        (pl.col("ma25") - 2 * pl.col("std25")).alias("bb_m2sig"),
                    ]
                )
                .with_columns(
                    PolarsProcessor._get_rsi_expr(window=14, stability_min=30)
                )
                .with_columns(PolarsProcessor._get_macd_expr())
            )

            # トレンドスコア等の派生指標
            df_pl = df_pl.with_columns(
                [
                    (((pl.col("price") - pl.col("ma25")) / pl.col("ma25")) * 100).alias(
                        "ma_divergence"
                    ),
                    (
                        (pl.col("ma25") > pl.col("ma75"))
                        .fill_null(False)
                        .cast(pl.Int32)
                        + (pl.col("price") > pl.col("ma25"))
                        .fill_null(False)
                        .cast(pl.Int32)
                        + (pl.col("macd_hist") > 0).fill_null(False).cast(pl.Int32)
                        + (pl.col("rsi_14") > 50).fill_null(False).cast(pl.Int32)
                    ).alias("trend_score"),
                ]
            ).with_columns(
                [
                    pl.col("trend_score").alias("trend_signal"),
                    (pl.col("trend_score") >= 3).cast(pl.Float64).alias("trend_up"),
                ]
            )

            # [v26.8] Holiday Guard: 計算の最後に営業日以外を排除
            df_pl = df_pl.filter(
                (pl.col("entry_date").is_not_null())
                & (pl.col("entry_date").dt.weekday() <= 5)
            )

            # 最新レコードのみか、全履歴かを切り替え
            if latest_only:
                df_pl = df_pl.group_by("code").last()

            # 最終的なスキーマ強制
            return PolarsProcessor._ensure_resilient_schema(df_pl)

        except Exception as e:
            logger.error(f"⚠️ Polars calculation failed: {e}")
            # エラー時も構造だけは正しい空の DataFrame を返して後続を助ける
            return PolarsProcessor._ensure_resilient_schema(
                pl.DataFrame(schema=PolarsProcessor.SSOT_SCHEMA)
            )

    @staticmethod
    def calc_technicals_vectorized(df_pandas: pd.DataFrame) -> dict[str, Any]:
        """[Deprecated] 単一銘グラフ用の古いメソッド。ユニットテスト維持のために Batch版 をラップして提供。"""
        if df_pandas.empty:
            return {}
        # Batch 版を再利用。コードを "_legacy_wrapper" として固定
        res_df = PolarsProcessor.calc_batch_technicals_vectorized(
            {"_legacy_wrapper": df_pandas}
        )
        if res_df.is_empty():
            return {}
        # 1行分を辞書で返す (既存テスト互換)
        return res_df.to_dicts()[0]
