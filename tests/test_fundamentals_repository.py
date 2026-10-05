import polars as pl
import pytest

from src.repositories.fundamentals_repository import FundamentalsRepository
from src.repositories.stock_repository import StockRepository


def test_upsert_and_count(db_conn):
    """財務データの登録と件数取得のテスト"""
    repo = FundamentalsRepository()
    data = [
        {"code": "7203", "roe": 12.5, "pbr": 1.1, "market_cap": 400000},
        {"code": "9984", "roe": 15.0, "pbr": 2.5, "market_cap": 1000000},
    ]
    repo.upsert(data)
    assert repo.get_count() == 2


def test_get_all_pl_join(db_conn):
    """銘柄マスタとの JOIN を含めた全件取得のテスト"""
    stock_repo = StockRepository()
    funda_repo = FundamentalsRepository()

    # 銘柄マスタの準備
    stock_repo.upsert(
        [
            {"code": "7203", "name": "トヨタ", "sector": "輸送用機器"},
            {"code": "9984", "name": "ソフトバンクG", "sector": "情報・通信業"},
        ]
    )

    # 財務データの準備
    funda_repo.upsert([{"code": "7203", "roe": 12.5, "pbr": 1.1}])

    df = funda_repo.get_all_pl()
    assert len(df) == 2  # stocks に 2件あるので LEFT JOIN で 2件返るはず

    # トヨタのデータ確認
    toyota = df.filter(pl.col("code") == "7203")
    assert toyota["name"][0] == "トヨタ"
    assert toyota["roe"][0] == 12.5

    # 財務データがない銘柄の確認
    sb = df.filter(pl.col("code") == "9984")
    assert sb["name"][0] == "ソフトバンクG"
    assert sb["roe"][0] is None


def test_exists(db_conn):
    """存在確認のテスト"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "1301", "roe": 8.0}])

    assert repo.exists("1301") is True
    assert repo.exists("9999") is False


def test_growth_columns_exist_and_are_persisted(db_conn):
    """成長率の列が fundamentals に存在し、保存・取得できる (旧スキーマでは捨てられていた)"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "7203", "roe": 12.5, "sales_growth": 4.2, "profit_growth": -3.1}])
    with repo.duck_repo.client.get_connection() as conn:
        row = conn.execute(
            "SELECT sales_growth, profit_growth FROM fundamentals WHERE code='7203'"
        ).fetchall()[0]
    assert row == (4.2, -3.1)


def test_backfill_growth_from_seed(db_conn, tmp_path):
    """既存 DB (成長率が全て空) にシードの成長率だけを補い、他の指標は変更しない"""
    repo = FundamentalsRepository()
    repo.upsert([
        {"code": "7203", "roe": 12.5, "pbr": 1.1},
        {"code": "9984", "roe": 15.0},
    ])
    seed = tmp_path / "seed.parquet"
    pl.DataFrame({
        "code": ["7203", "9984", "6758"],
        "sales_growth": [4.2, None, None],
        "profit_growth": [None, -3.1, None],
        "roe": [999.0, 999.0, 999.0],  # シードの他の列は反映されないこと
    }).write_parquet(seed)

    assert repo.backfill_growth_from_seed(seed) == 2  # 成長率を持つ 2 銘柄のみ
    with repo.duck_repo.client.get_connection() as conn:
        rows = dict(
            (r[0], r[1:])
            for r in conn.execute(
                "SELECT code, sales_growth, profit_growth, roe FROM fundamentals ORDER BY code"
            ).fetchall()
        )
    assert rows["7203"] == (4.2, None, 12.5)  # roe は元の値のまま
    assert rows["9984"] == (None, -3.1, 15.0)

    # 2 回目は成長率が登録済みのため何もしない (更新済みの値を上書きしない)
    assert repo.backfill_growth_from_seed(seed) == 0

    # シード無しは 0
    assert repo.backfill_growth_from_seed(tmp_path / "none.parquet") == 0


def test_restore_missing_from_seed_fills_only_nulls(db_conn, tmp_path):
    repo = FundamentalsRepository()
    repo.upsert(
        [
            {"code": "2590", "net_profit": None, "bps": None, "roe": 5.0},
            {"code": "9267", "net_profit": 7875.0, "bps": 2006.0, "roe": 13.8},
        ]
    )
    seed = tmp_path / "seed.parquet"
    pl.DataFrame(
        {
            "code": ["2590", "9267"],
            "net_profit": [100.0, 1.0],
            "bps": [900.0, 1.0],
            "roe": [9.9, 9.9],
            "updated_at": ["2026-09-23 15:44:04", "2026-09-20 10:00:00"],
        }
    ).write_parquet(seed)
    assert repo.restore_missing_from_seed(seed, ("net_profit", "bps")) == 1
    rows = {
        r[0]: r for r in db_conn.execute("SELECT code, net_profit, bps, roe FROM fundamentals").fetchall()
    }
    assert rows["2590"] == ("2590", 100.0, 900.0, 5.0)  # roe は対象外
    assert rows["9267"] == ("9267", 7875.0, 2006.0, 13.8)  # 値がある項目は変えない
    # シード由来の値には、シードの作成日時を基準日として記録する (株式分割の補正用)
    basis = dict(db_conn.execute("SELECT code, submitted_at FROM fundamentals").fetchall())
    assert basis == {"2590": "2026-09-23 15:44", "9267": "2026-09-20 10:00"}


# --- 同梱シードの作り直しを、既存の DB に反映する (refresh_from_seed) --------------------


def _seed(tmp_path, rows):
    path = tmp_path / "seed.parquet"
    schema = {
        "code": pl.Utf8, "sales": pl.Float64, "sales_growth": pl.Float64, "net_profit": pl.Float64,
        "eps": pl.Float64, "dps": pl.Float64, "net_assets": pl.Float64, "bps": pl.Float64,
        "period_end": pl.Utf8, "submitted_at": pl.Utf8, "bs_period_end": pl.Utf8, "bs_submitted_at": pl.Utf8,
    }
    pl.DataFrame(rows, schema=schema).write_parquet(path)
    return path


def _row(db_conn, code):
    return db_conn.execute("SELECT * FROM fundamentals WHERE code = ?", [code]).pl().to_dicts()[0]


def _seed_row(code, **kw):
    base = {"code": code, "sales": None, "sales_growth": None, "net_profit": None, "eps": None, "dps": None,
            "net_assets": None, "bps": None, "period_end": None, "submitted_at": None,
            "bs_period_end": None, "bs_submitted_at": None}
    return {**base, **kw}


def test_refresh_from_seed_updates_old_seed_values_and_records_provenance(db_conn, tmp_path):
    """出所の無い (旧シード由来の) 値は、新しいシードの値に更新し、出所 (決算期・提出日時) を記録する"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "7203", "net_profit": 1.0, "eps": None, "sales": 10.0, "sales_growth": 3.0}])
    seed = _seed(tmp_path, [_seed_row("7203", net_profit=2.0, eps=295.25, sales=12.0, sales_growth=None,
                                       period_end="2026-03-31", submitted_at="2026-06-18 15:38",
                                       net_assets=5.0, bps=3062.8, bs_period_end="2026-03-31",
                                       bs_submitted_at="2026-06-18 15:38")])
    assert repo.refresh_from_seed(seed) == 1
    r = _row(db_conn, "7203")
    assert (r["net_profit"], r["eps"], r["sales"]) == (2.0, 295.25, 12.0)
    assert (r["net_assets"], r["bps"]) == (5.0, 3062.8)
    assert r["sales_growth"] is None  # 売上を更新したため、旧い売上との比較の成長率は空にする
    assert (r["period_end"], r["bs_period_end"]) == ("2026-03-31", "2026-03-31")
    assert r["submitted_at"] == "2026-06-18 15:38"


def test_refresh_from_seed_does_not_overwrite_newer_or_same_period(db_conn, tmp_path):
    repo = FundamentalsRepository()
    repo.upsert([
        {"code": "1001", "net_profit": 9.0, "period_end": "2026-06-30", "sales": 1.0, "sales_growth": 1.5},
        {"code": "1002", "net_profit": 8.0, "period_end": "2026-03-31", "sales": 1.0, "sales_growth": 2.5},
    ])
    seed = _seed(tmp_path, [
        _seed_row("1001", net_profit=1.0, sales=2.0, period_end="2026-03-31"),  # DB の方が新しい
        _seed_row("1002", net_profit=1.0, sales=2.0, period_end="2026-03-31"),  # 同じ期 (適用済みの書類)
    ])
    assert repo.refresh_from_seed(seed) == 0
    assert _row(db_conn, "1001")["net_profit"] == 9.0 and _row(db_conn, "1001")["sales_growth"] == 1.5
    assert _row(db_conn, "1002")["net_profit"] == 8.0 and _row(db_conn, "1002")["sales_growth"] == 2.5


def test_refresh_from_seed_keeps_db_values_where_seed_is_empty(db_conn, tmp_path):
    """シードの値が空の項目は、DB の値を消さない。売上高成長率は、売上を更新しなければ変えない"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "2001", "dps": 30.0, "net_profit": 1.0, "sales": 5.0, "sales_growth": 4.0}])
    seed = _seed(tmp_path, [_seed_row("2001", net_profit=2.0, dps=None, sales=None,
                                       period_end="2026-03-31", submitted_at="2026-06-01 10:00")])
    assert repo.refresh_from_seed(seed) == 1
    r = _row(db_conn, "2001")
    assert (r["net_profit"], r["dps"], r["sales"], r["sales_growth"]) == (2.0, 30.0, 5.0, 4.0)


def test_refresh_from_seed_skips_rows_without_source_document_and_unknown_codes(db_conn, tmp_path):
    repo = FundamentalsRepository()
    repo.upsert([{"code": "3001", "net_profit": 1.0}])
    seed = _seed(tmp_path, [
        _seed_row("3001", net_profit=7.0),  # 出所の書類が無い行 (旧シードと同じ中身)
        _seed_row("9999", net_profit=7.0, period_end="2026-03-31"),  # DB に無い銘柄は追加しない
    ])
    assert repo.refresh_from_seed(seed) == 0
    assert _row(db_conn, "3001")["net_profit"] == 1.0
    assert repo.get_count() == 1


def test_refresh_from_seed_balance_sheet_and_pl_are_judged_separately(db_conn, tmp_path):
    """貸借対照表だけ新しい (半期報告書で更新済み) 場合は、損益だけ更新する"""
    repo = FundamentalsRepository()
    repo.upsert([{"code": "4001", "net_profit": 1.0, "net_assets": 100.0, "bs_period_end": "2026-09-30",
                  "period_end": "2025-03-31"}])
    seed = _seed(tmp_path, [_seed_row("4001", net_profit=2.0, net_assets=50.0, period_end="2026-03-31",
                                       bs_period_end="2026-03-31")])
    assert repo.refresh_from_seed(seed) == 1
    r = _row(db_conn, "4001")
    assert r["net_profit"] == 2.0 and r["net_assets"] == 100.0 and r["bs_period_end"] == "2026-09-30"


def test_refresh_from_seed_old_format_seed_is_ignored(db_conn, tmp_path):
    """出所の列が無い旧形式のシードでは、何もしない (旧シードと同じ中身を、新しい値として扱わない)"""
    path = tmp_path / "old.parquet"
    pl.DataFrame({"code": ["5001"], "net_profit": [2.0]}).write_parquet(path)
    repo = FundamentalsRepository()
    repo.upsert([{"code": "5001", "net_profit": 1.0}])
    assert repo.refresh_from_seed(path) == 0 and repo.refresh_from_seed(tmp_path / "none.parquet") == 0


def test_seed_fingerprint_changes_with_content(tmp_path):
    from src.repositories.fundamentals_repository import seed_fingerprint

    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"seed-1"), b.write_bytes(b"seed-2")
    assert seed_fingerprint(a) != seed_fingerprint(b) and seed_fingerprint(a) == seed_fingerprint(a)
