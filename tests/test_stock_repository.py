import pytest

from src.repositories.stock_repository import StockRepository


def test_upsert_and_get_all_codes(db_conn):
    """銘柄の一括登録と全コード取得のテスト"""
    repo = StockRepository()
    data = [
        {"code": "1301", "name": "極洋", "sector": "水産・農林業"},
        {"code": "1302", "name": "Dummy", "sector": "その他"},
    ]
    repo.upsert(data)

    codes = repo.get_all_codes()
    assert "1301" in codes
    assert "1302" in codes
    assert len(codes) == 2


def test_exists_and_get_by_code(db_conn):
    """存在確認と個別取得のテスト"""
    repo = StockRepository()
    repo.upsert([{"code": "7203", "name": "トヨタ自動車"}])

    assert repo.exists("7203") is True
    assert repo.exists("9999") is False

    info = repo.get_by_code("7203")
    assert info is not None
    assert info["name"] == "トヨタ自動車"
    assert info["code"] == "7203"


def test_get_all_active_codes(db_conn):
    """アクティブな銘柄のみ取得できるか確認"""
    repo = StockRepository()
    data = [
        {"code": "1001", "name": "Active Stock", "is_active": True},
        {"code": "1002", "name": "Inactive Stock", "is_active": False},
    ]
    repo.upsert(data)

    active_codes = repo.get_all_active_codes()
    assert "1001" in active_codes
    assert "1002" not in active_codes
    assert len(active_codes) == 1


def test_fail_count_management(db_conn):
    """失敗カウントのインクリメントとリセットのテスト"""
    repo = StockRepository()
    repo.upsert([{"code": "8001", "name": "伊藤忠", "fail_count": 0}])

    # インクリメント
    repo.increment_fail_counts(["8001"])
    info = repo.get_by_code("8001")
    assert info["fail_count"] == 1

    # リセット
    repo.reset_fail_counts(["8001"])
    info = repo.get_by_code("8001")
    assert info["fail_count"] == 0


def test_suspend_stocks(db_conn):
    """指定銘柄の休止（サスペンド）処理のテスト"""
    repo = StockRepository()
    repo.upsert([{"code": "9984", "name": "ソフトバンクG", "is_active": True}])

    repo.suspend_stocks(["9984"], reason="Test suspension")

    info = repo.get_by_code("9984")
    assert info["is_active"] is False
    assert info["status"] == "suspended"
    assert info["exclusion_reason"] == "Test suspension"


def test_get_count(db_conn):
    """記銘柄数のカウントが正しいか確認"""
    repo = StockRepository()
    assert repo.get_count() == 0

    repo.upsert([{"code": "1", "name": "A"}, {"code": "2", "name": "B"}])
    assert repo.get_count() == 2
