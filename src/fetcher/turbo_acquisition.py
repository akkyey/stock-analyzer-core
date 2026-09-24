import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Semaphore
from typing import Any, Dict, List

from src.fetcher.edinet_fetcher import EdinetFetcher
from src.fetcher.xbrl_parser import XbrlParser


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

        # パフォーマンス設定 (config から取得。デフォルトは旧 Turbo 設定準拠)
        fetcher_cfg = config.get("fetcher", {})
        self.max_dl_concurrency = fetcher_cfg.get("edinet_concurrency", 2)
        self.max_workers = fetcher_cfg.get("edinet_workers", (os.cpu_count() or 4) + 2)
        self.scan_workers = fetcher_cfg.get("edinet_scan_workers", 10)

        # 一時ディレクトリ設定
        self.tmp_dir = "data/tmp/edinet_xbrl"
        self.results_dir = "data/tmp/edinet_results"
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

    def _scan_document_list(self, days: int) -> List[Dict[str, Any]]:
        """過去 N 日分の書類を並列にスキャンする"""
        dates = [
            (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days)
        ]
        raw_docs = []

        with ThreadPoolExecutor(max_workers=self.scan_workers) as scan_executor:
            future_to_date = {
                scan_executor.submit(self.fetcher.fetch_documents_by_date, d): d
                for d in dates
            }
            for future in future_to_date:
                try:
                    day_res = future.result()
                    # 有報(120), 四半報(140), 修正(130-170) などを対象とする
                    for d in day_res.get("results", []):
                        doc_type = d.get("docTypeCode")
                        if doc_type in [
                            "120",
                            "130",
                            "140",
                            "150",
                            "160",
                            "170",
                        ] and d.get("secCode"):
                            # 証券コード 4桁化
                            d["clean_code"] = d.get("secCode")[:4]
                            d["is_annual"] = doc_type in ["120", "130"]
                            raw_docs.append(d)
                except Exception as e:
                    self.logger.error(f"❌ Scan error: {e}")

        return raw_docs

    def _filter_annual_priority(
        self, docs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """銘柄ごとに『本決算(有報・訂正有報)』を最優先で残し、最新のものを選択する"""
        latest_docs = {}
        for doc in docs:
            code = doc["clean_code"]
            submit_time = doc.get("submitDateTime", "")
            is_annual = doc["is_annual"]

            if code not in latest_docs:
                latest_docs[code] = doc
                continue

            current = latest_docs[code]
            # 優先順位 1: 有報・訂正有報があるなら四半報より優先
            if is_annual and not current["is_annual"]:
                latest_docs[code] = doc
            # 優先順位 2: 種別区分が同じならより提出日時が新しいもの
            elif is_annual == current["is_annual"]:
                if submit_time > current.get("submitDateTime", ""):
                    latest_docs[code] = doc

        return list(latest_docs.values())

    def _execute_pipeline(self, target_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """ダウンロード制限付きの並列パースパイプライン"""
        dl_semaphore = Semaphore(self.max_dl_concurrency)
        results = {}

        def pipeline_worker(doc):
            code = doc["clean_code"]
            doc_id = doc["docID"]
            result_file = os.path.join(self.results_dir, f"{code}.json")

            # 二層キャッシュガード判定 (取得済み同一書類の実通信を100%遮断)
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8") as f:
                        cached_item = json.load(f)
                    cached_doc_id = cached_item.get("doc_id")
                    cached_submit = cached_item.get("submit_date", "")
                    target_submit = doc.get("submitDateTime", "")

                    if cached_doc_id == doc_id or (cached_submit and cached_submit >= target_submit):
                        return code, cached_item
                except Exception:
                    pass

            try:
                # 1. ダウンロード (I/O 制限)
                with dl_semaphore:
                    zip_path = self.fetcher.download_xbrl(doc_id, self.tmp_dir)
                    time.sleep(0.5)  # EDINET API への敬意としてのスリープ

                # 2. パース (CPU 並列)
                financials = self.parser.parse_zip(zip_path)

                # 3. 掃除
                if os.path.exists(zip_path):
                    os.remove(zip_path)

                if financials:
                    item = {
                        "source": "edinet_turbo",
                        "doc_id": doc_id,
                        "doc_type": doc.get("docTypeCode"),
                        "is_annual": doc.get("is_annual", False),
                        "submit_date": doc.get("submitDateTime", ""),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        **financials,
                    }
                    # 個別ファイルにバッファリング
                    with open(result_file, "w") as f:
                        json.dump(item, f, indent=2, ensure_ascii=False)
                    return code, item

            except Exception as e:
                self.logger.error(f"❌ Pipeline failed for {code}: {e}")
            return None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_code = {
                executor.submit(pipeline_worker, doc): doc for doc in target_docs
            }
            for future in future_to_code:
                res = future.result()
                if res:
                    code, data = res
                    results[code] = data

        return results
