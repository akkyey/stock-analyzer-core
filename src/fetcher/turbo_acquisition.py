import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock, Semaphore
from typing import Any, Dict, List

from src.fetcher.edinet_fetcher import (
    EdinetAuthenticationError,
    EdinetFetcher,
    mask_api_key,
)
from src.fetcher.xbrl_parser import (
    KIND_ANNUAL,
    KIND_INTERIM,
    PARSER_VERSION,
    XbrlParser,
)
from src.utils import get_current_time

# 対象とする書類: 有価証券報告書 (120)・訂正 (130)、半期報告書 (160)・訂正 (170)。
# 四半期報告書 (140/150) は 2024 年 4 月に廃止され、現在提出されるのは過去分の訂正のみ
ANNUAL_DOC_TYPES = ("120", "130")
INTERIM_DOC_TYPES = ("160", "170")

# 有価証券報告書は期末から 3 か月以内に提出されるため、期末がこれより古い書類 (過年度の
# 訂正報告書) は最新の期ではありえない。DB に期の記録が無い (同梱シード由来の) 銘柄でも
# 古い期で上書きしないよう、選択の段階で除外する
STALE_ANNUAL_DAYS = 480

_PERIOD_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def doc_period_end(doc: Dict[str, Any]) -> str:
    """書類の対象期間の末日 (YYYY-MM-DD)。不明なら空文字。

    一覧の periodEnd は訂正報告書では空のため、書類名 (例: "訂正有価証券報告書－第27期
    (2024/04/01－2025/03/31)") の最後の日付で補う。
    """
    pe = (doc.get("periodEnd") or "").strip()
    if pe:
        return pe[:10]
    dates = _PERIOD_RE.findall(doc.get("docDescription") or "")
    if dates:
        y, m, d = dates[-1]
        return f"{y}-{m}-{d}"
    return ""


def clean_sec_code(sec_code: Any) -> str:
    """5 桁の証券コードを 4 桁にする。上場銘柄のコードでなければ空文字。"""
    code = str(sec_code or "").strip()[:4]
    if len(code) != 4 or code == "0000" or not code.isalnum():
        return ""
    return code


class TurboAcquisitionManager:
    """
    EDINET 取得の非対称並列処理を司るマネージャー。
    旧 backfill_edinet.py の『10分の壁を突破する』ロジックを実働コードに再統合。
    """

    def __init__(
        self, fetcher: EdinetFetcher, parser: XbrlParser, config: Dict[str, Any]
    ):
        self.fetcher = fetcher
        self.parser = parser
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.failed_dates: List[str] = []
        # 取得済み (キャッシュ) で通信を省いた書類数と、実際にダウンロードした書類数 (進捗表示用)
        self.cache_hits = 0
        self.downloads = 0
        self._count_lock = Lock()

        # パフォーマンス設定 (config から取得。デフォルトは旧 Turbo 設定準拠)
        fetcher_cfg = config.get("fetcher", {})
        self.max_dl_concurrency = fetcher_cfg.get("edinet_concurrency", 2)
        self.max_workers = fetcher_cfg.get("edinet_workers", (os.cpu_count() or 4) + 2)
        self.scan_workers = fetcher_cfg.get("edinet_scan_workers", 10)

        # 一時ディレクトリ設定 (PathResolver 経由)
        from src.utils.path_resolver import PathResolver

        tmp_base = PathResolver.get_tmp_dir()
        self.tmp_dir = str(tmp_base / "edinet_xbrl")
        self.results_dir = str(tmp_base / "edinet_results")
        os.makedirs(self.tmp_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)

    def run_turbo_acquisition(self, days: int = 365) -> Dict[str, Any]:
        """
        全工程を Turbo 並列で実行する。
        1. Scan Documents (Parallel)
        2. Filter Annual Priority
        3. Download & Parse Pipeline (Asymmetric Parallel)
        """
        self.logger.info(f"🚀 Starting Turbo Acquisition for {days} days...")

        # 1. 書類リストのスキャン (Turbo 1: 並列スキャン)
        raw_docs = self._scan_document_list(days)
        if not raw_docs:
            self.logger.warning("⚠️ No documents found during scan.")
            return {}

        # 2. フィルタリング (本決算優先)
        target_docs = self._filter_annual_priority(raw_docs)
        self.logger.info(f"🎯 Filtered to {len(target_docs)} unique targets.")

        # 3. 非対称パイプライン (Turbo 3: DL制限付き並列パース)
        final_results = self._execute_pipeline(target_docs)

        self.logger.info(f"✨ Turbo Acquisition completed. Total: {len(final_results)}")
        return final_results

    def _count(self, name: str) -> None:
        with self._count_lock:
            setattr(self, name, getattr(self, name) + 1)

    def _scan_document_list(self, days: int) -> List[Dict[str, Any]]:
        """過去 N 日分の書類を並列にスキャンする"""
        dates = [
            (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days)
        ]
        raw_docs = []
        # 一覧を取得できなかった日 (通信失敗)。次回の走査期間の起点に使う
        self.failed_dates = []

        with ThreadPoolExecutor(max_workers=self.scan_workers) as scan_executor:
            future_to_date = {
                scan_executor.submit(self.fetcher.fetch_documents_by_date, d): d
                for d in dates
            }
            for future in future_to_date:
                try:
                    day_res = future.result()
                    if day_res.get("failed"):
                        self.failed_dates.append(future_to_date[future])
                    for d in day_res.get("results", []):
                        doc_type = d.get("docTypeCode")
                        if doc_type not in ANNUAL_DOC_TYPES + INTERIM_DOC_TYPES:
                            continue
                        code = clean_sec_code(d.get("secCode"))
                        if not code:
                            continue
                        d["clean_code"] = code
                        d["is_annual"] = doc_type in ANNUAL_DOC_TYPES
                        d["period_end"] = doc_period_end(d)
                        raw_docs.append(d)
                except Exception as e:
                    if isinstance(e, EdinetAuthenticationError):
                        raise
                    self.logger.error(f"❌ Scan error: {mask_api_key(str(e))}")
                    self.failed_dates.append(future_to_date[future])

        return raw_docs

    def _filter_annual_priority(
        self, docs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """銘柄ごとに、取り込む書類を選ぶ。

        - 有価証券報告書 (訂正を含む): 対象期間が最も新しいもの。同じ期は提出が新しいもの
          (過年度の訂正報告書が後から出ても、新しい期の値を古い期で上書きしない)。
          期末が STALE_ANNUAL_DAYS より古いものは対象外
        - 半期報告書 (訂正を含む): 上の有価証券報告書より新しい期のもののみ
          (貸借対照表の項目だけを更新する)
        """

        def key(d: Dict[str, Any]) -> tuple:
            return (d.get("period_end") or "", d.get("submitDateTime") or "")

        stale_before = (get_current_time() - timedelta(days=STALE_ANNUAL_DAYS)).strftime("%Y-%m-%d")
        annual: Dict[str, Dict[str, Any]] = {}
        interim: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            if doc["is_annual"] and doc.get("period_end") and doc["period_end"] < stale_before:
                continue
            bucket = annual if doc["is_annual"] else interim
            code = doc["clean_code"]
            if code not in bucket or key(doc) > key(bucket[code]):
                bucket[code] = doc

        selected = list(annual.values())
        for code, doc in interim.items():
            base = annual.get(code)
            if base is None or (doc.get("period_end") or "") > (base.get("period_end") or ""):
                selected.append(doc)
        return selected

    def _execute_pipeline(self, target_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """ダウンロード制限付きの並列パースパイプライン"""
        from src.fetcher.edinet_fetcher import EdinetAuthenticationError, mask_api_key
        from src.repositories.duck_repository import DuckDBRepository

        # DuckDB 永続層から取得済みの doc_id 一覧をロード (Colab セッション跨ぎ差分キャッシュ)。
        # 古い版のパーサーで処理した書類は対象に含めない (取り込み直す)
        try:
            duck_repo = DuckDBRepository()
            processed_doc_ids = duck_repo.get_processed_edinet_doc_ids(
                min_parser_version=PARSER_VERSION
            )
        except Exception:
            processed_doc_ids = set()

        if processed_doc_ids:
            self.logger.info(
                f"🗄️ DuckDB 差分キャッシュ: {len(processed_doc_ids)} 件の処理済み書類を実通信から除外します。"
            )

        dl_semaphore = Semaphore(self.max_dl_concurrency)
        results = {}

        def _record(doc: Dict[str, Any], status: str) -> None:
            try:
                duck_repo.record_edinet_document(
                    doc_id=doc["docID"],
                    code=doc["clean_code"],
                    doc_type=doc.get("docTypeCode"),
                    submit_date=doc.get("submitDateTime", ""),
                    is_annual=doc.get("is_annual", False),
                    status=status,
                    parser_version=PARSER_VERSION,
                )
            except Exception:
                pass

        def pipeline_worker(doc):
            code = doc["clean_code"]
            doc_id = doc["docID"]
            kind = KIND_ANNUAL if doc.get("is_annual") else KIND_INTERIM
            result_file = os.path.join(self.results_dir, f"{code}_{kind}.json")

            # 第1段: DuckDB 永続層キャッシュ判定 (セッション跨ぎでの実通信を100%遮断)
            if doc_id in processed_doc_ids:
                self._count("cache_hits")
                return None

            # 第2段: ローカル作業層 JSON キャッシュ判定 (同じ書類を同じ版で処理済み)
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8") as f:
                        cached_item = json.load(f)
                    if (
                        cached_item.get("parser_version") == PARSER_VERSION
                        and cached_item.get("doc_id") == doc_id
                    ):
                        self._count("cache_hits")
                        return code, cached_item
                except Exception:
                    pass

            try:
                # 1. ダウンロード (I/O 制限)
                self._count("downloads")
                with dl_semaphore:
                    zip_path = self.fetcher.download_xbrl(doc_id, self.tmp_dir)
                    time.sleep(0.5)  # EDINET API への敬意としてのスリープ

                # 2. パース (CPU 並列)
                financials = self.parser.parse_zip(zip_path)

                # 3. 掃除
                if os.path.exists(zip_path):
                    os.remove(zip_path)

                # 書類の種別と中身の期間が食い違う場合 (様式の特殊な有価証券報告書等) は取り込まない
                if financials and financials.get("kind") == kind:
                    item = {
                        "source": "edinet_turbo",
                        "parser_version": PARSER_VERSION,
                        "doc_id": doc_id,
                        "doc_type": doc.get("docTypeCode"),
                        "is_annual": doc.get("is_annual", False),
                        "submit_date": doc.get("submitDateTime", ""),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        **financials,
                    }
                    if not item.get("period_end"):
                        item["period_end"] = doc.get("period_end") or None
                    # 個別ファイルにバッファリング
                    with open(result_file, "w", encoding="utf-8") as f:
                        json.dump(item, f, indent=2, ensure_ascii=False)
                    return code, item
                # パース失敗または財務項目未検出時も記録して次回以降の無駄な通信を防止
                _record(doc, "parse_failed")

            except EdinetAuthenticationError:
                raise
            except Exception as e:
                self.logger.error(
                    f"❌ Pipeline failed for {code}: {mask_api_key(str(e))}"
                )
                _record(doc, "error")
            return None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_code = {
                executor.submit(pipeline_worker, doc): doc for doc in target_docs
            }
            for future in future_to_code:
                res = future.result()
                if res:
                    code, data = res
                    results[f"{code}_{data.get('kind')}"] = data

        return results
