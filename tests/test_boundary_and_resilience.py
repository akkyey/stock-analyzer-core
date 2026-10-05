from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from src.calc.pre_filter import PreFilter
from src.calc.quant_evaluator import QuantEvaluator
from src.services.financial_repair import FinancialRepairService
from src.utils.colab_sync import ColabSyncManager

# ==============================================================================
# Phase 1: ユーザー操作ミス・リカバリ系テスト (Human-Error & Operational Safety)
# ==============================================================================


def test_colab_sync_reset_database_removes_tmp_and_resets(tmp_path: Path):
    """A-1: reset_database 実行時に一時ファイルやキャッシュが完全にクリーンアップされること."""
    drive_dir = tmp_path / "drive"
    working_dir = tmp_path / "working"
    drive_cache = drive_dir / "cache"
    working_cache = working_dir / "cache"
    drive_cache.mkdir(parents=True)
    working_cache.mkdir(parents=True)

    # ダミーファイルの作成
    dummy_db = drive_cache / "stock_analyzer.duckdb"
    dummy_db.write_text("dummy")
    dummy_tmp = working_cache / "stock_analyzer.duckdb.tmp"
    dummy_tmp.write_text("temp")
    dummy_wal = working_cache / "stock_analyzer.duckdb.wal"
    dummy_wal.write_text("wal")

    ColabSyncManager.reset_cache(drive_dir, working_dir)

    assert not dummy_db.exists()
    assert not dummy_tmp.exists()
    assert not dummy_wal.exists()


def _reset_block_source() -> str:
    """Step 1 セルの「初期化スイッチの処理」ブロックだけを取り出す。"""
    import json

    with open("notebooks/stock_analyzer_colab.ipynb") as f:
        nb = json.load(f)
    cell_src = "".join(nb["cells"][4]["source"])
    start = cell_src.index("# 0. 初期化スイッチの処理")
    end = cell_src.index("# DB の配置")
    return cell_src[start:end]


class _FakeSync:
    def __init__(self):
        self.reset_calls = 0

    def reset_cache(self, drive_dir, working_dir):
        self.reset_calls += 1


def _run_reset_block(namespace: dict) -> None:
    exec(compile(_reset_block_source(), "<step1-reset-block>", "exec"), namespace)


def _ns(sync, drive="/drive/StockAnalyzer", **extra):
    from pathlib import Path

    return {"ColabSyncManager": sync, "DRIVE_DIR": Path(drive), "WORKING_DIR": Path("/work"), **extra}


def test_colab_notebook_reset_database_runs_only_once_per_folder_per_session():
    """A-1: 「すべてのセルを実行」で Step 0 が reset_database を ON に戻しても、同じ保存先の消去は 1 回だけ。

    以前の自動解除は Step 1 の変数を戻すだけで、Step 0 のチェックボックスが ON のままだと、
    実行のたびに DB が消えて初回構築が繰り返されていた。
    """
    sync = _FakeSync()
    ns = _ns(sync)

    ns["reset_database"] = True  # Step 0 (チェックボックス ON)
    _run_reset_block(ns)
    assert sync.reset_calls == 1
    assert ns["reset_database"] is False  # 実行後に解除される

    for _ in range(3):  # 「すべてのセルを実行」: Step 0 が再び True にする
        ns["reset_database"] = True
        _run_reset_block(ns)
    assert sync.reset_calls == 1, "同じ保存先は、同じセッションでは 2 回目以降は消去しない"
    assert ns["reset_database"] is True  # 消去しない場合は、値には触れない


def test_colab_notebook_reset_database_allowed_again_when_folder_changes():
    """保存先フォルダを変えた場合は新しい保存先なので、同じセッションでも初期化できる (実機で、案内どおりに
    操作しても止められた不具合の再発防止)"""
    from pathlib import Path

    sync = _FakeSync()
    ns = _ns(sync, reset_database=True)
    _run_reset_block(ns)
    assert sync.reset_calls == 1

    ns["DRIVE_DIR"] = Path("/drive/StockAnalyzer_Test2")  # 保存先を変更
    ns["reset_database"] = True
    _run_reset_block(ns)
    assert sync.reset_calls == 2

    ns["reset_database"] = True  # 同じ保存先 (Test2) は、もう消さない
    _run_reset_block(ns)
    assert sync.reset_calls == 2


def test_colab_notebook_blocked_reset_explains_the_options(capsys):
    sync = _FakeSync()
    ns = _ns(sync, reset_database=True)
    _run_reset_block(ns)
    ns["reset_database"] = True
    _run_reset_block(ns)
    out = capsys.readouterr().out
    assert "すでに初期化済みです" in out
    assert "ランタイムを再起動するか、保存先フォルダ名を変えてください" in out


def test_colab_notebook_reset_database_off_never_resets():
    sync = _FakeSync()
    ns = _ns(sync, reset_database=False)
    _run_reset_block(ns)
    _run_reset_block(ns)
    assert sync.reset_calls == 0


def test_colab_notebook_reset_database_can_reset_again_after_runtime_restart():
    """ランタイムを再起動すると (変数が消えると)、もう一度消去できる。"""
    sync = _FakeSync()
    for _ in range(2):
        _run_reset_block(_ns(sync, reset_database=True))
    assert sync.reset_calls == 2


def test_colab_notebook_remounts_drive_before_reset():
    """未マウントのまま消去すると、Drive の DB が消えず、再マウント後に古い DB が復元される。
    (Step 3 の完了時にアンマウントされた後で Step 1 だけを再実行した場合)"""
    import json

    with open("notebooks/stock_analyzer_colab.ipynb") as f:
        cell_src = "".join(json.load(f)["cells"][4]["source"])
    assert cell_src.index('drive.mount("/content/drive")') < cell_src.index("ColabSyncManager.reset_cache")


def test_reset_cache_also_removes_edinet_results_but_not_unrelated_tmp(tmp_path: Path):
    """初期化で、EDINET の取得結果 (JSON) とダウンロードした XBRL も消す。残すと、キャッシュの破損を疑って
    初期化しても、前回の (壊れているかもしれない) 取得結果が使い回される。"""
    drive_dir, working_dir = tmp_path / "drive", tmp_path / "working"
    results = working_dir / "tmp" / "edinet_results"
    xbrl = working_dir / "tmp" / "edinet_xbrl"
    other = working_dir / "tmp" / "other.txt"
    for d in (results, xbrl, drive_dir / "cache"):
        d.mkdir(parents=True)
    (results / "7203_annual.json").write_text("{}")
    (xbrl / "x.zip").write_text("x")
    other.write_text("keep")

    ColabSyncManager.reset_cache(drive_dir, working_dir)

    assert not results.exists() and not xbrl.exists()
    assert other.exists()  # 関係のない一時ファイルは消さない


def test_colab_sync_pull_when_drive_unmounted_falls_back_safely(tmp_path: Path):
    """A-3: Google Drive が未マウント（空・存在しない）でも作業層の既存DBで安全に稼働すること."""
    unmounted_drive = tmp_path / "non_existent_drive"
    working_dir = tmp_path / "working"
    working_cache = working_dir / "cache"
    working_cache.mkdir(parents=True)

    local_db = working_cache / "stock_analyzer.duckdb"
    local_db.write_text("local_db")

    with patch.object(ColabSyncManager, "is_duckdb_healthy", return_value=True), \
         patch.object(ColabSyncManager, "verify_database_integrity", return_value=(True, "OK")):
        selected_db = ColabSyncManager.pull_database(unmounted_drive, working_dir, validate_integrity=True)

    assert selected_db == local_db
    assert selected_db.exists()


def test_colab_sync_pull_when_both_missing_creates_fresh_path(tmp_path: Path):
    """A-3/A-4: Drive にも作業層にもDBが無い場合は、新規作成用のパスを返し例外で落ちないこと."""
    empty_drive = tmp_path / "drive"
    empty_working = tmp_path / "working"
    empty_drive.mkdir()
    empty_working.mkdir()

    selected_db = ColabSyncManager.pull_database(empty_drive, empty_working, validate_integrity=True)
    assert selected_db == empty_working / "cache" / "stock_analyzer.duckdb"


# ==============================================================================
# Phase 2: 財務・株価データの境界値・異常値テスト (Boundary & Anomaly Safety)
# ==============================================================================


def test_pre_filter_boundary_zero_and_negative_price():
    """B-1: 株価が0円以下、極小値（1円等）の境界値テスト."""
    schema = {
        "code": pl.String,
        "price": pl.Float64,
        "entry_date": pl.String,
        "avg_trading_value_20d": pl.Float64,
        "zero_volume_days_5d": pl.Int64,
        "latest_trade_date": pl.String,
        "is_recent_trade": pl.Boolean,
        "equity_ratio": pl.Float64,
        "net_assets": pl.Float64,
        "status": pl.String,
    }

    # 1001: 株価0円 (未取得または異常値)
    # 1002: 株価マイナス (あり得ない異常値)
    # 1003: 株価10円 (超低位ボロ株: min_price 50円未満)
    # 1004: 株価50円 (境界値: 50円以上で通過可能)
    records = [
        {"code": "1001", "price": 0.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 50.0, "net_assets": 1e9, "status": "active"},
        {"code": "1002", "price": -5.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 50.0, "net_assets": 1e9, "status": "active"},
        {"code": "1003", "price": 10.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 50.0, "net_assets": 1e9, "status": "active"},
        {"code": "1004", "price": 50.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 50.0, "net_assets": 1e9, "status": "active"},
    ]
    df = pl.DataFrame(records, schema=schema)
    result = PreFilter.evaluate(df, config={"hard_filters": {"min_price": 50.0}})

    passed_codes = result.passed_df["code"].to_list()
    assert "1004" in passed_codes  # 50円ちょうどは通過
    assert "1001" not in passed_codes  # 0円は足切り
    assert "1002" not in passed_codes  # 負値は足切り
    assert "1003" not in passed_codes  # 10円は超低位ボロ株として足切り


def test_pre_filter_boundary_insolvent_negative_equity():
    """B-2: 債務超過（純資産マイナス / 自己資本比率 0% 以下）の確実な隔離."""
    schema = {
        "code": pl.String,
        "price": pl.Float64,
        "entry_date": pl.String,
        "avg_trading_value_20d": pl.Float64,
        "zero_volume_days_5d": pl.Int64,
        "latest_trade_date": pl.String,
        "is_recent_trade": pl.Boolean,
        "equity_ratio": pl.Float64,
        "net_assets": pl.Float64,
        "status": pl.String,
    }
    records = [
        # 2001: 純資産マイナス（債務超過）
        {"code": "2001", "price": 500.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": -15.0, "net_assets": -5e8, "status": "active"},
        # 2002: 自己資本比率 0.0% (境界値)
        {"code": "2002", "price": 500.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 0.0, "net_assets": 100.0, "status": "active"},
        # 2003: 正常企業 (純資産プラス、自己資本比率 40%)
        {"code": "2003", "price": 500.0, "entry_date": "2026-10-02", "avg_trading_value_20d": 1e8, "zero_volume_days_5d": 0, "latest_trade_date": "2026-10-02", "is_recent_trade": True, "equity_ratio": 40.0, "net_assets": 1e9, "status": "active"},
    ]
    df = pl.DataFrame(records, schema=schema)
    result = PreFilter.evaluate(df)

    passed_codes = result.passed_df["code"].to_list()
    assert passed_codes == ["2003"]

    rejected_reasons = dict(
        zip(result.rejected_df["code"], result.rejected_df["filter_reason"], strict=True)
    )
    assert rejected_reasons["2001"] == "構造的破綻 (債務超過)"
    assert rejected_reasons["2002"] == "構造的破綻 (債務超過)"


def test_financial_repair_zero_division_resilience():
    """B-1: FinancialRepairService で EPS=0, BPS=0, 株価=0, 負値のゼロ除算耐性."""
    records = [
        # EPS=0 (PER 除算ゼロ), BPS=0 (PBR 除算ゼロ)
        {"code": "3001", "price": 1000.0, "eps": 0.0, "bps": 0.0, "dps": 0.0},
        # EPS負 (赤字), BPS負 (債務超過)
        {"code": "3002", "price": 1000.0, "eps": -50.0, "bps": -100.0, "dps": -10.0},
        # 株価0 (配当利回り除算ゼロ)
        {"code": "3003", "price": 0.0, "eps": 100.0, "bps": 500.0, "dps": 30.0},
        # 正常値
        {"code": "3004", "price": 1000.0, "eps": 100.0, "bps": 500.0, "dps": 30.0},
    ]
    df = pl.DataFrame(records)
    repaired = FinancialRepairService.repair(df)

    row1 = repaired.filter(pl.col("code") == "3001").to_dicts()[0]
    assert row1["per"] is None  # EPS=0 では PER を算出せず None
    assert row1["pbr"] is None  # BPS=0 では PBR を算出せず None

    row2 = repaired.filter(pl.col("code") == "3002").to_dicts()[0]
    assert row2["per"] is None  # 赤字では PER None
    assert row2["pbr"] is None  # 債務超過では PBR None

    row3 = repaired.filter(pl.col("code") == "3003").to_dicts()[0]
    assert row3["dividend_yield"] is None  # 株価0では配当利回り None (ZeroDivisionError 回避)

    row4 = repaired.filter(pl.col("code") == "3004").to_dicts()[0]
    assert row4["per"] == pytest.approx(10.0)
    assert row4["pbr"] == pytest.approx(2.0)
    assert row4["dividend_yield"] == pytest.approx(3.0)


def test_quant_evaluator_extreme_outliers_capping():
    """B-2: 極端な外れ値 (PER 99,999倍、PER 0.01倍の一過性トラップ、負のROE) のスコア安全性."""
    evaluator = QuantEvaluator()

    # 1. 負のROE、0のROE -> 0点
    assert evaluator._score_roe(-50.0) == 0.0
    assert evaluator._score_roe(0.0) == 0.0
    # 極大のROE (100%) -> 30点満点キャップ
    assert evaluator._score_roe(100.0) == 30.0

    # 2. 負のPBR、0のPBR -> 0点
    assert evaluator._score_pbr(-2.0) == 0.0
    assert evaluator._score_pbr(0.0) == 0.0
    # 巨大なPBR (50倍) -> 0点
    assert evaluator._score_pbr(50.0) == 0.0

    # 3. 負のPER、0のPER -> 0点
    assert evaluator._score_per(-10.0) == 0.0
    assert evaluator._score_per(0.0) == 0.0
    # PER 1.5倍だが本業赤字 (営業利益率 <= 0) の一過性トラップ -> 0点に抑制
    assert evaluator._score_per(1.5, operating_margin=-2.0) == 0.0
    # PER 99,999倍の超割高 -> ペナルティ上限 -3.0点
    assert evaluator._score_per(99999.0) == -3.0
