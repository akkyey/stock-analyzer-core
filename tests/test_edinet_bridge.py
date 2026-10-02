"""EDINET 取得結果の DB 反映 (EdinetBridge) と書類選択の検証"""

import json
from datetime import datetime, timedelta

import pytest

from src.fetcher.turbo_acquisition import (
    TurboAcquisitionManager,
    clean_sec_code,
    doc_period_end,
)
from src.fetcher.xbrl_parser import PARSER_VERSION
from src.repositories.duck_repository import DuckDBRepository
from src.repositories.fundamentals_repository import FundamentalsRepository
from src.services.edinet_bridge import EdinetBridge, build_record


def _fund(conn, code):
    row = conn.execute("SELECT * FROM fundamentals WHERE code = ?", [code]).pl()
    return row.to_dicts()[0] if row.height else None


@pytest.fixture
def bridge(tmp_path):
    b = EdinetBridge(FundamentalsRepository(DuckDBRepository()))
    b.results_dir = str(tmp_path)
    return b


def _write(bridge, name, item):
    with open(f"{bridge.results_dir}/{name}", "w", encoding="utf-8") as f:
        json.dump({"parser_version": PARSER_VERSION, "doc_id": name, **item}, f)


def test_missing_items_do_not_overwrite_existing_values(bridge, db_conn):
    FundamentalsRepository().upsert(
        [{"code": "2590", "net_profit": 100.0, "sales": 5000.0, "bps": 900.0, "dps": 30.0}]
    )
    # 半期報告書 (貸借対照表の項目のみ)。損益・1 株当たり指標を NULL にしない
    _write(
        bridge,
        "2590_interim.json",
        {"kind": "interim", "period_end": "2026-07-20", "total_assets": 166.0, "net_assets": 69.0},
    )
    assert bridge.bridge_all() == 1
    row = _fund(db_conn, "2590")
    assert row["net_profit"] == 100.0 and row["sales"] == 5000.0
    assert row["bps"] == 900.0 and row["dps"] == 30.0
    assert row["total_assets"] == 166.0 and row["net_assets"] == 69.0
    assert row["bs_period_end"] == "2026-07-20"
    assert abs(row["equity_ratio"] - 69.0 / 166.0 * 100) < 1e-9


def test_annual_report_refreshes_roe_and_equity_ratio(bridge, db_conn):
    FundamentalsRepository().upsert([{"code": "9267", "roe": 5.0, "equity_ratio": 10.0}])
    _write(
        bridge,
        "9267_annual.json",
        {
            "kind": "annual",
            "period_end": "2026-06-20",
            "submit_date": "2026-09-09 10:59",
            "net_profit": 7875.0,
            "net_assets": 61373.0,
            "total_assets": 145598.0,
            "roe": 13.8,
            "equity_ratio": 42.0,
            "sales": 220638.0,
            "operating_income": 10985.0,
        },
    )
    bridge.bridge_all()
    row = _fund(db_conn, "9267")
    assert row["roe"] == 13.8 and row["equity_ratio"] == 42.0
    assert row["period_end"] == "2026-06-20" and row["submitted_at"] == "2026-09-09 10:59"
    assert abs(row["operating_margin"] - 10985 / 220638 * 100) < 1e-9


def test_older_period_does_not_overwrite_newer(bridge, db_conn):
    FundamentalsRepository().upsert(
        [{"code": "3154", "net_profit": 999.0, "period_end": "2026-06-30", "bs_period_end": "2026-06-30"}]
    )
    # 過年度 (2021 年 6 月期) の訂正報告書
    _write(bridge, "3154_annual.json", {"kind": "annual", "period_end": "2021-06-30", "net_profit": 1.0})
    assert bridge.bridge_all() == 0
    assert _fund(db_conn, "3154")["net_profit"] == 999.0
    status = db_conn.execute(
        "SELECT status FROM edinet_documents WHERE code = '3154'"
    ).fetchall()
    assert status == [("superseded",)]


def test_annual_then_interim_in_same_run(bridge, db_conn):
    _write(bridge, "7196_annual.json", {"kind": "annual", "period_end": "2026-01-31", "net_profit": 10.0, "net_assets": 100.0, "total_assets": 400.0})
    _write(bridge, "7196_interim.json", {"kind": "interim", "period_end": "2026-07-31", "net_assets": 120.0, "total_assets": 410.0})
    assert bridge.bridge_all() == 2
    row = _fund(db_conn, "7196")
    assert row["period_end"] == "2026-01-31" and row["bs_period_end"] == "2026-07-31"
    assert row["net_assets"] == 120.0  # 半期の方が新しい
    assert row["net_profit"] == 10.0


def test_legacy_results_are_ignored_and_removed(bridge, db_conn):
    FundamentalsRepository().upsert([{"code": "6184", "net_profit": 50.0}])
    legacy = f"{bridge.results_dir}/6184.json"
    with open(legacy, "w") as f:
        json.dump({"doc_id": "X", "net_profit": None, "eps": 1.0}, f)  # 旧版 (版の記録なし)
    assert bridge.bridge_all() == 0
    assert _fund(db_conn, "6184")["net_profit"] == 50.0
    import os

    assert not os.path.exists(legacy)


def test_build_record_interim_older_than_stored_is_skipped():
    item = {"code": "1", "kind": "interim", "period_end": "2026-03-31", "net_assets": 1.0}
    assert build_record(item, {"bs_period_end": "2026-06-30"}) is None
    assert build_record(item, {"period_end": "2026-03-31"}) is None
    # 同じ期の再適用は許容する (毎回の取り込みで「古い書類」と記録しない)
    assert build_record(item, {"bs_period_end": "2026-03-31"}) is not None


# --- 書類の選択 -------------------------------------------------------------


def _doc(code, doc_type, period_end, submit, doc_id):
    return {
        "clean_code": code,
        "docTypeCode": doc_type,
        "is_annual": doc_type in ("120", "130"),
        "period_end": period_end,
        "submitDateTime": submit,
        "docID": doc_id,
    }


def test_selection_prefers_latest_period_not_latest_submission():
    recent = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    older = (datetime.now() - timedelta(days=465)).strftime("%Y-%m-%d")
    docs = [
        _doc("9517", "130", recent, "2026-09-04 15:33", "A"),
        # 新しい期の後に、前期分の訂正が提出された
        _doc("9517", "130", older, "2026-09-14 10:31", "B"),
    ]
    chosen = TurboAcquisitionManager._filter_annual_priority(None, docs)
    assert [d["docID"] for d in chosen] == ["A"]


def test_selection_excludes_stale_amendments_and_keeps_newer_interim():
    stale = (datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d")
    fy = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    next_fy = (datetime.now() + timedelta(days=165)).strftime("%Y-%m-%d")
    docs = [
        _doc("6869", "130", stale, "2025-11-10 10:00", "OLD"),
        _doc("6497", "130", fy, "2026-09-14 15:03", "ANN"),
        _doc("6497", "160", next_fy, "2026-09-14 15:08", "INT"),
        _doc("6497", "160", fy, "2026-03-01 15:08", "OLDINT"),
    ]
    chosen = {d["docID"] for d in TurboAcquisitionManager._filter_annual_priority(None, docs)}
    assert chosen == {"ANN", "INT"}


def test_doc_period_end_and_sec_code():
    assert doc_period_end({"periodEnd": "2026-03-31"}) == "2026-03-31"
    assert (
        doc_period_end({"periodEnd": None, "docDescription": "訂正有価証券報告書－第27期(2024/04/01－2025/03/31)"})
        == "2025-03-31"
    )
    assert clean_sec_code("72030") == "7203"
    assert clean_sec_code("130A0") == "130A"
    assert clean_sec_code("00000") == ""
    assert clean_sec_code(None) == ""
