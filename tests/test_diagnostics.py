"""調査用サマリ (Run Diagnostics) の検証"""

import duckdb

from src.utils.diagnostics import (
    DIAGNOSTICS_FILENAME,
    SOURCE_COMPLETED,
    build_run_summary,
    record_fetch_stat,
    scrub,
    stamp_run,
    write_run_summary,
)


class _Ctx:
    def __init__(self):
        self.config = {
            "fetch_profile": "colab",
            "is_first_run": False,
            # 設定全体は出さない: ホワイトリスト外は載らないこと
            "gdrive": {"shared_folder_id": "SHOULD_NOT_APPEAR"},
            "edinet_api_key": "SHOULD_NOT_APPEAR_EITHER",
        }
        self.errors = ["Spreadsheet export failed", "Batch 3 failed: boom"]
        self.has_partial_failure = True
        self.perf_stats = {"fetch_sec": 812.0, "calc_sec": 0.0}


def test_scrub_masks_secrets():
    key = "0123456789abcdef0123456789abcdef"
    text = (
        f"GET /documents.json?Subscription-Key={key}&date=2026-10-01 "
        f"api_key: {key} user akkyey@example.com"
    )
    out = scrub(text)
    assert key not in out
    assert "akkyey@example.com" not in out
    assert "date=2026-10-01" in out  # 無関係な内容は残る


def test_summary_contains_key_sections_and_excludes_secrets(tmp_path):
    ctx = _Ctx()
    stamp_run(ctx)
    record_fetch_stat(ctx, "sub_batches", 196)
    record_fetch_stat(ctx, "empty_responses")
    ctx.config["fetch_missing_codes"] = ["1432", "1445", "1432"]

    text = build_run_summary(
        ctx,
        working_dir=tmp_path,
        drive_dir=tmp_path / "drive",
        elapsed_sec=908.4,
        extra={"取得コード指定(TARGET_BRANCH)": "v1.2.0"},
        source=SOURCE_COMPLETED,
    )
    assert "調査用サマリ" in text and "Step 3 完了" in text and "908" in text
    assert "JST" in text
    assert "実行時のコード版" in text and "開始時刻" in text
    assert "fetch_profile: colab" in text
    assert "sub_batches: 196" in text and "empty_responses: 1" in text
    assert "取得できなかった銘柄: 2 件" in text  # 重複は除外
    assert "Spreadsheet export failed" in text
    assert "TARGET_BRANCH" in text and "v1.2.0" in text
    assert "SHOULD_NOT_APPEAR" not in text  # ホワイトリスト外の設定は出さない


def test_summary_includes_db_stats(tmp_path):
    db = tmp_path / "cache" / "stock_analyzer.duckdb"
    db.parent.mkdir(parents=True)
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE stocks (code VARCHAR)")
        con.execute("CREATE TABLE fundamentals (code VARCHAR)")
        con.execute("CREATE TABLE daily_metrics (code VARCHAR, entry_date DATE)")
        con.execute("INSERT INTO stocks VALUES ('7203')")
        con.execute("INSERT INTO daily_metrics VALUES ('7203','2026-10-01'),('7203','2026-10-02')")
    text = build_run_summary(_Ctx(), working_dir=tmp_path)
    assert "stocks: 1 行" in text
    assert "daily_metrics: 2 日分" in text
    assert "2026-10-01" in text and "2026-10-02" in text


def test_failure_summary_includes_traceback(tmp_path):
    try:
        raise RuntimeError("データベース不整合のため停止")
    except RuntimeError as e:
        path = write_run_summary(
            tmp_path / "output" / DIAGNOSTICS_FILENAME, context=_Ctx(), error=e
        )
    text = path.read_text(encoding="utf-8")
    assert "Step 3 失敗 (中断)" in text
    assert "RuntimeError" in text and "データベース不整合のため停止" in text


def test_summary_never_raises_even_without_context_or_db(tmp_path):
    """収集に失敗しても本処理を止めない (Step 3 より前の失敗時にも使える)"""
    text = build_run_summary(None, working_dir=tmp_path / "nothing", drive_dir=None)
    assert "実行環境" in text and "市場データ取得" in text
    # 書き込み不能な場所 (親がファイル) でも例外にしない
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    write_run_summary(blocker / DIAGNOSTICS_FILENAME)


def test_record_fetch_stat_ignores_missing_context():
    record_fetch_stat(None, "x")  # 例外にならない
    record_fetch_stat(object(), "x")


def test_summary_is_read_only(tmp_path):
    """診断は存在しないパスを作らない (副作用なし)"""
    work = tmp_path / "not_created"
    drive = tmp_path / "also_not_created"
    text = build_run_summary(_Ctx(), working_dir=work, drive_dir=drive)
    assert "作業層DB: なし" in text
    assert not work.exists() and not drive.exists()


def test_manual_summary_is_labeled_and_distinguishes_run_record():
    """「困ったときは」セルで後から作ったサマリは、結果を『完了』と誤表示しない"""
    no_run = build_run_summary(_Ctx())
    assert "Step 3 の実行記録なし" in no_run
    assert "Step 3 完了" not in no_run

    ctx = _Ctx()
    stamp_run(ctx)
    stale = build_run_summary(ctx)
    assert "直近の Step 3 実行記録" in stale
    assert "実行時のコード版" in stale


def test_drive_unmounted_is_reported(tmp_path):
    """Colab で Drive がアンマウントされている場合、『ファイルなし』ではなく未マウントと表示する"""
    from pathlib import Path

    if Path("/content/drive/MyDrive").exists():
        import pytest

        pytest.skip("Drive がマウントされた環境では検証できない")
    text = build_run_summary(_Ctx(), drive_dir=Path("/content/drive/MyDrive/StockAnalyzer"))
    assert "未マウント" in text
