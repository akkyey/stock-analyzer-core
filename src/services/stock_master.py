"""銘柄マスタの月次更新 (JPX 公式の上場銘柄一覧との同期)

同梱の銘柄リストは固定のため、放置すると新規上場銘柄が対象外のまま、上場廃止銘柄が
毎回取得失敗として残り続ける。JPX の一覧は月次更新なので、約 30 日に 1 回取り直して
次を反映する。

- 新規上場: stocks に追加 (is_active=TRUE)
- 上場廃止 (一覧から消えた銘柄): is_active=FALSE / status='delisted' (取得対象から外す。
  データは削除せず、レポート上は「上場廃止」として除外銘柄に残る)
- 再上場: 再び有効化
- 社名・業種・市場の変更: 更新

安全弁: 取得した一覧が極端に小さい・廃止判定が多すぎる場合は、誤った一覧による
大量の無効化を防ぐため何も変更しない。失敗は本処理を止めない (呼び出し側で握りつぶす)。
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from logging import getLogger
from typing import Any, Optional

import polars as pl

from src.repositories.duck_repository import DuckDBRepository

META_KEY = "stock_master_refreshed_at"
REFRESH_INTERVAL_DAYS = 30
MIN_LIVE_STOCKS = 3000  # これ未満の一覧は不完全とみなす
MAX_DELIST_RATIO = 0.10  # 登録銘柄のうち、これを超える割合の廃止判定は異常とみなす
DELISTED_REASON = "上場廃止 (JPX 上場銘柄一覧に掲載なし)"

logger = getLogger(__name__)


@dataclass
class RefreshResult:
    status: str  # "updated" | "skipped_recent" | "skipped_unsafe" | "failed"
    added: int = 0
    delisted: int = 0
    relisted: int = 0
    updated: int = 0
    detail: str = ""


def needs_refresh(
    repo: DuckDBRepository, today: Optional[date] = None, force: bool = False
) -> bool:
    """最終更新から REFRESH_INTERVAL_DAYS 日以上経過していれば True (未実施も True)。"""
    if force:
        return True
    last = repo.get_meta(META_KEY)
    if not last:
        return True
    try:
        last_date = datetime.strptime(last[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today or date.today()) - last_date >= timedelta(days=REFRESH_INTERVAL_DAYS)


def apply_live_list(
    repo: DuckDBRepository, live: pl.DataFrame, today: Optional[date] = None
) -> RefreshResult:
    """JPX の最新一覧 (code/name/sector/market) を銘柄マスタへ反映する。"""
    today = today or date.today()
    if live.is_empty() or "code" not in live.columns:
        return RefreshResult("skipped_unsafe", detail="一覧が空です")
    live = live.with_columns(pl.col("code").cast(pl.Utf8)).unique("code", keep="first")
    if live.height < MIN_LIVE_STOCKS:
        return RefreshResult(
            "skipped_unsafe", detail=f"一覧が不完全です ({live.height} 件 < {MIN_LIVE_STOCKS})"
        )

    with repo.client.get_connection() as conn:
        rows = conn.execute(
            "SELECT code, coalesce(is_active, TRUE), coalesce(status, 'active') FROM stocks"
        ).fetchall()
    current = {r[0]: (bool(r[1]), r[2]) for r in rows}
    live_codes = set(live["code"].to_list())

    new_codes = sorted(live_codes - set(current))
    gone = sorted(c for c, (active, _) in current.items() if active and c not in live_codes)
    relisted = sorted(
        c for c, (active, status) in current.items()
        if not active and status == "delisted" and c in live_codes
    )

    if current and len(gone) > len(current) * MAX_DELIST_RATIO:
        return RefreshResult(
            "skipped_unsafe",
            detail=f"廃止判定が多すぎます ({len(gone)} / {len(current)} 件)。一覧の異常を疑い変更しません",
        )

    # 社名・業種・市場の更新と新規登録 (UPSERT は入力に含まれる列だけを更新する)
    cols = [c for c in ("code", "name", "sector", "market") if c in live.columns]
    repo.save_stocks(live.select(cols))

    with repo.client.get_connection() as conn:
        if gone:
            conn.execute(
                "UPDATE stocks SET is_active = FALSE, status = 'delisted', exclusion_reason = ? "
                "WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))",
                [DELISTED_REASON, gone],
            )
        if relisted:
            conn.execute(
                "UPDATE stocks SET is_active = TRUE, status = 'active', exclusion_reason = NULL "
                "WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))",
                [relisted],
            )
    repo.set_meta(META_KEY, today.isoformat())
    return RefreshResult(
        "updated",
        added=len(new_codes),
        delisted=len(gone),
        relisted=len(relisted),
        updated=len(live_codes & set(current)),
    )


def refresh_stock_master(
    repo: DuckDBRepository,
    fetcher: Any,
    force: bool = False,
    today: Optional[date] = None,
) -> RefreshResult:
    """必要なら JPX から一覧を取得してマスタを更新する。失敗しても例外は送出しない。"""
    try:
        if not needs_refresh(repo, today=today, force=force):
            return RefreshResult("skipped_recent")
        live_pd = fetcher.download_live_list()
        result = apply_live_list(repo, pl.from_pandas(live_pd), today=today)
        if result.status == "skipped_unsafe":
            logger.warning(f"銘柄マスタの更新をスキップしました: {result.detail}")
        return result
    except Exception as e:  # noqa: BLE001 - 更新失敗は本処理を止めない
        logger.warning(f"銘柄マスタの更新に失敗しました (従来のマスタで継続): {e}")
        return RefreshResult("failed", detail=str(e))


# --- 市場データを提供しない銘柄 (PRO Market 等) の再取得抑制 ---------------------------------

NO_DATA_THRESHOLD = 3  # 履歴が全く無い銘柄が、これ回数連続で取得できなかったら再取得を止める
NO_DATA_COOLDOWN_DAYS = 30
NO_DATA_REASON = "市場データ提供なし (Yahoo Finance にデータなし)"
MIN_HEALTHY_COVERAGE = 0.7  # 取得成功率がこれ未満の実行 (通信障害等) は失敗として数えない


def get_cooling_codes(repo: DuckDBRepository, today: Optional[date] = None) -> set[str]:
    """再取得を止めている (excluded_until が今日より先の) 銘柄コードを返す。"""
    today_s = (today or date.today()).isoformat()
    with repo.client.get_connection() as conn:
        rows = conn.execute(
            "SELECT code FROM stocks WHERE excluded_until IS NOT NULL AND excluded_until > ?",
            [today_s],
        ).fetchall()
    return {str(r[0]) for r in rows}


def update_no_data_tracking(
    repo: DuckDBRepository,
    fetched: set[str],
    attempted: list[str],
    codes_with_history: set[str],
    today: Optional[date] = None,
) -> dict[str, int]:
    """取得結果から、市場データが提供されない銘柄を記録する。

    - データを取得できた銘柄: 失敗回数と再取得停止をリセット
    - 取得できず、かつ DB に履歴も無い銘柄: 失敗回数を加算。NO_DATA_THRESHOLD 回連続で
      NO_DATA_COOLDOWN_DAYS 日間、再取得を止める (期間後に 1 回だけ再確認)
    直近 2 日に取引が無いだけの銘柄 (履歴あり) や、通信障害で全体の取得率が低い実行は数えない。

    Returns:
        {"recovered": n, "failed": n, "cooled": n} (記録しなかった場合は全て 0)
    """
    stats = {"recovered": 0, "failed": 0, "cooled": 0}
    if not attempted or len(fetched & set(attempted)) < len(attempted) * MIN_HEALTHY_COVERAGE:
        return stats

    today = today or date.today()
    ok = sorted(fetched & set(attempted))
    failed = sorted(c for c in attempted if c not in fetched and c not in codes_with_history)
    until = (today + timedelta(days=NO_DATA_COOLDOWN_DAYS)).isoformat()

    with repo.client.get_connection() as conn:
        if ok:
            recovered = conn.execute(
                "UPDATE stocks SET fail_count = 0, excluded_until = NULL, "
                "exclusion_reason = CASE WHEN exclusion_reason = ? THEN NULL ELSE exclusion_reason END "
                "WHERE code IN (SELECT * FROM (SELECT UNNEST(?))) "
                "AND (coalesce(fail_count, 0) > 0 OR excluded_until IS NOT NULL) "
                "RETURNING code",
                [NO_DATA_REASON, ok],
            ).fetchall()
            stats["recovered"] = len(recovered)
        if failed:
            conn.execute(
                "UPDATE stocks SET fail_count = coalesce(fail_count, 0) + 1 "
                "WHERE code IN (SELECT * FROM (SELECT UNNEST(?)))",
                [failed],
            )
            stats["failed"] = len(failed)
            cooled = conn.execute(
                "UPDATE stocks SET excluded_until = ?, exclusion_reason = ? "
                "WHERE code IN (SELECT * FROM (SELECT UNNEST(?))) AND fail_count >= ? "
                "RETURNING code",
                [until, NO_DATA_REASON, failed, NO_DATA_THRESHOLD],
            ).fetchall()
            stats["cooled"] = len(cooled)
    return stats
