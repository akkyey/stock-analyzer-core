"""ColabSyncManager (Stage-and-Sync 二層ストレージ) の異常系・耐障害性単体テスト"""

import shutil
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from src.utils.colab_sync import ColabSyncManager


def _create_valid_duckdb(db_path: Path) -> None:
    """テスト用の健全な DuckDB ファイルを生成する"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE stocks (code VARCHAR PRIMARY KEY, name VARCHAR)")
        conn.execute("CREATE TABLE fundamentals (code VARCHAR PRIMARY KEY, per DOUBLE)")
        conn.execute("INSERT INTO stocks VALUES ('7203', 'トヨタ自動車')")
        conn.execute("INSERT INTO fundamentals VALUES ('7203', 10.5)")


def test_is_duckdb_healthy(tmp_path):
    """DuckDB の健全性検証 (非存在、0バイト、破損、健全)"""
    non_existent = tmp_path / "non_existent.duckdb"
    assert not ColabSyncManager.is_duckdb_healthy(non_existent)

    zero_byte = tmp_path / "zero.duckdb"
    zero_byte.write_bytes(b"")
    assert not ColabSyncManager.is_duckdb_healthy(zero_byte)

    corrupted = tmp_path / "corrupted.duckdb"
    corrupted.write_bytes(b"NOT A DUCKDB FILE HEADER 1234567890")
    assert not ColabSyncManager.is_duckdb_healthy(corrupted)

    valid = tmp_path / "valid.duckdb"
    _create_valid_duckdb(valid)
    assert ColabSyncManager.is_duckdb_healthy(valid)


def test_pull_database_recovery_tiers(tmp_path):
    """3段構えの復元検証テスト (第1段: メインDB -> 第2段: .bak -> 第3段: 新規初期化)"""
    drive_dir = tmp_path / "drive"
    drive_cache = drive_dir / "cache"
    drive_cache.mkdir(parents=True, exist_ok=True)

    working_dir = tmp_path / "working"
    working_dir.mkdir(parents=True, exist_ok=True)

    drive_db = drive_cache / "stock_analyzer.duckdb"
    drive_bak = drive_cache / "stock_analyzer.duckdb.bak"

    # [Case 1] 第1段: メイン DB が健全
    _create_valid_duckdb(drive_db)
    pulled_db = ColabSyncManager.pull_database(drive_dir, working_dir)
    assert pulled_db.exists()
    assert ColabSyncManager.is_duckdb_healthy(pulled_db)

    # [Case 2] 第2段: メイン DB が破損、.bak が健全
    drive_db.write_bytes(b"CORRUPTED MAIN DB")
    _create_valid_duckdb(drive_bak)
    # working_dir をリセット
    shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    pulled_db = ColabSyncManager.pull_database(drive_dir, working_dir)
    assert pulled_db.exists()
    assert ColabSyncManager.is_duckdb_healthy(pulled_db)

    # [Case 3] 第3段: メイン DB も .bak も破損
    drive_db.write_bytes(b"CORRUPTED MAIN DB")
    drive_bak.write_bytes(b"CORRUPTED BAK DB")
    shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    pulled_db = ColabSyncManager.pull_database(drive_dir, working_dir)
    # 新規初期化フォールバック (破損したファイルは配置されない)
    assert not pulled_db.exists() or pulled_db.stat().st_size == 0


def test_pull_database_fuse_io_error_handling(tmp_path):
    """Google Drive FUSE からのコピー時に OSError/IOError が発生してもクラッシュせずフォールバックすること"""
    drive_dir = tmp_path / "drive"
    drive_cache = drive_dir / "cache"
    drive_cache.mkdir(parents=True, exist_ok=True)

    working_dir = tmp_path / "working"
    working_dir.mkdir(parents=True, exist_ok=True)

    drive_db = drive_cache / "stock_analyzer.duckdb"
    _create_valid_duckdb(drive_db)

    # shutil.copy2 で FUSE I/O 例外をシミュレート
    with patch("shutil.copy2", side_effect=OSError("Drive FUSE I/O Glitch")):
        pulled_db = ColabSyncManager.pull_database(drive_dir, working_dir)
        # クラッシュせず第3段フォールバックへ到達すること
        assert not pulled_db.exists() or pulled_db.stat().st_size == 0


def test_push_artifacts_safety_and_cleanup(tmp_path):
    """Push フェーズの健全性検証、.bak 世代退避、およびエラー時の .tmp 掃除"""
    drive_dir = tmp_path / "drive"
    drive_cache = drive_dir / "cache"
    drive_output = drive_dir / "output"
    drive_cache.mkdir(parents=True, exist_ok=True)
    drive_output.mkdir(parents=True, exist_ok=True)

    working_dir = tmp_path / "working"
    working_cache = working_dir / "cache"
    working_output = working_dir / "output"
    working_cache.mkdir(parents=True, exist_ok=True)
    working_output.mkdir(parents=True, exist_ok=True)

    working_db = working_dir / "stock_analyzer.duckdb"
    _create_valid_duckdb(working_db)

    # レポート CSV の作成
    daily_csv = working_output / "daily_report.csv"
    daily_csv.write_text("Rank,Code,Score\n1,7203,85.5\n", encoding="utf-8")

    # 既存の古い Drive DB を配置
    old_drive_db = drive_cache / "stock_analyzer.duckdb"
    _create_valid_duckdb(old_drive_db)

    # [Case 1] 正常 Push
    success = ColabSyncManager.push_artifacts(
        working_dir, drive_dir, flush_unmount=False
    )
    assert success is True
    # .bak に古い世代が退避されたこと
    assert (drive_cache / "stock_analyzer.duckdb.bak").exists()
    assert (drive_cache / "stock_analyzer.duckdb").exists()
    assert (drive_output / "daily_report.csv").exists()

    # [Case 2] 作業層 DB が破損している場合の Push 遮断
    working_db.write_bytes(b"CORRUPTED NEW DB")
    blocked = ColabSyncManager.push_artifacts(
        working_dir, drive_dir, flush_unmount=False
    )
    assert blocked is False

    # [Case 3] Drive がアンマウント・切断されている場合の安全復帰
    _create_valid_duckdb(working_db)
    with patch("pathlib.Path.mkdir", side_effect=OSError("Transport endpoint is not connected")):
        safe_return = ColabSyncManager.push_artifacts(
            working_dir, drive_dir, flush_unmount=False
        )
        assert safe_return is False


def test_resolve_drive_dir(monkeypatch):
    """Google Drive パス解決のカスタマイズ性テスト（デフォルト、フォルダ名、相対パス、絶対パス、環境変数）"""
    monkeypatch.delenv("STOCK_ANALYZER_DRIVE_DIR", raising=False)

    # 1. デフォルト (引数なし)
    p_def = ColabSyncManager.resolve_drive_dir()
    assert p_def == Path("/content/drive/MyDrive/StockAnalyzer")

    # 2. ユーザー指定フォルダ名
    p_custom = ColabSyncManager.resolve_drive_dir("MyStockAnalyzer")
    assert p_custom == Path("/content/drive/MyDrive/MyStockAnalyzer")

    # 3. 階層化された相対パス
    p_nested = ColabSyncManager.resolve_drive_dir("Portfolio/JapanEquities")
    assert p_nested == Path("/content/drive/MyDrive/Portfolio/JapanEquities")

    # 4. フルパス（共有ドライブ含む）
    p_shared = ColabSyncManager.resolve_drive_dir("/content/drive/Shareddrives/Team/StockAnalyzer")
    assert p_shared == Path("/content/drive/Shareddrives/Team/StockAnalyzer")

    # 5. Path オブジェクト直接渡し
    p_path_obj = ColabSyncManager.resolve_drive_dir(Path("/custom/dir/path"))
    assert p_path_obj == Path("/custom/dir/path")

    # 6. 環境変数による指定
    monkeypatch.setenv("STOCK_ANALYZER_DRIVE_DIR", "/custom/env/drive/StockAnalyzer")
    p_env = ColabSyncManager.resolve_drive_dir()
    assert p_env == Path("/custom/env/drive/StockAnalyzer")


def test_verify_database_integrity_and_reset(tmp_path):
    """DB 整合性チェックおよびキャッシュリセットのテスト"""
    drive_dir = tmp_path / "drive"
    working_dir = tmp_path / "working"
    db_file = working_dir / "cache" / "stock_analyzer.duckdb"

    # 1. 存在しない DB -> 新規として許可
    valid, msg = ColabSyncManager.verify_database_integrity(db_file)
    assert valid is True
    assert "新規初期化" in msg

    # 2. テーブル欠落 DB -> 不整合検知
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_file)) as conn:
        conn.execute("CREATE TABLE stocks (code VARCHAR PRIMARY KEY)")
    valid, msg = ColabSyncManager.verify_database_integrity(db_file)
    assert valid is False
    assert "必須テーブル" in msg

    # 3. 健全な DB -> 正常判定
    _create_valid_duckdb(db_file)
    valid, msg = ColabSyncManager.verify_database_integrity(db_file)
    assert valid is True
    assert "正常" in msg

    # 4. キャッシュリセット -> ファイル削除確認
    ColabSyncManager.reset_cache(drive_dir, working_dir)
    assert not db_file.exists()


