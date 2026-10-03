"""株価履歴の差分取得を支える純粋関数群

差分取得 (DB に保存済みの履歴 + Yahoo Finance の直近分) で、初回の全期間取得と
同じ履歴を再現するための判定をまとめる。

- 取得期間: DB の最終日から今日までの欠落営業日数に合わせて決める
  (固定の 2 日では、実行間隔が空いた分の日付が永久に欠落する)
- 当日足: 取引終了直後は値が確定していないため採用しない
- 調整のずれ: yfinance は配当落ちのたびに過去の値を遡って調整し直すため、
  DB の保存値と重なり日の終値が食い違ったら、その銘柄は全期間を取り直す
- 株式分割: Yahoo Finance は日本株の分割を、銘柄によっては過去に遡って調整しない。
  分割イベントを返さない銘柄や、異常な比率のイベントもある。株価に段差が残っている分割だけを
  自前で調整し、イベントの無い分割は値幅制限を超える整数比の段差から推定する
  (2026-10 時点で確認した実例は、設計書 colab_execution_architecture_and_guide_design.md 3.3.2)
"""

import math
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

import polars as pl

# 差分取得で使う期間 (取引日数)。Yahoo は任意の "Nd" を受け付ける。
# バッチの分割数を抑えるため、必要日数をこの段階に切り上げる
INCREMENTAL_PERIODS = (2, 5, 10, 21)
# これを超える欠落は差分ではなく全期間を取り直す
FULL_PERIOD = "1y"
# 調整のずれ (分割・配当落ち) で全期間を取り直すときの期間。
# DB から読む履歴 (13 か月) を覆えるだけの長さにする
REFETCH_PERIOD = "2y"

# 東証の大引けは 15:30。Yahoo の日足が確定するまでの余裕をみて 16:00 以降に当日足を採用する
SESSION_SETTLED_AT = time(16, 0)

# 重なり日の終値の食い違いがこれを超えたら、調整のずれとみなす (0.5%)。
# 分割は数十 % 以上、配当落ちの調整は概ね 0.5〜3% の差になる
ADJUSTMENT_TOLERANCE = 0.005


def count_weekdays_after(last: date, today: date) -> int:
    """last の翌日から today までの平日数 (祝日は数えてしまうため、必要日数の上限になる)。"""
    if today <= last:
        return 0
    n = 0
    d = last + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def plan_period(last: Optional[date], today: date) -> str:
    """DB の最終日 last から、差分取得に使う期間を決める。

    最終日そのものも取り直す (重なり日として調整のずれの判定に使う) ため、
    必要な取引日数は「欠落した平日数 + 1」。
    """
    if last is None:
        return FULL_PERIOD
    needed = count_weekdays_after(last, today) + 1
    for n in INCREMENTAL_PERIODS:
        if needed <= n:
            return f"{n}d"
    return FULL_PERIOD


def drop_unsettled_today(df: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """取引時間中・終了直後に取得した当日足 (値が確定していない) を除く。

    Args:
        df: "Date" 列を持つ履歴
        now: 現在時刻 (JST)
    """
    if df.is_empty() or "Date" not in df.columns:
        return df
    if now.time() >= SESSION_SETTLED_AT:
        return df
    return df.filter(pl.col("Date").cast(pl.Date) != now.date())


def adjustment_ratio(df_db: pl.DataFrame, df_new: pl.DataFrame) -> Optional[float]:
    """重なる日付の終値について、DB の値 / 新しい値 のうち 1 から最も離れたものを返す。

    重なる日付が無ければ None。
    """
    if df_db.is_empty() or df_new.is_empty():
        return None
    left = df_db.select(
        pl.col("Date").cast(pl.Date).alias("d"), pl.col("Close").alias("db")
    )
    right = df_new.select(
        pl.col("Date").cast(pl.Date).alias("d"), pl.col("Close").alias("new")
    )
    both = left.join(right, on="d", how="inner").filter(
        pl.col("db").is_not_null()
        & pl.col("new").is_not_null()
        & (pl.col("db") > 0)
        & (pl.col("new") > 0)
    )
    if both.is_empty():
        return None
    ratios = both.select((pl.col("db") / pl.col("new")).alias("r"))["r"]
    val = max(ratios.to_list(), key=lambda r: abs(float(r) - 1.0))
    return float(val) if val is not None else None


# Yahoo の分割イベントの日付は、実際に株価が切り替わった日 (権利落ち日) より 1〜8 日ほど
# 後になる。この範囲で段差を探す
SPLIT_SEARCH_BEFORE = timedelta(days=14)
SPLIT_SEARCH_AFTER = timedelta(days=5)

# Yahoo が分割イベントを返さない銘柄もある。前日比が
# 1.9 倍以上 (または 1/1.9 以下) で、変化額が東証の値幅制限を超え (値動きではありえない)、
# かつ整数比 (2・3・5・10 倍等) から INFERRED_SPLIT_TOLERANCE 以内の段差は分割 (併合) とみなす
INFERRED_SPLIT_MIN_RATIO = 1.9
INFERRED_SPLIT_TOLERANCE = 0.05
INFERRED_SPLIT_MAX_GAP = timedelta(days=7)
# 前日比が分割比率からこの割合 (対数) 以内なら、分割による段差とみなす。
# 広すぎると、調整後の履歴に残った実際の大きな値動きまで段差と誤認し、実行のたびに
# 調整が重なる。分割日の値動きを見込んで 12% とする
SPLIT_JUMP_TOLERANCE = 0.12

# 東証の値幅制限 (基準値段の未満 → 制限値幅、円)。1 営業日にこれを超えて動くことはないため、
# 前日比の変化額がこれを超える段差だけを、分割 (併合) による段差とみなす。
# 100 円未満の銘柄は制限値幅が 30 円で、1 日に 2 倍近く動き得る
_PRICE_LIMITS = (
    (100, 30), (200, 50), (500, 80), (700, 100), (1_000, 150), (1_500, 300),
    (2_000, 400), (3_000, 500), (5_000, 700), (7_000, 1_000), (10_000, 1_500),
    (15_000, 3_000), (20_000, 4_000), (30_000, 5_000), (50_000, 7_000),
    (70_000, 10_000), (100_000, 15_000), (150_000, 30_000), (200_000, 40_000),
    (300_000, 50_000), (500_000, 70_000), (700_000, 100_000), (1_000_000, 150_000),
)


def daily_price_limit(price: float) -> float:
    """基準値段に対する、東証の 1 日の制限値幅 (円)。"""
    for upper, limit in _PRICE_LIMITS:
        if price < upper:
            return float(limit)
    return float(price) * 0.3


# 制限値幅の 80% 以上動いた日を、「制限値幅まで動いた日」とみなす割合
LIMIT_HIT_RATIO = 0.8


def exceeds_daily_limit(prev: float, cur: float) -> bool:
    """前日終値から当日終値への変化額が、制限値幅を超えているか (値動きではありえない段差か)。"""
    return abs(cur - prev) > daily_price_limit(prev)


def _hit_limit(closes: list, j: int) -> int:
    """j 日目に制限値幅まで動いたか。上げなら +1、下げなら -1、そうでなければ 0。"""
    if j < 1:
        return 0
    prev, cur = closes[j - 1], closes[j]
    if not prev or not cur or prev <= 0 or cur <= 0:
        return 0
    if abs(cur - prev) < LIMIT_HIT_RATIO * daily_price_limit(prev):
        return 0
    return 1 if cur > prev else -1


def limit_may_be_widened(closes: list, i: int) -> bool:
    """i 日目の制限値幅が、拡大されている可能性があるか。

    東証は、2 営業日連続でストップ高 (安) となり売買が成立しなかった銘柄の制限値幅を、
    翌営業日から拡大する (通常の 2 倍など)。直前の 2 日が続けて同じ向きに制限値幅まで
    動いていれば、当日の変化額が通常の制限値幅を超えても、値動きでありうる (急騰・急落を
    併合と誤認しないため)。出来高は見ていない (取得できない場合があるため近似)。
    """
    if i < 3:
        return False
    d1, d2 = _hit_limit(closes, i - 1), _hit_limit(closes, i - 2)
    return d1 != 0 and d1 == d2


_PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close")

# 分割比率として現実的な範囲。Yahoo は上場廃止前後などに 2e-07 のような異常な分割イベントを
# 返すことがある
SPLIT_RATIO_RANGE = (0.02, 100.0)


def is_plausible_split_ratio(ratio: Optional[float]) -> bool:
    return (
        ratio is not None
        and SPLIT_RATIO_RANGE[0] <= ratio <= SPLIT_RATIO_RANGE[1]
        and ratio != 1
    )


def _is_impossible_jump(closes: list, i: int) -> bool:
    """i 日目の前日比が、値動きではありえない段差か (変化額が制限値幅を超え、拡大でもない)。"""
    prev, cur = closes[i - 1], closes[i]
    if not prev or not cur or prev <= 0 or cur <= 0:
        return False
    return exceeds_daily_limit(prev, cur) and not limit_may_be_widened(closes, i)


def find_split_jump(df: pl.DataFrame, split_day: date, ratio: float) -> Optional[date]:
    """分割イベントの日付の近くで、前日比が分割比率に最も近い日 (段差の日) を返す。

    比率に合う段差が無ければ None (調整済みの履歴、該当期間のデータが無い、異常なイベント)。
    """
    if not is_plausible_split_ratio(ratio) or df.is_empty():
        return None
    # 比率が 1 に近い分割 (1:1.05 など) は、日々の値動きと見分けがつかない。
    # 株価の調整は、値幅制限ではありえない大きさ (INFERRED_SPLIT_MIN_RATIO 以上) に限る。
    # (許容幅 SPLIT_JUMP_TOLERANCE が比率より広いと、どの日も「段差」になり、
    #  実行のたびに履歴が割られ続ける)
    if abs(math.log(ratio)) < math.log(INFERRED_SPLIT_MIN_RATIO):
        return None
    out = df.sort("Date")
    days = out["Date"].cast(pl.Date).to_list()
    lo, hi = split_day - SPLIT_SEARCH_BEFORE, split_day + SPLIT_SEARCH_AFTER
    window = [i for i in range(1, len(days)) if days[i] is not None and lo <= days[i] <= hi]
    if not window:
        return None
    closes = out["Close"].to_list()
    best_i, best_err = None, None
    for i in window:
        if not _is_impossible_jump(closes, i):
            continue
        err = abs(math.log(closes[i - 1] / closes[i]) - math.log(ratio))
        if best_err is None or err < best_err:
            best_i, best_err = i, err
    if best_i is None or best_err is None or best_err > SPLIT_JUMP_TOLERANCE:
        return None
    res_day = days[best_i]
    return res_day if isinstance(res_day, date) else None


def infer_unrecorded_splits(df: pl.DataFrame) -> list[tuple[date, float]]:
    """分割イベントの無い、分割 (併合) とみなせる段差を探す。

    Returns:
        (段差の日, 比率) の列。3:1 分割相当なら比率 3.0、1:3 併合相当なら 1/3
    """
    if df.is_empty() or "Date" not in df.columns or "Close" not in df.columns:
        return []
    out = df.sort("Date")
    # 全銘柄・全履歴に毎回かかるため、前日比 INFERRED_SPLIT_MIN_RATIO 倍以上の行を先に絞る
    # (ほとんどの銘柄は候補が 0 件)
    prev = pl.col("Close").shift(1)
    candidates = (
        out.with_row_index("_i")
        .filter(
            (prev > 0)
            & (pl.col("Close") > 0)
            & (
                (prev / pl.col("Close") >= INFERRED_SPLIT_MIN_RATIO)
                | (pl.col("Close") / prev >= INFERRED_SPLIT_MIN_RATIO)
            )
        )["_i"]
        .to_list()
    )
    if not candidates:
        return []
    days = out["Date"].cast(pl.Date).to_list()
    closes = out["Close"].to_list()
    found: list[tuple[date, float]] = []
    for i in candidates:
        d0, d1 = days[i - 1], days[i]
        if d0 is None or d1 is None or d1 - d0 > INFERRED_SPLIT_MAX_GAP:
            continue
        r = closes[i - 1] / closes[i]
        x = r if r >= 1 else 1 / r
        n = round(x)
        if not is_plausible_split_ratio(float(n)) or abs(x / n - 1) > INFERRED_SPLIT_TOLERANCE:
            continue
        if not _is_impossible_jump(closes, i):
            continue
        found.append((d1, float(n) if r >= 1 else 1.0 / n))
    return found


def apply_split_adjustments(
    df: pl.DataFrame, splits: Iterable[tuple[date, float]]
) -> pl.DataFrame:
    """分割前の株価を分割後の基準に調整する (分割比率で割り、出来高に比率を掛ける)。

    分割イベントの日付の近くで、前日比が分割比率に最も近い日を段差の日とし、それより前の
    行を調整する。既に調整済み (段差が無い) 履歴には何もしないため、何度適用しても同じ結果になる。

    Args:
        df: "Date" と "Close" を持つ 1 銘柄の履歴
        splits: (分割イベントの日付, 比率) の列。3:1 分割なら比率 3.0、併合なら 1 未満
    """
    if df.is_empty() or "Date" not in df.columns or "Close" not in df.columns:
        return df
    out = df.sort("Date")
    for split_day, ratio in sorted(splits):
        jump_day = find_split_jump(out, split_day, ratio)
        if jump_day is None:
            continue  # 段差が無い (調整済み、または該当期間のデータが無い)
        before = pl.col("Date").cast(pl.Date) < jump_day
        exprs = [
            pl.when(before).then(pl.col(c) / ratio).otherwise(pl.col(c)).alias(c)
            for c in _PRICE_COLUMNS
            if c in out.columns
        ]
        if "Volume" in out.columns:
            exprs.append(
                pl.when(before)
                .then(pl.col("Volume").cast(pl.Float64) * ratio)
                .otherwise(pl.col("Volume").cast(pl.Float64))
                .alias("Volume")
            )
        out = out.with_columns(exprs)
    return out


def has_adjustment_mismatch(
    df_db: pl.DataFrame, df_new: pl.DataFrame, tolerance: float = ADJUSTMENT_TOLERANCE
) -> bool:
    """DB の保存値と新しく取得した値が、重なる日付の終値で食い違っていれば True。

    yfinance は分割・配当落ちのたびに過去の値を遡って調整するため、食い違いは
    「DB 側の過去の値が古い調整のまま」であることを示す。
    """
    ratio = adjustment_ratio(df_db, df_new)
    return ratio is not None and abs(ratio - 1.0) > tolerance


def merge_history(df_db: Optional[pl.DataFrame], df_new: pl.DataFrame) -> pl.DataFrame:
    """DB の履歴に新しく取得した分を重ねる (同じ日付は新しい値を採用)。"""
    if df_db is None or df_db.is_empty():
        return df_new
    db_norm = df_db
    if "entry_date" in db_norm.columns and "Date" not in db_norm.columns:
        db_norm = db_norm.rename({"entry_date": "Date"})
    if "Volume" in db_norm.columns:
        db_norm = db_norm.with_columns(pl.col("Volume").cast(pl.Float64))
    new_norm = df_new
    if "Volume" in new_norm.columns:
        new_norm = new_norm.with_columns(pl.col("Volume").cast(pl.Float64))
    return (
        pl.concat(
            [
                db_norm.with_columns(pl.col("Date").cast(pl.Datetime)),
                new_norm.with_columns(pl.col("Date").cast(pl.Datetime)),
            ],
            how="diagonal_relaxed",
        )
        .unique("Date", keep="last", maintain_order=True)
        .sort("Date")
    )


class SplitTracker:
    """株式分割の記録を持ち、銘柄ごとの株価履歴を調整する (取得フェーズで 1 回の実行に 1 つ)。

    - 記録済みの分割と、今回の取得で見つかった分割 (Yahoo のイベント) を合わせて持つ
    - 比率が範囲内のイベントは、株価に段差が無く (Yahoo が調整済み) ても記録する
      (1 株当たりの財務指標の補正に使う)
    - 株価の調整は、段差が残っている分割だけ。イベントの無い分割は段差から推定して記録する
    """

    # 同じ分割のイベントが日付をずらして重ねて届くことがあるため、この日数以内は同じ分割とみなす
    DEDUP_DAYS = 10

    def __init__(self, recorded: Optional[pl.DataFrame] = None):
        self.by_code: dict[str, list[tuple[date, float]]] = {}
        self.new: list[tuple[str, date, float]] = []
        if recorded is not None:
            for code, d, r in recorded.iter_rows():
                self.by_code.setdefault(str(code), []).append((d, r))

    def _record(self, code: str, d: date, r: float) -> None:
        self.by_code.setdefault(code, []).append((d, r))
        self.new.append((code, d, r))

    def add_events(self, found: dict[str, list[tuple[date, float]]]) -> None:
        """取得で見つかった分割イベントを加える (異常な比率と、既知の分割の重複は除く)。"""
        for code, events in found.items():
            for d, r in events:
                known = self.by_code.get(code, [])
                if is_plausible_split_ratio(r) and all(
                    abs((d - kd).days) > self.DEDUP_DAYS for kd, _ in known
                ):
                    self._record(code, d, r)

    def adjust(self, code: str, df: pl.DataFrame) -> pl.DataFrame:
        """株価に段差が残っている分割を調整し、イベントの無い分割を推定して記録・調整する。"""
        events = self.by_code.get(code)
        if events:
            df = apply_split_adjustments(df, events)
        inferred = infer_unrecorded_splits(df)
        for d, r in inferred:
            self._record(code, d, r)
        return apply_split_adjustments(df, inferred) if inferred else df
