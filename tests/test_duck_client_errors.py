"""DuckDB の接続エラーの案内"""

import duckdb
import pytest

from src.database.duck_client import DuckDBClient


def _client(path) -> DuckDBClient:
    client = object.__new__(DuckDBClient)  # シングルトン (テスト用の :memory:) を避ける
    client.db_path, client.memory_limit = str(path), "1GB"
    return client


def test_connection_left_open_elsewhere_gets_a_clear_message(tmp_path):
    """別のセルで DB を読み取り専用で開いたまま閉じていないと、書き込み用に開けない (Colab で起きた)。
    DuckDB のエラーだけでは、原因が分からないため、対処を案内する"""
    path = tmp_path / "a.duckdb"
    duckdb.connect(str(path)).close()
    leftover = duckdb.connect(str(path), read_only=True)  # 閉じ忘れ
    try:
        with pytest.raises(duckdb.ConnectionException) as exc:
            _client(path).get_connection()
    finally:
        leftover.close()
    message = str(exc.value)
    assert "別のセルに残っています" in message and "ランタイムを再起動" in message
    assert "保存済みのデータは壊れていません" in message
    assert "different configuration" in message  # 元のエラーも残す
    _client(path).get_connection().close()  # 閉じれば、開ける


def test_other_connection_errors_are_not_rewritten(tmp_path):
    with pytest.raises(Exception) as exc:
        _client(tmp_path / "no_such_dir" / "b.duckdb").get_connection()
    assert "別のセルに残っています" not in str(exc.value)
