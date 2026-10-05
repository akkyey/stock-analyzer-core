"""EDINET 取得結果 (JSON) を財務データ (fundamentals) へ反映するサービス

- 値が取れなかった項目は更新しない (既存の値を NULL で消さない)
- 決算期の新旧を比べ、古い期の書類で新しい値を上書きしない
  (損益・1 株当たり指標は period_end、貸借対照表の項目は bs_period_end で判定)
- 半期報告書は貸借対照表の項目 (総資産・純資産・自己資本比率・発行済株式数) だけを更新する
- ROE・自己資本比率は書類の報告値を使う (報告が無い場合は純利益・純資産等から計算)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, TypeGuard

from src.fetcher.xbrl_parser import (
    KIND_ANNUAL,
    KIND_INTERIM,
    PARSER_VERSION,
    XbrlParser,
)
from src.repositories.fundamentals_repository import ANNUAL_PL_FIELDS as _SEED_PL_FIELDS
from src.repositories.fundamentals_repository import (
    BALANCE_SHEET_FIELDS,
    FundamentalsRepository,
)

# 有価証券報告書から更新する損益・1 株当たり指標 (営業利益率は、売上と営業利益から計算して補う)
ANNUAL_PL_FIELDS = tuple(f for f in _SEED_PL_FIELDS if f != "operating_margin")
INTERIM_FIELDS = XbrlParser.INTERIM_FIELDS
# 自己資本比率 (%) として採用する範囲。債務超過 (純資産が負) では 100% を大きく下回るため下限は広く取るが、
# 総資産が 0 と読まれた書類 (例: 543A の -4.08×10^10) のような、桁の違う値は採用しない
EQUITY_RATIO_RANGE = (-1000.0, 100.0)
# 発行済株式数がこれ未満の書類は、1 株当たりの値を採用しない。上場前に提出された書類 (543A は 1 株) では、
# EPS・BPS が純利益・純資産そのもの (約 -4 億円) になり、PER などが意味のない値になる
MIN_PLAUSIBLE_SHARES = 1000.0
PER_SHARE_FIELDS = ("eps", "bps", "dps")


def _is_num(v: Any) -> TypeGuard[float]:
    return isinstance(v, (int, float)) and v == v  # NaN を除く


def _before(period: str, stored: str, or_equal: bool = False) -> bool:
    """書類の期 period が、DB に記録済みの期 stored より古いか (どちらかが不明なら False)。"""
    if not period or not stored:
        return False
    return period <= stored if or_equal else period < stored


def build_record(item: Dict[str, Any], stored: Dict[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    """取得結果 1 件から、fundamentals へ書き込む項目を決める。

    Args:
        item: パーサーの結果 (kind / period_end / submit_date / 各項目)
        stored: DB の現在の出所 {"period_end", "bs_period_end"} (無ければ None)

    Returns:
        書き込む項目 (code を含む)。古い期の書類などで書き込むものが無ければ None。
    """
    item = _drop_implausible_per_share(item)
    period_end = item.get("period_end") or ""
    submitted = item.get("submit_date") or None
    stored_pl = stored.get("period_end") or ""
    stored_bs = stored.get("bs_period_end") or ""
    kind = item.get("kind")

    if kind == KIND_ANNUAL:
        if _before(period_end, stored_pl):
            return None
        record = _annual_record(item, period_end, submitted, update_bs=not _before(period_end, stored_bs))
    elif kind == KIND_INTERIM:
        # 同じ期の再適用は同じ値になるため許容し、古い期だけを除く
        if _before(period_end, stored_bs) or _before(period_end, stored_pl, or_equal=True):
            return None
        record = _pick(item, INTERIM_FIELDS)
        record.update(bs_period_end=period_end or None, bs_submitted_at=submitted)
    else:
        return None

    _fill_equity_ratio(record)
    if not record:
        return None
    return {"code": item["code"], **record}


def _pick(item: Dict[str, Any], fields: tuple) -> Dict[str, Any]:
    """item の項目のうち、数値のものだけを取り出す (範囲外の自己資本比率は除く)。"""
    picked = {f: float(item[f]) for f in fields if _is_num(item.get(f))}
    ratio = picked.get("equity_ratio")
    if ratio is not None and not (EQUITY_RATIO_RANGE[0] <= ratio <= EQUITY_RATIO_RANGE[1]):
        del picked["equity_ratio"]  # 純資産 / 総資産で計算できる場合は、_fill_equity_ratio が補う
    return picked


def _drop_implausible_per_share(item: Dict[str, Any]) -> Dict[str, Any]:
    """発行済株式数が極端に少ない書類 (上場前など) の、株数と 1 株当たりの値を除いた写しを返す。

    損益側 (eps・dps) と貸借対照表側 (bps・shares_outstanding) は別々に取り出すため、書類全体で判定する。
    """
    shares = item.get("shares_outstanding")
    if _is_num(shares) and shares < MIN_PLAUSIBLE_SHARES:
        return {k: v for k, v in item.items() if k not in ("shares_outstanding", *PER_SHARE_FIELDS)}
    return item


def _annual_record(
    item: Dict[str, Any], period_end: str, submitted: Optional[str], update_bs: bool
) -> Dict[str, Any]:
    """有価証券報告書から書き込む項目 (貸借対照表は、半期報告書の方が新しくなければ更新)。"""
    record = _pick(item, ANNUAL_PL_FIELDS)
    record.update(period_end=period_end or None, submitted_at=submitted)
    if "sales" in record:
        # 売上を新しい期の値に更新したときは、旧い売上との比較で計算された売上高成長率 (同梱シード由来。
        # EDINET の取り込みでは計算していない) を空にする。残すと、新しい売上と食い違った成長率が
        # 表示されてしまう (取り込みの「空の値で上書きしない」原則の、意図した例外)
        record["sales_growth"] = None
    if update_bs:
        record.update(_pick(item, BALANCE_SHEET_FIELDS))
        record.update(bs_period_end=period_end or None, bs_submitted_at=submitted)
    # 報告値が無い場合のみ計算で補う
    net_profit, net_assets = item.get("net_profit"), item.get("net_assets")
    if "roe" not in record and _is_num(net_profit) and _is_num(net_assets) and net_assets > 0:
        record["roe"] = net_profit / net_assets * 100.0
    sales, operating_income = item.get("sales"), item.get("operating_income")
    if _is_num(sales) and _is_num(operating_income) and sales > 0:
        record["operating_margin"] = operating_income / sales * 100.0
    return record


def _fill_equity_ratio(record: Dict[str, Any]) -> None:
    """自己資本比率の報告が無ければ、純資産 / 総資産 で補う。"""
    net_assets, total_assets = record.get("net_assets"), record.get("total_assets")
    if (
        "equity_ratio" not in record
        and _is_num(net_assets)
        and _is_num(total_assets)
        and total_assets > 0
    ):
        record["equity_ratio"] = net_assets / total_assets * 100.0


class EdinetBridge:
    """Turbo Acquisition の成果物（JSON）を DuckDB へ橋渡しするサービス。"""

    def __init__(self, repository: Optional[FundamentalsRepository] = None):
        self.logger = logging.getLogger(__name__)
        self.repository = repository or FundamentalsRepository()
        from src.utils.path_resolver import PathResolver

        self.results_dir = str(PathResolver.get_tmp_dir() / "edinet_results")

    def _load_items(self) -> List[Dict[str, Any]]:
        """現行のパーサーで作られた取得結果を読む。古い版の結果は削除する (取り込み直し済み)。"""
        items = []
        for filename in os.listdir(self.results_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.results_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                self.logger.error(f"❌ Failed to load {filename}: {e}")
                continue
            if data.get("parser_version") != PARSER_VERSION:
                # 旧版の結果 (半期報告書で値を NULL にする等の不具合を含む) は使わない
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            data["code"] = filename.split("_")[0].replace(".json", "")
            items.append(data)
        return items

    def bridge_all(self, purge_after: bool = False) -> int:
        """全ての JSON 成果物を DB へ反映する。

        Returns:
            反映した書類数
        """
        if not os.path.exists(self.results_dir):
            self.logger.warning(f"⚠️ Results directory not found: {self.results_dir}")
            return 0

        items = self._load_items()
        if not items:
            self.logger.info("ℹ️ No results to bridge.")
            return 0

        self.logger.info(f"🌉 Bridging {len(items)} results to DuckDB...")
        stored = self.repository.get_provenance()
        # 同じ銘柄は 有価証券報告書 → 半期報告書 の順、期の古い順に反映する
        order = {KIND_ANNUAL: 0, KIND_INTERIM: 1}
        items.sort(
            key=lambda d: (d["code"], order.get(str(d.get("kind") or ""), 9), d.get("period_end") or "")
        )

        records: Dict[str, Dict[str, Any]] = {}
        outcomes: List[tuple[Dict[str, Any], str]] = []  # (書類, success | superseded)
        for item in items:
            code = item["code"]
            current = stored.get(code, {})
            record = build_record(item, current)
            if record is None:
                outcomes.append((item, "superseded"))
                continue
            merged = records.setdefault(code, {"code": code})
            merged.update(record)
            # 同じ実行内の後続書類の判定に反映する
            stored[code] = {
                "period_end": merged.get("period_end", current.get("period_end")),
                "bs_period_end": merged.get("bs_period_end", current.get("bs_period_end")),
            }
            outcomes.append((item, "success"))

        try:
            self._upsert(list(records.values()))
        except Exception as e:
            self.logger.error(f"❌ Batch UPSERT failed: {e}")
            return 0

        self.repository.duck_repo.record_edinet_documents(
            [
                {
                    "doc_id": item["doc_id"],
                    "code": item["code"],
                    "doc_type": item.get("doc_type"),
                    "submit_date": item.get("submit_date"),
                    "is_annual": item.get("is_annual", False),
                    "status": status,
                    "parser_version": PARSER_VERSION,
                }
                for item, status in outcomes
                if item.get("doc_id")
            ]
        )

        applied = sum(1 for _, status in outcomes if status == "success")
        self.logger.info(
            f"✅ Bridged {applied} documents into {len(records)} stocks "
            f"({len(outcomes) - applied} skipped as older than stored data)."
        )
        if purge_after:
            self._cleanup_results([f for f in os.listdir(self.results_dir) if f.endswith(".json")])
        return applied

    def _upsert(self, records: List[Dict[str, Any]]) -> None:
        """項目の組み合わせごとにまとめて UPSERT する (含まれない項目は更新されない)。"""
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for r in records:
            groups.setdefault(tuple(sorted(r)), []).append(r)
        for rows in groups.values():
            self.repository.upsert(rows)

    def _cleanup_results(self, filenames: List[str]):
        """処理済みのファイルをアーカイブまたは削除する"""
        for f in filenames:
            try:
                os.remove(os.path.join(self.results_dir, f))
            except Exception:
                pass
        self.logger.info("🧹 Cleaned up result JSONs.")
