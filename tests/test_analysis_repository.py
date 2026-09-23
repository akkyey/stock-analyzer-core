from datetime import datetime, timedelta

import polars as pl
import pytest

from src.repositories.analysis_repository import AnalysisRepository
from src.utils import get_current_time


def test_save_and_get_count(db_conn):
    """分析結果の保存と総数取得のテスト"""
    repo = AnalysisRepository()
    record = {
        "code": "7203",
        "analyzed_at": get_current_time(),
        "strategy_name": "growth_v1",
        "quant_score": 85.5,
        "ai_reason": "Strong fundamentals",
        "row_hash": "hash123",
    }
    repo.save(record)
    assert repo.get_count() == 1


def test_get_cache(db_conn):
    """キャッシュ取得（code, row_hash, strategy 一致）のテスト"""
    repo = AnalysisRepository()
    code = "9984"
    strategy = "value_v1"
    row_hash = "abc-123"

    # 1. キャッシュが存在しない場合
    assert repo.get_cache(code, row_hash, strategy) is None

    # 2. キャッシュを保存
    repo.save(
        {
            "code": code,
            "analyzed_at": get_current_time(),
            "strategy_name": strategy,
            "row_hash": row_hash,
            "ai_reason": "Cache hit test",
        }
    )

    # 3. キャッシュヒット
    cache = repo.get_cache(code, row_hash, strategy)
    assert cache is not None
    assert cache["ai_reason"] == "Cache hit test"

    # 4. ハッシュが違う場合はヒットしない
    assert repo.get_cache(code, "different-hash", strategy) is None


def test_get_smart_cache(db_conn):
    """有効期限内キャッシュ取得のテスト"""
    repo = AnalysisRepository()
    code = "1301"
    strategy = "trend_v1"

    # 古いレコード (5日前)
    old_time = get_current_time() - timedelta(days=5)
    repo.save(
        {
            "code": code,
            "analyzed_at": old_time,
            "strategy_name": strategy,
            "row_hash": "old",
            "ai_reason": "Old cache",
        }
    )

    # 3日以内の有効期限で検索 -> ヒットしないはず
    assert repo.get_smart_cache(code, strategy, validity_days=3) is None

    # 7日以内の有効期限で検索 -> ヒットするはず
    assert repo.get_smart_cache(code, strategy, validity_days=7) is not None


def test_get_top_results(db_conn):
    """スコア上位取得のテスト"""
    repo = AnalysisRepository()
    repo.save(
        {
            "code": "1",
            "strategy_name": "S",
            "quant_score": 50,
            "analyzed_at": get_current_time(),
            "row_hash": "h1",
        }
    )
    repo.save(
        {
            "code": "2",
            "strategy_name": "S",
            "quant_score": 90,
            "analyzed_at": get_current_time(),
            "row_hash": "h2",
        }
    )
    repo.save(
        {
            "code": "3",
            "strategy_name": "S",
            "quant_score": 70,
            "analyzed_at": get_current_time(),
            "row_hash": "h3",
        }
    )

    top = repo.get_top_results("S", limit=2)
    assert len(top) == 2
    assert top[0]["code"] == "2"  # スコア 90 がトップ
    assert top[1]["code"] == "3"  # スコア 70 が次点


def test_clear(db_conn):
    """分析結果のクリア機能のテスト"""
    repo = AnalysisRepository()
    repo.save(
        {
            "code": "1",
            "strategy_name": "A",
            "analyzed_at": get_current_time(),
            "row_hash": "h1",
        }
    )
    repo.save(
        {
            "code": "2",
            "strategy_name": "B",
            "analyzed_at": get_current_time(),
            "row_hash": "h2",
        }
    )

    # 戦略 A のみクリア
    repo.clear(strategy_name="A")
    assert repo.get_count() == 1

    # 全件クリア
    repo.clear()
    assert repo.get_count() == 0


def test_delete_by_codes_with_filters(db_conn):
    """コード指定削除とフィルタのテスト"""
    repo = AnalysisRepository()
    now_time = get_current_time()

    repo.save(
        {
            "code": "1001",
            "analyzed_at": now_time,
            "row_hash": "h1",
            "ai_reason": "Real results",
        }
    )
    repo.save(
        {
            "code": "1002",
            "analyzed_at": now_time,
            "row_hash": "h2",
            "ai_reason": "[MOCK] test",
        }
    )

    # [MOCK] を除外してコード指定削除
    deleted = repo.delete_by_codes(["1001", "1002"], exclude_mock=True)
    assert deleted == 1
    assert repo.get_count() == 1  # MOCK レコードが残っているはず
