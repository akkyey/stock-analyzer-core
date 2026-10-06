"""scripts/build_fundamentals_seed.py の、シードの列の組み立て (EDINET の取得なしで確認できる部分)"""

import importlib.util
from pathlib import Path

import polars as pl

_SPEC = importlib.util.spec_from_file_location(
    "build_fundamentals_seed", Path(__file__).resolve().parent.parent / "scripts" / "build_fundamentals_seed.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assemble_seed = _MODULE.assemble_seed

PROVENANCE = ["period_end", "submitted_at", "bs_period_end", "bs_submitted_at"]


def _db(rows):
    """DB の fundamentals (DB の内部列と出所の列を持つ)"""
    return pl.DataFrame(
        rows,
        schema={
            "code": pl.Utf8, "eps": pl.Float64, "period_end": pl.Utf8, "submitted_at": pl.Utf8,
            "bs_period_end": pl.Utf8, "bs_submitted_at": pl.Utf8, "updated_at": pl.Datetime,
        },
        orient="row",
    )


def _row(code, eps=1.0, sub=None, upd="2026-10-06 09:00:00"):
    from datetime import datetime

    return (code, eps, "2026-03-31" if sub else None, sub, "2026-03-31" if sub else None, sub,
            datetime.fromisoformat(upd))


def test_old_format_base_seed_gets_provenance_columns_and_legacy_values():
    base = pl.DataFrame({"code": ["1001", "1002"], "eps": [None, 5.0], "fetch_status": ["old", "old"],
                         "updated_at": ["2026-09-22 19:21:58", "2026-09-22 19:21:58"]})
    out, dropped = assemble_seed(_db([_row("1001", 10.0, "2026-06-18 15:38"), _row("1002")]), base, None)
    assert out.columns == ["code", "eps", "fetch_status", "updated_at", *PROVENANCE]
    assert dropped is None
    r = out.filter(pl.col("code") == "1001").row(0, named=True)
    assert r["eps"] == 10.0 and r["fetch_status"] == "old"  # DB に無い旧い状態列は、土台の値
    assert r["updated_at"] == "2026-09-22 19:21:58"  # DB へ読み込んだ時刻ではなく、土台の基準日


def test_base_seed_that_already_has_provenance_columns_does_not_duplicate_them():
    """作り直し済みのシード (出所の列を持つ) を土台にしても、列が重複しない
    (出所の列を足す処理が、DuplicateError で落ちていた)"""
    base = pl.DataFrame({"code": ["1001"], "eps": [1.0], "updated_at": ["2026-09-22 19:21:58"],
                         "period_end": ["2026-03-31"], "submitted_at": ["2026-06-18 15:38"],
                         "bs_period_end": ["2026-03-31"], "bs_submitted_at": ["2026-06-18 15:38"]})
    out, _ = assemble_seed(_db([_row("1001", 2.0, "2026-06-19 10:00")]), base, None)
    assert out.columns == ["code", "eps", "updated_at", *PROVENANCE]
    assert out.row(0, named=True)["submitted_at"] == "2026-06-19 10:00"  # 出所は、DB (新しい取得) の値


def test_new_listing_uses_submission_date_as_updated_at_basis():
    base = pl.DataFrame({"code": ["1001"], "eps": [1.0], "updated_at": ["2026-09-22 19:21:58"]})
    out, _ = assemble_seed(_db([_row("1001"), _row("9999", 3.0, "2026-08-01 10:00")]), base, None)
    new = out.filter(pl.col("code") == "9999").row(0, named=True)
    assert new["updated_at"] == "2026-08-01 10:00"


def test_rows_are_limited_to_base_seed_and_listed_codes():
    base = pl.DataFrame({"code": ["1001", "2002"], "eps": [1.0, 2.0], "updated_at": ["x", "y"]})
    db = _db([_row("1001"), _row("2002"), _row("3003"), _row("4004")])
    out, dropped = assemble_seed(db, base, {"3003"})  # 土台の 2 銘柄 + 現在の上場銘柄 3003。4004 は除く
    assert sorted(out["code"].to_list()) == ["1001", "2002", "3003"] and dropped == 1
    out, dropped = assemble_seed(db, base, None)  # JPX の一覧を取得できなかった場合は、絞り込まない
    assert out.height == 4 and dropped is None
