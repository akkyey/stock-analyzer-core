"""レポート出力ヘルパー

複数のハンドラから共通で利用されるレポート生成ロジック。
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from collections import defaultdict

from src.utils import get_current_time

if TYPE_CHECKING:
    from src.orchestration.context import OrchestratorContext


def export_reports(
    context: "OrchestratorContext",
    output_context: str = "daily",
    source_map: dict[str, Any] | None = None,
    only_today: bool = False,
    target_codes: list[str] | None = None,
    scout_results: list[dict[str, Any]] | None = None,
    include_unranked: bool = False,  # [v28.4] 全件レポート用
) -> None:
    """分析結果を抽出し、レポーターに渡してファイルを生成する。"""
    context.logger.info(f"📊 レポート出力処理を開始します ({output_context})...")

    # 1. データの抽出とフィルタリング
    results = _extract_report_data(context, only_today, target_codes, scout_results)
    # [v20.9-fix] results が空でも scout_results があれば（あるいは常に）レポーターを呼び、ヘッダーのみでも生成してURLを確保する
    if not results and not scout_results and not target_codes:
        context.logger.warning(
            "対象となる分析結果もスカウト結果は見つかりませんでした。レポート生成をスキップします。"
        )
        # [v28.11] Fix Discord URL missing: URLだけでも確保を試みる
        fixed_id = context.config.get("gdrive", {}).get("report_spreadsheet_id")
        if fixed_id:
            context.report_url = f"https://docs.google.com/spreadsheets/d/{fixed_id}/edit"
        return

    # 2. 重複排除とスキップレコードの除外
    # [v28.4] include_unranked を渡す
    final_entries = _filter_and_deduplicate_results(
        context, results, include_unranked=include_unranked
    )
    # [v20.9-fix] レポートURLを維持するため、0件でもレポーターを動かす
    if not final_entries and not scout_results:
        context.logger.warning(
            "対象となる有効な分析結果がありません。レポート生成をスキップします。"
        )
        # [v28.11] Fix Discord URL missing: URLだけでも確保を試みる
        fixed_id = context.config.get("gdrive", {}).get("report_spreadsheet_id")
        if fixed_id:
            context.report_url = f"https://docs.google.com/spreadsheets/d/{fixed_id}/edit"
        return

    # 3. ランク履歴の注入
    report_data = _inject_rank_history(context, final_entries)

    # 4. レポーターの呼び出し
    report_paths = context.reporter.generate_reports(
        results=report_data, source_map=source_map, output_context=output_context
    )

    # 5. [v15.1] Google Drive Upload
    _upload_summary_to_gspread(context, report_paths)


def _fetch_master_market_data(context: "OrchestratorContext", target_codes: list[str] | None) -> dict[str, Any]:
    """1. 銘柄ごとの「最新」マーケットデータを取得 (Master構築)"""
    # [v6.1.0] DuckDB SQL で一括取得 (daily_metrics が最新スナップショットを保持)
    query = """
        SELECT 
            m.*, 
            s.name,
            f.* EXCLUDE(code)
        FROM daily_metrics m
        JOIN stocks s ON m.code = s.code
        LEFT JOIN fundamentals f ON m.code = f.code
    """
    params = []
    if target_codes:
        query += " WHERE m.code IN (" + ",".join(["?" for _ in target_codes]) + ")"
        params.extend(target_codes)

    master_dicts = {}
    with context.db.duck_repo.client.get_connection() as conn:
        res = conn.execute(query, params).fetchall()
        cols = [desc[0] for desc in conn.description]
        for row in res:
            d = dict(zip(cols, row))
            # code カラムを明示的に文字列に
            code = str(d["code"])
            master_dicts[code] = d

    return master_dicts


def _fetch_db_analysis(
    context: "OrchestratorContext",
    only_today: bool,
    target_codes: list[str] | None,
    scout_results: list[dict] | None,
) -> list[dict]:
    """2. 分析結果を取得"""
    context.logger.info("  [v6.1.0] Fetching Analysis Results from DuckDB...")
    
    today_dt = get_current_time().date()
    cutoff_date = (today_dt - timedelta(days=4)).isoformat()

    query = """
        SELECT a.*, s.name
        FROM analysis_results a
        JOIN stocks s ON a.code = s.code
    """
    conditions = []
    params = []

    if only_today:
        today_str = context.get_execution_date()
        # analyzed_at は TIMESTAMP なので日付部分で比較するように調整が必要な場合があるが、
        # ここでは単純化のため entry_date (daily_metrics) と JOIN するか、
        # あるいは analyzed_at の日付を抽出する。
        conditions.append("CAST(a.analyzed_at AS DATE) = ?")
        params.append(today_str)
    else:
        conditions.append("CAST(a.analyzed_at AS DATE) >= ?")
        params.append(cutoff_date)

    target_strategies = list(context.config.get("strategies", {}).keys())
    if target_strategies:
        placeholders = ",".join(["?" for _ in target_strategies])
        conditions.append(f"a.strategy_name IN ({placeholders})")
        params.extend(target_strategies)

    if target_codes:
        placeholders = ",".join(["?" for _ in target_codes])
        conditions.append(f"a.code IN ({placeholders})")
        params.extend(target_codes)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    with context.db.duck_repo.client.get_connection() as conn:
        res = conn.execute(query, params).fetchall()
        cols = [desc[0] for desc in conn.description]
        db_analysis_list = [dict(zip(cols, row)) for row in res]

    # [v28.4] 常に scout_results をベースにし、過去の分析結果をマージする構造に変更
    if not scout_results:
        return db_analysis_list

    # 重複排除用のマップ作成 (scout_results を優先)
    # code -> record
    scout_base_map = {}
    for s in scout_results:
        code = str(s.get("code"))
        # scout_results からベースレコード作成
        scout_base_map[code] = {
            "code": code,
            "strategy_name": s.get("strategy", "Scouted"),
            "analyzed_at": get_current_time().isoformat(),
            "ai_reason": "[WAITING FOR ANALYSIS] スコアリング済み、AI詳細分析待ち",
            "ai_score": 0.0,
            "quant_score": s.get("quant_score", 0),
            "is_memory": True,
        }

    # DBにある分析結果で上書き（AI分析データなどを引き継ぐ）
    for db_r in db_analysis_list:
        code = str(db_r.get("code"))
        if code in scout_base_map:
            # 一致する銘柄があれば、AIスコアや理由をDBから優先
            scout_base_map[code].update(
                {
                    "ai_score": db_r.get("quant_score", 0.0),
                    "ai_reason": db_r.get("ai_reason"),
                    "analyzed_at": db_r.get("analyzed_at"),
                    "is_memory": False,  # DB由来
                }
            )
        else:
            # スキャン対象外だがDBにある過去の分析結果も入れる（必要なら）
            scout_base_map[code] = db_r

    return list(scout_base_map.values())


def _update_analysis_from_master(
    db_analysis: list[dict], master_dicts: dict[str, dict]
) -> list[dict]:
    final_list = []
    # [v21.0] Whitelist extraction for updating analysis results
    whitelist_update = [
        "price",
        "trading_value",
        "macd_hist",
        "rsi_14",
        "volatility",
        "real_volatility",
        "trend_up",
        "turnaround_status",
        "profit_status",
        "name",
        "sector",
        "per",
        "pbr",
        "roe",
        "current_ratio",
        "dividend_yield",
        "equity_ratio",
        "operating_cf",
        "operating_margin",
        "sales",
        "sales_growth",
        "profit_growth",
        "debt_equity_ratio",
        "free_cf",
        "bb_mid",
        "bb_sigma",
        "bb_p1sig",
        "bb_p2sig",
        "bb_m1sig",
        "bb_m2sig",
    ]
    for r in db_analysis:
        code = r.get("code")
        if code and code in master_dicts:
            for k in whitelist_update:
                if k in master_dicts[code]:
                    r[k] = master_dicts[code][k]
        final_list.append(r)
    return final_list


def _add_missing_targets_from_master(
    final_list: list[dict],
    master_dicts: dict[str, dict],
    target_codes: list[str] | None,
) -> None:
    existing_codes = {r["code"] for r in final_list}
    if target_codes:
        for code in target_codes:
            if code not in existing_codes and code in master_dicts:
                new_row = master_dicts[code].copy()
                new_row["strategy_name"] = "Balanced Strategy"
                final_list.append(new_row)


def _override_with_scout_results(
    final_list: list[dict], scout_results: list[dict] | None
) -> None:
    if not scout_results:
        return

    # [v28.3] (code, strategy) をキーにしたマップを作成し、高速・正確にアクセス
    scout_map: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scout_results:
        code_val = item.get("code")
        strat_val = item.get("strategy")
        if code_val and strat_val:
            scout_map[(str(code_val), str(strat_val))] = item

    for r in final_list:
        code = str(r.get("code", ""))
        strat = str(r.get("strategy_name") or "Balanced Strategy")

        # マップから該当するスコアリング結果を取得
        m = scout_map.get((code, strat))
        if m:
            mem_score = m.get("quant_score", 0)
            # [v4.5-fix] DB側のスコアの有無（0以外）に関わらず、
            # 今回のメモリ上の最新計算結果を常に最優先して反映する。
            r.update(
                {
                    "quant_score": mem_score,
                    "score_base": m.get("score_base"),
                    "score_value": m.get("score_value"),
                    "score_growth": m.get("score_growth"),
                    "score_quality": m.get("score_quality"),
                    "score_trend": m.get("score_trend"),
                    "score_penalty": m.get("score_penalty"),
                }
            )

            # テクニカル指標の救済 (価格含む)
            metrics_to_rescue = [
                "rsi_14",
                "macd_hist",
                "bb_mid",
                "bb_sigma",
                "bb_p1sig",
                "bb_p2sig",
                "bb_m1sig",
                "bb_m2sig",
                "volatility",
                "real_volatility",
                "trend_up",
                "trend_score",
                "trend_signal",
                "ma_divergence",
                "price",
            ]
            for metric in metrics_to_rescue:
                if metric in m and m[metric] is not None:
                    # [v4.5-fix] name はDBマスタ（日本語）を優先し、スカウト結果（英語）での上書きを避ける
                    if metric == "name" and r.get("name"):
                        continue
                    r[metric] = m[metric]

            if m.get("snapshot_data"):
                r["snapshot_data"] = m.get("snapshot_data")

            r["is_memory"] = True


def _inject_dynamic_data(
    db_analysis: list[dict],
    master_dicts: dict[str, dict],
    target_codes: list[str] | None,
    scout_results: list[dict] | None,
) -> list[dict]:
    """3. 動的注入 (Dynamic Injection)"""
    final_list = _update_analysis_from_master(db_analysis, master_dicts)
    _add_missing_targets_from_master(final_list, master_dicts, target_codes)
    _override_with_scout_results(final_list, scout_results)
    return final_list


def _extract_report_data(
    context: "OrchestratorContext",
    only_today: bool,
    target_codes: list[str] | None,
    scout_results: list[dict[str, Any]] | None = None,
) -> list[dict]:
    """DBからレポート対象データを抽出。
    [v19.6] Master/Journal 分離構造
    """
    context.logger.info("  [v6.1.0] Building Market Master dictionary...")
    master_dicts = _fetch_master_market_data(context, target_codes)
    db_analysis = _fetch_db_analysis(context, only_today, target_codes, scout_results)
    return _inject_dynamic_data(db_analysis, master_dicts, target_codes, scout_results)


def _filter_and_deduplicate_results(
    context: "OrchestratorContext", results: list[dict], include_unranked: bool = False
) -> dict:
    """重複排除。1銘柄+1戦略に対し最新のレコードを優先。
    [v19.6] 数値は既に注入済みのため、ここでは(Code, Strategy)の唯一性と分析の鮮度のみを管理する。
    """
    entry_map: dict[tuple[str, str], dict[str, Any]] = {}

    for r in results:
        strat = r.get("strategy_name") or "Balanced Strategy"
        code = str(r["code"])
        key = (code, strat)

        # 優先順位判定
        if key not in entry_map:
            is_better = True
        else:
            current = entry_map[key]["latest"]

            # 1. メモリ(今回の実行結果)が最優先
            if r.get("is_memory"):
                is_better = True
            elif current.get("is_memory"):
                is_better = False
            else:
                # 2. 分析日時が新しいものを優先
                new_at = str(r.get("analyzed_at") or "")
                old_at = str(current.get("analyzed_at") or "")
                if new_at > old_at:
                    is_better = True
                elif new_at < old_at:
                    is_better = False
                else:
                    # 3. 日時が同じなら ID が大きい（新しい）方を優先
                    is_better = r.get("id", 0) > current.get("id", 0)

        if is_better:
            entry_map[key] = {
                "latest": r,
                "strategies": {strat},
                "data": r,
            }

    if include_unranked:
        context.logger.info(
            f"  [v28.4] Forced all {len(entry_map)} entries for unranked report."
        )
        return entry_map

    final_entries = {}
    min_score = context.config.get("filter", {}).get("min_quant_score", 0)
    for key, info in entry_map.items():
        # [v28.4] include_unranked でない場合はフィルタリングを適用
        score = info["latest"].get("quant_score", 0)
        if score < min_score:
            continue

        # スキップされたものは除外
        ai_reason = info["latest"].get("ai_reason")
        if ai_reason and str(ai_reason).startswith("[ANALYSIS SKIPPED]"):
            context.logger.info(f"🚫 Removing skipped record from report: {key}")
            continue
        final_entries[key] = info
    return final_entries


from collections import defaultdict


def _inject_rank_history(context: "OrchestratorContext", final_entries: dict) -> list[dict]:
    """ランク履歴を成果物データに注入 (N+1問題解消済)"""
    report_data = []
    codes = list({key[0] for key in final_entries.keys() if key[0]})
    history_map = _fetch_bulk_rank_history(context, codes)

    for key, info in final_entries.items():
        code, strat = key
        ranks = history_map.get((code, strat), [])
        info["rank_history"] = " -> ".join(reversed(ranks)) if ranks else "-"
        report_data.append(info)
    return report_data


def _upload_summary_to_gspread(
    context: "OrchestratorContext", report_paths: dict | None
) -> None:
    """サマリーレポートをGoogle Spreadsheetにアップロード。"""
    gdrive_cfg = context.config.get("gdrive", {})
    if not gdrive_cfg.get("enabled", True) or not report_paths:
        return

    summary_path = report_paths.get("summary")
    if not summary_path:
        return

    # 従来の直接アップロード (gspread等を使用)
    try:
        from src.colab_tools import ColabTools

        tools = ColabTools()
        sheet_url = tools.export_to_sheets(
            csv_path=str(summary_path),
            sheet_name="StockAnalysis_Latest_Report",
            output_dir=str(context.reporter.output_dir),
        )
        if sheet_url:
            context.logger.info(f"🚀 Spreadsheet Updated Directly: {sheet_url}")
            context.report_url = sheet_url
        else:
            context.add_error("Spreadsheet export failed (Check log for details).")
    except Exception as e:
        context.logger.error(f"❌ Spreadsheet upload failed: {e}")
        context.add_error(f"Spreadsheet upload failed: {e}")


def _fetch_bulk_rank_history(context: "OrchestratorContext", codes: list[str]) -> dict[tuple[str, str], list[str]]:
    """指定された銘柄リストの直近の順位履歴を一括取得する。"""
    if not codes:
        return {}

    history_map: dict[tuple[str, str], list[str]] = defaultdict(list)
    # [v6.1.0] DuckDB SQL
    query = """
        SELECT code, strategy_name, rank
        FROM rank_history
        WHERE code IN ({})
        ORDER BY code, strategy_name, recorded_at DESC
    """
    
    with context.db.duck_repo.client.get_connection() as conn:
        # 大量銘柄の場合、チャンク分けして実行
        for i in range(0, len(codes), 900):
            chunk = codes[i:i+900]
            placeholders = ",".join(["?" for _ in chunk])
            formatted_query = query.format(placeholders)
            res = conn.execute(formatted_query, chunk).fetchall()
            for row in res:
                key = (str(row[0]), str(row[1]))
                # 直近 3 件を保持
                if len(history_map[key]) < 3:
                    history_map[key].append(str(row[2]))

    return dict(history_map)
