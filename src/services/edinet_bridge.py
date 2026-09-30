import json
import logging
import os
from typing import List, Optional

import polars as pl

from src.repositories.fundamentals_repository import FundamentalsRepository
from src.services.financial_repair import FinancialRepairService


class EdinetBridge:
    """
    Turbo Acquisition の成果物（JSON）を DuckDB へ橋渡しするサービス。
    """

    def __init__(self, repository: Optional[FundamentalsRepository] = None):
        self.logger = logging.getLogger(__name__)
        self.repository = repository or FundamentalsRepository()
        from src.utils.path_resolver import PathResolver

        self.results_dir = str(PathResolver.get_tmp_dir() / "edinet_results")

    def bridge_all(self, purge_after: bool = False) -> int:
        """
        全ての JSON 成果物を一括で DB へ UPSERT する。
        """
        if not os.path.exists(self.results_dir):
            self.logger.warning(f"⚠️ Results directory not found: {self.results_dir}")
            return 0

        json_files = [f for f in os.listdir(self.results_dir) if f.endswith(".json")]
        if not json_files:
            self.logger.info("ℹ️ No results to bridge.")
            return 0

        self.logger.info(f"🌉 Bridging {len(json_files)} results to DuckDB...")

        data_to_upsert = []
        docs_to_record = []
        for filename in json_files:
            filepath = os.path.join(self.results_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 証券コードをファイル名またはメタデータから確定
                code = filename.replace(".json", "")
                data["code"] = code

                # スキーマに合わせたマッピング
                # parser からは net_profit, prev_net_profit, shares_outstanding, total_assets, net_assets, eps, bps, dps などが来ている
                record = {
                    "code": code,
                    "net_profit": data.get("net_profit"),
                    "prev_net_profit": data.get("prev_net_profit"),
                    "shares_outstanding": data.get("shares_outstanding"),
                    "sales": data.get("sales"),
                    "operating_income": data.get("operating_income"),
                    "total_assets": data.get("total_assets"),
                    "net_assets": data.get("net_assets"),
                    "eps": data.get("eps"),
                    "bps": data.get("bps"),
                    "dps": data.get("dps"),
                }

                # operating_margin の計算 (パルサー側でやっていない場合の補填)
                if (
                    record["sales"]
                    and record["operating_income"]
                    and record["sales"] > 0
                ):
                    record["operating_margin"] = (
                        record["operating_income"] / record["sales"]
                    ) * 100.0

                data_to_upsert.append(record)

                doc_id = data.get("doc_id")
                if doc_id:
                    docs_to_record.append(
                        {
                            "doc_id": doc_id,
                            "code": code,
                            "doc_type": data.get("doc_type"),
                            "submit_date": data.get("submit_date"),
                            "is_annual": data.get("is_annual", False),
                        }
                    )

            except Exception as e:
                self.logger.error(f"❌ Failed to load {filename}: {e}")

        if data_to_upsert:
            try:
                # [v27.3] 永続化前の最終修復プロトコル (#1-#5, #6)
                # リストを一度 Polars DF に変換して一括修復し、再度リストに戻す
                df_to_upsert = pl.DataFrame(data_to_upsert)
                df_repaired = FinancialRepairService.repair(df_to_upsert)
                repaired_records = df_repaired.to_dicts()

                self.repository.upsert(repaired_records)

                # DuckDB の edinet_documents テーブルへ処理済みメタデータを記録
                duck_repo = getattr(self.repository, "duck_repo", None)
                if duck_repo and hasattr(duck_repo, "record_edinet_document"):
                    for d in docs_to_record:
                        duck_repo.record_edinet_document(
                            doc_id=d["doc_id"],
                            code=d["code"],
                            doc_type=d["doc_type"],
                            submit_date=d["submit_date"],
                            is_annual=d["is_annual"],
                        )

                self.logger.info(
                    f"✅ Successfully bridged and repaired {len(repaired_records)} records, recorded {len(docs_to_record)} docs."
                )

                if purge_after:
                    self._cleanup_results(json_files)

                return len(data_to_upsert)
            except Exception as e:
                self.logger.error(f"❌ Batch UPSERT failed: {e}")
                return 0

        return 0

    def _cleanup_results(self, filenames: List[str]):
        """処理済みのファイルをアーカイブまたは削除する"""
        for f in filenames:
            try:
                os.remove(os.path.join(self.results_dir, f))
            except Exception:
                pass
        self.logger.info("🧹 Cleaned up result JSONs.")
