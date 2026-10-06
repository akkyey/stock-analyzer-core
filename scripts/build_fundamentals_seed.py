"""同梱の財務シード (src/resources/fundamentals_seed.parquet) を EDINET から作り直す。

実際のパイプラインと同じ処理 (EDINET の取得 → XBRL パーサー → 決算期の新旧判定つきの反映) を、
作業用の空の DB に対して全社分で実行し、その結果をシードの形式で書き出す。新しい取り込みロジックは
持たないため、パイプラインの検証済みの処理がそのまま使われる。

手順:
  1. 作業用の DB に、現在のシードを読み込む (EDINET から取れない項目を引き継ぐため)
  2. 過去 N 日分 (既定 400 日) の有価証券報告書・半期報告書を取得してパーサーにかける
  3. 反映 (EdinetBridge) し、fundamentals テーブルをシードの列構成で書き出す
     (売上を新しい期の値に更新した銘柄は、旧い売上との比較の売上高成長率を空にする)

EDINET の API キーは .env (EDINET_API_KEY) から読む。値は表示しない。
所要時間は、全社分で約 100 分 (EDINET の書類数と通信の速さによる)。

公開するシードは、空の作業ディレクトリで、1 回の実行で作ること。作業ディレクトリを使い回すと、前回の
取得結果が混ざり、同じ銘柄で、訂正報告書と原本のどちらの提出日時になるかが、実行ごとに変わる
(値は同じだが、出所の列が変わる)。そのため、空でない作業ディレクトリでは、--reuse を付けない限り実行しない。
--reuse は、書き出しの部分だけをやり直す開発用 (取得済みの結果と DB を使う)。

使い方:
  python scripts/build_fundamentals_seed.py --work-dir /tmp/seed_build --out /tmp/seed_build/seed_new.parquet
  python scripts/build_fundamentals_seed.py --days 14 ...   # 動作確認用の短い期間 (公開用には使わない)
  python scripts/build_fundamentals_seed.py --base-seed <旧シード> ...  # 土台のシードを指定する
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "src" / "resources" / "fundamentals_seed.parquet"


PROVENANCE_COLUMNS = ["period_end", "submitted_at", "bs_period_end", "bs_submitted_at"]


def assemble_seed(df, base_seed, listed: set[str] | None):
    """DB の fundamentals (df) から、シードの列構成の DataFrame を組み立てる。

    - 列は、土台のシードの列 + 出所の列 (土台のシードが、すでに出所の列を持つ場合も、重複させない)。
      DB の内部列は含めない
    - 土台のシードにあって DB のテーブルに無い列 (fetch_status 等の旧い状態列) は、土台の値を引き継ぐ
    - updated_at は、株式分割の補正で「財務値がいつ時点のものか」を決める基準日。DB へ読み込んだ時刻
      (今日) で上書きされているため、土台の値を引き継ぐ。土台に無い銘柄 (新規上場) は、取得した書類の
      提出日を使う
    - 土台のシードの銘柄と、現在の JPX の上場銘柄 (listed) だけに絞る。EDINET には、上場廃止済みなどで
      銘柄マスタに無い提出者も含まれ、そのまま入れると、画面に出る銘柄数 (「財務シードから N 銘柄」)
      が、取得対象と食い違って分かりにくくなる (土台に既にある上場廃止済みの銘柄は、そのまま残す)。
      listed が None (取得できなかった) の場合は、絞り込まない

    Returns:
        (シードの DataFrame, 絞り込みで除いた件数 (絞り込まなかった場合は None))
    """
    import polars as pl

    columns = [*base_seed.columns, *[c for c in PROVENANCE_COLUMNS if c not in base_seed.columns]]
    legacy = [c for c in base_seed.columns if c not in df.columns or c == "updated_at"]
    keep = [c for c in columns if c in df.columns and c != "updated_at"]
    out = df.select(keep).join(
        base_seed.select("code", *legacy).with_columns(pl.col("code").cast(pl.Utf8)), on="code", how="left"
    )
    out = out.with_columns(
        pl.coalesce("updated_at", pl.col("submitted_at").cast(pl.Utf8)).alias("updated_at")
    ).select(columns)
    if listed is None:
        return out, None
    known = set(base_seed["code"].cast(pl.Utf8).to_list()) | listed
    kept = out.filter(pl.col("code").is_in(list(known)))
    return kept, out.height - kept.height


def build(
    work_dir: Path, out_path: Path, days: int, seed_path: Path = SEED_PATH, reuse: bool = False
) -> dict:
    """シードを作り直し、out_path に書き出して、件数の概要を返す。"""
    if work_dir.exists() and any(work_dir.iterdir()) and not reuse:
        raise SystemExit(
            f"作業ディレクトリが空ではありません: {work_dir}\n"
            "公開用のシードは、空のディレクトリで 1 回の実行で作ってください (開発用に使い回す場合は --reuse)"
        )
    # 作業ディレクトリへ向ける (DB・取得結果・一時ファイルは、すべてここに作られる)
    work_dir.mkdir(parents=True, exist_ok=True)
    os.environ["STOCK_ANALYZER_BASE_DIR"] = str(work_dir)
    sys.path.insert(0, str(REPO_ROOT))

    import polars as pl

    from src.config_loader import load_config
    from src.env_loader import load_env_file
    from src.fetcher.edinet_fetcher import EdinetFetcher
    from src.fetcher.turbo_acquisition import TurboAcquisitionManager
    from src.fetcher.xbrl_parser import XbrlParser
    from src.repositories.duck_repository import DuckDBRepository
    from src.repositories.fundamentals_repository import FundamentalsRepository
    from src.services.edinet_bridge import EdinetBridge

    load_env_file()
    if not os.getenv("EDINET_API_KEY"):
        raise SystemExit("EDINET_API_KEY が見つかりません (.env に設定してください)")

    config = load_config()
    repo = DuckDBRepository()
    funda = FundamentalsRepository(repo)

    # 1. 現在のシードを読み込む (既に読み込み済みの作業ディレクトリでは、取得結果の反映だけをやり直す)
    if funda.get_count() == 0:
        old = pl.read_parquet(str(seed_path))
        repo.save_fundamentals(old)
        print(f"[1/3] 現在のシード {old.height} 銘柄を読み込みました", flush=True)
    else:
        print(f"[1/3] 作業用 DB に {funda.get_count()} 銘柄があるため、読み込みを省きます", flush=True)

    # 2. EDINET の取得
    started = time.time()
    print(f"[2/3] EDINET から過去 {days} 日分を取得します ({datetime.now():%H:%M:%S})...", flush=True)
    turbo = TurboAcquisitionManager(EdinetFetcher(config), XbrlParser(), config)
    turbo.run_turbo_acquisition(days=days)
    print(
        f"      完了: 新たに取得 {turbo.downloads} 件 / 取得済み {turbo.cache_hits} 件 / "
        f"一覧を取得できなかった日 {len(turbo.failed_dates)} 日 ({(time.time() - started) / 60:.1f} 分)",
        flush=True,
    )

    # 3. 反映と書き出し
    applied = EdinetBridge().bridge_all(purge_after=False)
    with repo.client.get_connection() as conn:
        df = conn.execute("SELECT * FROM fundamentals ORDER BY code").pl()
    base_seed = pl.read_parquet(str(seed_path))
    out, dropped = assemble_seed(df, base_seed, _listed_codes(config))
    if dropped is not None:
        print(f"      銘柄マスタに無い提出者 {dropped} 件を除きました", flush=True)
    out.write_parquet(str(out_path))
    print(f"[3/3] {applied} 件の書類を反映し、{out.height} 銘柄を {out_path} へ書き出しました", flush=True)
    return {
        "applied": applied,
        "rows": out.height,
        "downloads": turbo.downloads,
        "cache_hits": turbo.cache_hits,
        "failed_dates": list(turbo.failed_dates),
    }


def _listed_codes(config: dict) -> set[str] | None:
    """現在の JPX の上場銘柄コード。取得できなければ None (絞り込みをしない)。"""
    try:
        from src.fetcher.jpx import JPXFetcher

        return set(JPXFetcher(config).download_live_list()["code"].astype(str))
    except Exception as e:
        print(f"      ⚠️ JPX の一覧を取得できず、絞り込みを省きます: {e}", flush=True)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", type=Path, required=True, help="作業ディレクトリ (DB・取得結果の置き場)")
    parser.add_argument("--out", type=Path, required=True, help="書き出すシードの parquet")
    parser.add_argument("--days", type=int, default=400, help="EDINET を取得する過去の日数 (既定 400)")
    parser.add_argument(
        "--base-seed",
        type=Path,
        default=SEED_PATH,
        help="土台のシード (既定: 同梱のシード)。EDINET から取れない項目と updated_at を引き継ぐ。"
        "出所の列を持つ (作り直し済みの) シードも指定できる",
    )
    parser.add_argument("--reuse", action="store_true", help="空でない作業ディレクトリを使う (開発用)")
    args = parser.parse_args()
    build(
        args.work_dir.resolve(), args.out.resolve(), args.days, seed_path=args.base_seed.resolve(), reuse=args.reuse
    )


if __name__ == "__main__":
    main()
