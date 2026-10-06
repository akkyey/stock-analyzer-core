"""銘柄マスタの月次更新 (JPX 同期) と、市場データ非提供銘柄の再取得抑制の検証"""

from datetime import date, timedelta

import pandas as pd
import polars as pl

from src.calc.pre_filter import PreFilter
from src.fetcher.jpx import JPXFetcher
from src.repositories.duck_repository import DuckDBRepository
from src.services import stock_master as sm

TODAY = date(2026, 10, 3)


def _repo_with(codes: list[str], **extra) -> DuckDBRepository:
    repo = DuckDBRepository()
    repo.save_stocks(
        pl.DataFrame(
            {
                "code": codes,
                "name": [f"社{c}" for c in codes],
                "sector": ["卸売業"] * len(codes),
                "market": ["Prime"] * len(codes),
                **extra,
            }
        )
    )
    return repo


def _live(codes: list[str], name_prefix="社") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "code": codes,
            "name": [f"{name_prefix}{c}" for c in codes],
            "sector": ["卸売業"] * len(codes),
            "market": ["Prime"] * len(codes),
        }
    )


def _row(repo, code):
    with repo.client.get_connection() as conn:
        r = conn.execute(
            "SELECT is_active, status, exclusion_reason, name FROM stocks WHERE code = ?",
            [code],
        ).fetchall()
    return r[0] if r else None


def _bulk(n=3100, start=1000):
    return [str(start + i) for i in range(n)]


# ---- JPX 一覧の正規化 -----------------------------------------------------------------


def test_normalize_jpx_frame():
    raw = pd.DataFrame(
        {
            "コード": ["13010", "130A0", "13010", "13050"],
            "銘柄名": ["極洋", "テスト", "極洋(優先)", "ETF"],
            "市場・商品区分": ["プライム（内国株式）", "グロース（内国株式）", "プライム（内国株式）", "ETF・ETN"],
            "33業種区分": ["水産・農林業", "情報・通信業", "水産・農林業", "-"],
        }
    )
    df = JPXFetcher.normalize_jpx_frame(raw)
    assert df["code"].tolist() == ["1301", "130A"]  # ETF 除外・4 桁化・重複除去
    assert df["market"].tolist() == ["Prime", "Growth"]
    assert list(df.columns) == ["code", "name", "sector", "market"]


# ---- 月次判定 -------------------------------------------------------------------------


def test_needs_refresh_monthly():
    repo = _repo_with(["1301"])
    assert sm.needs_refresh(repo, today=TODAY) is True  # 未実施
    repo.set_meta(sm.META_KEY, TODAY.isoformat())
    assert sm.needs_refresh(repo, today=TODAY + timedelta(days=29)) is False
    assert sm.needs_refresh(repo, today=TODAY + timedelta(days=30)) is True
    assert sm.needs_refresh(repo, today=TODAY, force=True) is True
    repo.set_meta(sm.META_KEY, "壊れた値")
    assert sm.needs_refresh(repo, today=TODAY) is True  # 不正値は再取得


# ---- 反映ロジック ---------------------------------------------------------------------


def test_apply_live_list_adds_delists_relists_and_updates():
    base = _bulk()
    repo = _repo_with(base + ["9001", "9002"])  # 9001/9002 は一覧から消える (廃止)
    # 事前: 9003 は過去に廃止扱い → 一覧に戻る (再上場)
    repo.save_stocks(_live(["9003"]))
    with repo.client.get_connection() as conn:
        conn.execute("UPDATE stocks SET is_active=FALSE, status='delisted' WHERE code='9003'")

    live = _live(base + ["9003", "542A"], name_prefix="新")  # 542A は新規上場
    res = sm.apply_live_list(repo, live, today=TODAY)

    assert res.status == "updated"
    assert (res.added, res.delisted, res.relisted) == (1, 2, 1)
    assert _row(repo, "542A")[0] is True  # 新規は有効
    gone = _row(repo, "9001")
    assert gone[0] is False and gone[1] == "delisted" and gone[2] == sm.DELISTED_REASON
    assert _row(repo, "9003")[:3] == (True, "active", None)  # 再上場で復活
    assert _row(repo, base[0])[3].startswith("新")  # 社名更新
    assert repo.get_meta(sm.META_KEY) == TODAY.isoformat()
    # 廃止はデータ削除ではなく無効化 (行は残る)
    assert _row(repo, "9002") is not None
    # 取得対象 (get_all_codes) から廃止銘柄は外れ、新規は入る
    codes = set(repo.get_all_codes())
    assert "9001" not in codes and "542A" in codes


def test_apply_live_list_safety_valves():
    repo = _repo_with(_bulk())
    # 不完全な一覧 (件数不足) は何もしない
    small = sm.apply_live_list(repo, _live(_bulk(100)), today=TODAY)
    assert small.status == "skipped_unsafe"
    assert repo.get_meta(sm.META_KEY) is None
    assert len(repo.get_all_codes()) == 3100

    # 廃止判定が多すぎる (一覧の異常) 場合も変更しない
    mostly_other = _live(_bulk(3100, start=5000))  # 現マスタとほぼ重ならない
    bad = sm.apply_live_list(repo, mostly_other, today=TODAY)
    assert bad.status == "skipped_unsafe"
    assert len(repo.get_all_codes()) == 3100
    assert sm.apply_live_list(repo, pl.DataFrame(), today=TODAY).status == "skipped_unsafe"


class _Fetcher:
    def __init__(self, df=None, error=None):
        self.df, self.error, self.calls = df, error, 0

    def download_live_list(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.df


def test_refresh_stock_master_skips_when_recent_and_never_raises():
    base = _bulk()
    repo = _repo_with(base)
    live = pd.DataFrame(_live(base).to_dicts())

    f = _Fetcher(live)
    assert sm.refresh_stock_master(repo, f, today=TODAY).status == "updated"
    assert f.calls == 1
    # 直後は取得しない
    f2 = _Fetcher(live)
    assert sm.refresh_stock_master(repo, f2, today=TODAY + timedelta(days=1)).status == "skipped_recent"
    assert f2.calls == 0
    # ダウンロード失敗は例外にせず、従来のマスタで継続
    f3 = _Fetcher(error=RuntimeError("network down"))
    res = sm.refresh_stock_master(repo, f3, today=TODAY + timedelta(days=40))
    assert res.status == "failed" and "network down" in res.detail
    assert len(repo.get_all_codes()) == 3100


# ---- 市場データ非提供銘柄の再取得抑制 ---------------------------------------------------------


def test_no_data_tracking_cooldown_and_recovery():
    repo = _repo_with(_bulk(50))
    codes = _bulk(50)
    dead = codes[:2]
    fetched_ok = set(codes[2:])  # 98% が取得成功 = 健全な実行

    # 1〜2 日目: 失敗回数のみ加算 (まだ停止しない)
    for day in range(2):
        d = TODAY + timedelta(days=day)
        s = sm.update_no_data_tracking(repo, fetched_ok, codes, codes_with_history=set(), today=d)
        assert s["failed"] == 2 and s["cooled"] == 0
    assert sm.get_cooling_codes(repo, today=TODAY + timedelta(days=1)) == set()

    # 3 日目: 30 日間の再取得停止
    third = TODAY + timedelta(days=2)
    s = sm.update_no_data_tracking(repo, fetched_ok, codes, codes_with_history=set(), today=third)
    assert s["cooled"] == 2
    assert sm.get_cooling_codes(repo, today=third) == set(dead)
    assert _row(repo, dead[0])[2] == sm.NO_DATA_REASON
    # 29 日後もまだ停止中、30 日後は再確認のため対象に戻る
    assert sm.get_cooling_codes(repo, today=third + timedelta(days=29)) == set(dead)
    assert sm.get_cooling_codes(repo, today=third + timedelta(days=30)) == set()

    # データが取れたら回復 (失敗回数・停止・理由をリセット)
    s = sm.update_no_data_tracking(repo, set(codes), codes, codes_with_history=set(), today=third)
    assert s["recovered"] == 2
    assert sm.get_cooling_codes(repo, today=third) == set()
    assert _row(repo, dead[0])[2] is None


def test_no_data_failures_are_counted_once_per_day():
    """同じ日に何回実行しても、失敗は 1 回と数える。一時的な取得失敗で、同じ日の 3 回の実行だけで、
    30 日間の再取得停止にならない (Colab の本番で、同じ日の 3 回の実行で、44 銘柄が停止になった)"""
    repo = _repo_with(_bulk(50))
    codes = _bulk(50)
    dead = codes[:2]
    fetched_ok = set(codes[2:])
    for _ in range(5):  # 同じ日に 5 回
        s = sm.update_no_data_tracking(repo, fetched_ok, codes, codes_with_history=set(), today=TODAY)
        assert s["cooled"] == 0
    assert sm.get_cooling_codes(repo, today=TODAY) == set()
    with repo.client.get_connection() as conn:
        counts = conn.execute(
            "SELECT fail_count, last_fail_date FROM stocks WHERE code = ?", [dead[0]]
        ).fetchone()
    assert counts == (1, TODAY.isoformat())
    # 翌日・翌々日にも失敗が続けば、3 日連続で停止する
    for day in (1, 2):
        s = sm.update_no_data_tracking(
            repo, fetched_ok, codes, codes_with_history=set(), today=TODAY + timedelta(days=day)
        )
    assert s["cooled"] == 2


def test_stocks_schema_migration_adds_last_fail_date(tmp_path):
    """旧い DB (last_fail_date など新しい列が無い stocks テーブル) でも、スキーマの更新で列が加わる"""
    import duckdb

    from src.database.duck_client import DuckDBClient
    from src.repositories.duck_repository import DuckDBRepository

    path = tmp_path / "old.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE stocks (code VARCHAR PRIMARY KEY, name VARCHAR)")
        con.execute("INSERT INTO stocks VALUES ('7203', 'トヨタ')")
    client = object.__new__(DuckDBClient)  # シングルトン (テスト用の :memory:) を避けて、旧 DB を指す
    client.db_path, client.memory_limit = str(path), "1GB"
    DuckDBRepository(client)  # 初期化で、スキーマを更新する
    with duckdb.connect(str(path)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info('stocks')").fetchall()}
        row = con.execute("SELECT code, name, fail_count, last_fail_date FROM stocks").fetchone()
    assert {"fail_count", "last_fail_date", "excluded_until"} <= cols
    assert row[:2] == ("7203", "トヨタ")  # 既存の行は残る


def test_no_data_tracking_does_not_count_unhealthy_or_history_cases():
    repo = _repo_with(_bulk(50))
    codes = _bulk(50)

    # 取得率が低い実行 (通信障害等) は誰も失敗として数えない
    s = sm.update_no_data_tracking(repo, set(codes[:10]), codes, codes_with_history=set(), today=TODAY)
    assert s == {"recovered": 0, "failed": 0, "cooled": 0}

    # 履歴がある銘柄 (直近 2 日に取引が無いだけ) は数えない
    s = sm.update_no_data_tracking(repo, set(codes[1:]), codes, codes_with_history={codes[0]}, today=TODAY)
    assert s["failed"] == 0


# ---- PreFilter の除外理由 --------------------------------------------------------------


def _rows(**kw):
    base = {
        "code": "1001", "name": "テスト", "sector": "卸売業", "market": "Prime",
        "price": None, "equity_ratio": 50.0, "latest_trade_date": None,
    }
    return pl.DataFrame([{**base, **kw}])


def test_prefilter_reports_delisted_and_no_data_reasons():
    delisted = _rows(status="delisted")
    _, rejected = PreFilter.apply_filter(delisted, df_liquidity=None, config={})
    assert rejected["filter_reason"][0] == "上場廃止"

    no_data = _rows(status="active", exclusion_reason=sm.NO_DATA_REASON)
    _, rejected = PreFilter.apply_filter(no_data, df_liquidity=None, config={})
    assert rejected["filter_reason"][0] == "市場データ取得不能"
    assert "PRO Market" in rejected["filter_detail"][0]

    plain = _rows()
    _, rejected = PreFilter.apply_filter(plain, df_liquidity=None, config={})
    assert rejected["filter_reason"][0] == "市場データ取得不能"
    assert "OHLCV" in rejected["filter_detail"][0]
