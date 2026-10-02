"""株価履歴の差分取得 (取得期間・当日足・調整のずれ) の検証"""

from datetime import date, datetime

import polars as pl

from src.fetcher.incremental import (
    adjustment_ratio,
    count_weekdays_after,
    drop_unsettled_today,
    has_adjustment_mismatch,
    merge_history,
    plan_period,
)
from src.utils import JST


def test_plan_period_covers_gap_plus_overlap_day():
    fri = date(2026, 9, 25)
    # 翌営業日 (月) の実行: 欠落 1 日 + 重なり 1 日
    assert plan_period(fri, date(2026, 9, 28)) == "2d"
    # 1 週間空いた (欠落 5 平日) → 6 日必要 → 10d
    assert plan_period(fri, date(2026, 10, 2)) == "10d"
    # 同じ日の再実行でも最終日を取り直す
    assert plan_period(fri, fri) == "2d"
    # 1 か月超の欠落は全期間
    assert plan_period(date(2026, 8, 1), date(2026, 10, 2)) == "1y"
    assert plan_period(None, fri) == "1y"


def test_count_weekdays_after():
    assert count_weekdays_after(date(2026, 9, 25), date(2026, 9, 28)) == 1  # 土日を除く
    assert count_weekdays_after(date(2026, 9, 28), date(2026, 9, 28)) == 0


def _hist(dates, closes):
    return pl.DataFrame(
        {"Date": [datetime.fromisoformat(d) for d in dates], "Close": closes}
    )


def test_drop_unsettled_today_before_settlement_only():
    df = _hist(["2026-10-01", "2026-10-02"], [100.0, 101.0])
    during = datetime(2026, 10, 2, 14, 0, tzinfo=JST)
    after = datetime(2026, 10, 2, 16, 30, tzinfo=JST)
    assert drop_unsettled_today(df, during).height == 1
    assert drop_unsettled_today(df, after).height == 2


def test_adjustment_mismatch_detects_split_and_ignores_identical():
    db = _hist(["2026-02-17", "2026-02-18"], [10900.0, 10935.0])
    same = _hist(["2026-02-18", "2026-02-19"], [10935.0, 10800.0])
    split = _hist(["2026-02-18", "2026-02-19"], [3645.0, 3537.0])  # 3:1 分割で前日も調整済み
    assert not has_adjustment_mismatch(db, same)
    assert has_adjustment_mismatch(db, split)
    assert abs(adjustment_ratio(db, split) - 3.0) < 0.01
    # 配当落ちの調整程度のずれ (約 1.5%) も検知する (全期間を取り直して調整を揃える)
    div = _hist(["2026-02-18"], [10935.0 / 1.015])
    assert has_adjustment_mismatch(db, div)
    # 0.5% 以内は同じ値とみなす
    assert not has_adjustment_mismatch(db, _hist(["2026-02-18"], [10935.0 * 1.002]))


def test_merge_history_prefers_new_values_and_sorts():
    db = _hist(["2026-10-01", "2026-10-02"], [100.0, 0.0])
    new = _hist(["2026-10-02", "2026-10-05"], [102.0, 103.0])
    merged = merge_history(db, new)
    assert merged["Close"].to_list() == [100.0, 102.0, 103.0]


def _series(start, closes, volumes=None):
    from datetime import timedelta

    days, d = [], date.fromisoformat(start)
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(datetime(d.year, d.month, d.day))
        d += timedelta(days=1)
    data = {"Date": days, "Close": closes}
    if volumes is not None:
        data["Volume"] = volumes
    return pl.DataFrame(data)


def test_split_adjustment_finds_jump_before_event_date_and_is_idempotent():
    from src.fetcher.incremental import apply_split_adjustments

    # 8227 の実例: 段差は 2/18、Yahoo のイベント日は 2/19 (3:1)
    df = _series("2026-02-12", [10845.9, 10816.5, 10797.0, 10806.8, 3634.9, 3585.1, 3495.2], [100] * 7)
    adj = apply_split_adjustments(df, [(date(2026, 2, 19), 3.0)])
    closes = adj["Close"].to_list()
    assert abs(closes[3] - 10806.8 / 3) < 1e-6 and closes[4] == 3634.9
    assert adj["Volume"].to_list()[:4] == [300.0] * 4 and adj["Volume"][4] == 100.0
    # 2 回目は段差が無いため変わらない
    assert apply_split_adjustments(adj, [(date(2026, 2, 19), 3.0)])["Close"].to_list() == closes


def test_split_adjustment_ignores_event_without_matching_jump():
    from src.fetcher.incremental import apply_split_adjustments

    # 7946 の実例: イベント日 3/5 の 3 営業日前 (3/2) に 5:1 の段差
    df = _series("2026-02-25", [3216.0, 3781.4, 3673.3, 733.7, 664.8, 657.0, 658.9])
    adj = apply_split_adjustments(df, [(date(2026, 3, 5), 5.0)])
    assert abs(adj["Close"][2] - 3673.3 / 5) < 1e-6 and adj["Close"][3] == 733.7
    # 比率に合う段差が無い (既に調整済みの履歴) なら何もしない
    flat = _series("2026-02-25", [700.0, 710.0, 705.0, 733.7, 664.8])
    assert apply_split_adjustments(flat, [(date(2026, 3, 5), 5.0)])["Close"].to_list() == flat["Close"].to_list()


def test_market_fetcher_extracts_split_events_and_drops_action_columns():
    import pandas as pd

    from src.fetcher.market_fetcher import MarketFetcher

    idx = pd.to_datetime(["2026-02-17", "2026-02-18", "2026-02-19"])
    cols = pd.MultiIndex.from_product([["8227.T"], ["Close", "Volume", "Dividends", "Stock Splits"]])
    raw = pd.DataFrame(
        [[10806.8, 1, 0.0, 2.27e-07], [3634.9, 1, 0.0, 0.0], [3585.1, 1, 0.0, 3.0]], index=idx, columns=cols
    )
    mf = MarketFetcher({})
    out = mf._extract_dfs_from_batch(raw, ["8227.T"])
    assert list(out["8227"].columns) == ["Close", "Volume"]
    assert mf.pop_detected_splits() == {"8227": [(date(2026, 2, 19), 3.0)]}
    assert mf.pop_detected_splits() == {}


def test_implausible_split_events_are_ignored():
    from src.fetcher.incremental import apply_split_adjustments, find_split_jump

    # Yahoo が上場廃止前後に返す異常なイベント (比率 2e-07)。株価は変えない
    df = _series("2026-09-08", [1000.0, 1001.0, 1002.0, 1003.0, 1004.0])
    assert find_split_jump(df, date(2026, 9, 14), 2.2727e-07) is None
    assert apply_split_adjustments(df, [(date(2026, 9, 14), 2.2727e-07)])["Close"].to_list() == df["Close"].to_list()
    # 段差の無い 2:1 イベントも採用しない
    assert find_split_jump(df, date(2026, 9, 10), 2.0) is None


def test_infer_unrecorded_splits_from_integer_jumps():
    from src.fetcher.incremental import apply_split_adjustments, infer_unrecorded_splits

    # 8377 の実例: イベント無しで 7870 → 806 (約 10:1、休日を挟み 7 日)
    df = pl.DataFrame(
        {
            "Date": [datetime(2026, 9, 17), datetime(2026, 9, 18), datetime(2026, 9, 25), datetime(2026, 9, 26)],
            "Close": [7625.1, 7870.2, 806.3, 804.4],
        }
    )
    found = infer_unrecorded_splits(df)
    assert found == [(date(2026, 9, 25), 10.0)]
    adj = apply_split_adjustments(df, found)
    assert abs(adj["Close"][1] - 787.02) < 0.01
    assert infer_unrecorded_splits(adj) == []  # 調整後は段差が無い

    # 併合 (1:2) 相当の上方向の段差
    up = pl.DataFrame({"Date": [datetime(2026, 2, 12), datetime(2026, 2, 13)], "Close": [328.0, 648.0]})
    assert infer_unrecorded_splits(up) == [(date(2026, 2, 13), 0.5)]


def test_infer_ignores_market_moves_and_glitches():
    from src.fetcher.incremental import infer_unrecorded_splits

    # 値幅制限内の大きな動き、整数比から遠い段差 (5537: 7.55 倍)、桁違いのデータ不良は対象外
    df = pl.DataFrame(
        {
            "Date": [datetime(2026, 1, d) for d in (5, 6, 7, 8, 9)],
            "Close": [1000.0, 700.0, 264.75 * 2.644, 264.75, 53637025792.0],
        }
    )
    assert infer_unrecorded_splits(df) == []
