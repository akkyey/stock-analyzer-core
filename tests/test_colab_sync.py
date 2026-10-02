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
        conn.execute(
            "CREATE TABLE daily_metrics (code VARCHAR, entry_date DATE, PRIMARY KEY (code, entry_date))"
        )
        conn.execute("INSERT INTO stocks VALUES ('7203', 'トヨタ自動車')")
        conn.execute("INSERT INTO fundamentals VALUES ('7203', 10.5)")
        for i in range(5):
            conn.execute(
                f"INSERT INTO daily_metrics VALUES ('7203', '2026-10-0{i+1}'::DATE)"
            )


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
    valid, msg = ColabSyncManager.verify_database_integrity(
        db_file, min_stocks=1, min_history_dates=5
    )
    assert valid is True
    assert "正常" in msg

    # 4. 時系列履歴不足の DB -> 不整合検知
    valid_thin, msg_thin = ColabSyncManager.verify_database_integrity(
        db_file, min_stocks=1, min_history_dates=10
    )
    assert valid_thin is False
    assert "株価時系列履歴 (daily_metrics) が不足しています" in msg_thin

    # 5. pull_database(..., validate_integrity=True) のフェイルセーフ動作
    drive_cache_dir = drive_dir / "cache"
    drive_cache_dir.mkdir(parents=True, exist_ok=True)
    _create_valid_duckdb(drive_cache_dir / "stock_analyzer.duckdb")
    with pytest.raises(RuntimeError, match="データベース不整合を検知したため安全に中断しました"):
        ColabSyncManager.pull_database(
            drive_dir,
            working_dir,
            validate_integrity=True,
            min_stocks=1000,
        )

    # 6. キャッシュリセット -> ファイル削除確認
    ColabSyncManager.reset_cache(drive_dir, working_dir)
    assert not db_file.exists()


def test_colab_notebook_syntax():
    """Google Colab ノートブックの全コードセルが構文エラーなく Python としてパース可能かを検証"""
    import ast
    import json

    notebook_path = (
        Path(__file__).resolve().parent.parent
        / "notebooks"
        / "stock_analyzer_colab.ipynb"
    )
    assert notebook_path.exists(), f"Notebook not found: {notebook_path}"

    with open(notebook_path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    for idx, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") == "code":
            source = "".join(cell.get("source", []))
            # Jupyter マジックコマンド (! または %) をコメントアウトしてパース
            cleaned_lines = []
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("!") or stripped.startswith("%"):
                    cleaned_lines.append("# " + line)
                else:
                    cleaned_lines.append(line)
            cleaned_code = "\n".join(cleaned_lines)
            try:
                ast.parse(cleaned_code)
            except SyntaxError as e:
                pytest.fail(
                    f"Syntax error in Colab notebook cell {idx} (line {e.lineno}): {e.msg}\n"
                    f"Code:\n{e.text}"
                )






# ---------------------------------------------------------------------------
# Colab / CLI 両立の追加検証
# ---------------------------------------------------------------------------


def test_is_first_run_considers_bak(tmp_path):
    """メインが破損でも健全な .bak があれば初回扱いにしない (pull_database の復元順序と一致)"""
    drive_dir = tmp_path / "drive"
    cache = drive_dir / "cache"
    cache.mkdir(parents=True)

    # 何も無い → 初回
    assert ColabSyncManager.is_first_run(drive_dir) is True

    # メイン破損 + 健全な .bak → 初回ではない
    (cache / "stock_analyzer.duckdb").write_bytes(b"CORRUPTED")
    _create_valid_duckdb(cache / "stock_analyzer.duckdb.bak")
    assert ColabSyncManager.is_first_run(drive_dir) is False

    # 両方破損 → 初回
    (cache / "stock_analyzer.duckdb.bak").write_bytes(b"CORRUPTED")
    assert ColabSyncManager.is_first_run(drive_dir) is True


def test_push_preserves_bak_when_drive_db_is_corrupt(tmp_path):
    """Drive 上のメイン DB が破損していても、健全な .bak を上書きしない"""
    drive_dir = tmp_path / "drive"
    working_dir = tmp_path / "working"
    cache = drive_dir / "cache"
    cache.mkdir(parents=True)

    _create_valid_duckdb(cache / "stock_analyzer.duckdb.bak")
    good_bak = (cache / "stock_analyzer.duckdb.bak").read_bytes()
    (cache / "stock_analyzer.duckdb").write_bytes(b"CORRUPTED_MAIN")
    _create_valid_duckdb(working_dir / "cache" / "stock_analyzer.duckdb")

    assert ColabSyncManager.push_artifacts(working_dir, drive_dir, flush_unmount=False)
    assert (cache / "stock_analyzer.duckdb.bak").read_bytes() == good_bak
    assert ColabSyncManager.is_duckdb_healthy(cache / "stock_analyzer.duckdb")


def test_reset_cache_removes_tmp_leftovers(tmp_path):
    """reset_cache は Push 中断で残った .tmp も削除する"""
    drive_dir = tmp_path / "drive"
    working_dir = tmp_path / "working"
    d_tmp = drive_dir / "cache" / "stock_analyzer.duckdb.tmp"
    w_tmp = working_dir / "cache" / "stock_analyzer.duckdb.tmp"
    for f in (d_tmp, w_tmp):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"partial")

    ColabSyncManager.reset_cache(drive_dir, working_dir)
    assert not d_tmp.exists()
    assert not w_tmp.exists()


def test_enforce_integrity_guides_by_environment(tmp_path, capsys, monkeypatch):
    """不整合時は実行環境に合う対処方法だけを案内して RuntimeError で中断する。閾値は共通定数"""
    db_file = tmp_path / "cache" / "stock_analyzer.duckdb"
    _create_valid_duckdb(db_file)  # 銘柄 1 件・履歴 5 日 → 既定閾値では不整合

    # Colab: reset_database の手順のみ (コード例は出さない)
    monkeypatch.setenv("COLAB_RELEASE_TAG", "test")
    with pytest.raises(RuntimeError, match="データベース不整合を検知したため安全に中断しました"):
        ColabSyncManager.enforce_integrity(db_file)
    out = capsys.readouterr().out
    assert "reset_database" in out
    assert "reset_cache" not in out

    # CLI: reset_cache の手順のみ
    monkeypatch.delenv("COLAB_RELEASE_TAG", raising=False)
    monkeypatch.delitem(__import__("sys").modules, "google.colab", raising=False)
    with pytest.raises(RuntimeError):
        ColabSyncManager.enforce_integrity(db_file)
    out = capsys.readouterr().out
    assert "reset_cache" in out
    assert "reset_database" not in out

    # 既定値は共通定数と一致 (verify / pull / enforce で不揃いにならない)
    valid, _ = ColabSyncManager.verify_database_integrity(
        db_file,
        min_stocks=1,
        min_history_dates=ColabSyncManager.MIN_HISTORY_DATES,
    )
    assert valid is False  # 履歴 5 日 < 20 日
    ColabSyncManager.enforce_integrity(db_file, min_stocks=1, min_history_dates=5)


def test_notebook_uses_only_existing_sync_methods():
    """ノートブックが呼ぶ ColabSyncManager のメソッドが実在し、固定タグ時点のコードにも存在する"""
    import json
    import re
    import subprocess

    root = Path(__file__).resolve().parent.parent
    nb = json.loads(
        (root / "notebooks" / "stock_analyzer_colab.ipynb").read_text(encoding="utf-8")
    )
    code = "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
    )
    called = set(re.findall(r"ColabSyncManager\.(\w+)", code))
    assert called, "ノートブックから ColabSyncManager 呼び出しが検出できません"
    for name in called:
        assert hasattr(ColabSyncManager, name), f"未実装メソッド: {name}"

    tag = re.search(r'TARGET_BRANCH = "([^"]+)"', code).group(1)
    res = subprocess.run(
        ["git", "show", f"{tag}:src/utils/colab_sync.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        pytest.skip(f"タグ {tag} が未作成のためタグ時点の検証をスキップ")
    for name in called:
        assert f"def {name}(" in res.stdout, f"{tag} に {name} が存在しません"


def test_pull_keeps_working_db_when_drive_unavailable(tmp_path):
    """Drive 未マウント (Step 3 の flush_and_unmount 後に Step 1 だけ再実行) でも作業層 DB を消さない"""
    drive_dir = tmp_path / "drive_not_mounted"  # 存在しない
    working_dir = tmp_path / "working"
    working_db = working_dir / "cache" / "stock_analyzer.duckdb"
    _create_valid_duckdb(working_db)
    before = working_db.read_bytes()

    res = ColabSyncManager.pull_database(drive_dir, working_dir)
    assert res == working_db
    assert working_db.read_bytes() == before


def test_pull_does_not_clobber_working_db_with_corrupt_drive_copy(tmp_path):
    """Drive 側が破損していても、検証前に作業層 DB を上書きしない"""
    drive_dir = tmp_path / "drive"
    (drive_dir / "cache").mkdir(parents=True)
    (drive_dir / "cache" / "stock_analyzer.duckdb").write_bytes(b"CORRUPTED")
    working_dir = tmp_path / "working"
    working_db = working_dir / "cache" / "stock_analyzer.duckdb"
    _create_valid_duckdb(working_db)

    res = ColabSyncManager.pull_database(drive_dir, working_dir)
    assert ColabSyncManager.is_duckdb_healthy(res)
    assert not list((working_dir / "cache").glob("*.pulltmp"))
