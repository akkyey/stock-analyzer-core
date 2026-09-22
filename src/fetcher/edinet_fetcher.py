import os
import requests
import pandas as pd
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any


class EdinetFetcher:
    """EDINET API v2 連携フェッチャー (v28.4 Draft)"""

    BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.doc_list_path = config.get("paths", {}).get(
            "edinet_code_csv", "data/EDINET/EdinetcodeDlInfo.csv"
        )
        self.api_key = os.getenv("EDINET_API_KEY")

    def _get_headers_and_params(self, extra_params: Dict[str, Any] = None) -> tuple:
        params = extra_params.copy() if extra_params else {}
        headers = self.DEFAULT_HEADERS.copy()

        if self.api_key:
            params["Subscription-Key"] = self.api_key
            headers["Ocp-Apim-Subscription-Key"] = self.api_key

        return headers, params

    def fetch_documents_by_date(self, date_str: str) -> Dict[str, Any]:
        """指定日の書類一覧を取得する (v2 API)"""
        url = f"{self.BASE_URL}/documents.json"
        headers, params = self._get_headers_and_params({"date": date_str, "type": 2})

        self.logger.info(f"🌐 Fetching EDINET documents for {date_str}...")
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError as e:
            self.logger.warning(
                f"⚠️ EDINET API returned non-JSON response for {date_str} (HTTP {response.status_code}): {e}. Content preview: {response.text[:150]}"
            )
            return {"results": []}
        except Exception as e:
            self.logger.error(f"❌ Failed to fetch EDINET documents for {date_str}: {e}")
            return {"results": []}

    def parse_code_listing(self) -> Dict[str, str]:
        """EdinetcodeDlInfo.csv を解析して {証券コード(4桁): EDINETコード} のマップを返す"""
        if not os.path.exists(self.doc_list_path):
            self.logger.warning(
                f"⚠️ EDINET code listing not found at {self.doc_list_path}"
            )
            return {}

        self.logger.info(f"📂 Parsing EDINET code listing: {self.doc_list_path}")
        # 1行目はメタデータなので skip
        df = pd.read_csv(self.doc_list_path, encoding="shift_jis", skiprows=1)

        mapping = {}
        for _, row in df.iterrows():
            # 上場区分が "上場" 且つ 証券コードがあるもの
            if row["上場区分"] == "上場" and not pd.isna(row["証券コード"]):
                raw_code = str(row["証券コード"]).strip()
                # 5桁（末尾にチェックデジット等がある場合）は4桁にする
                if len(raw_code) == 5:
                    clean_code = raw_code[:4]
                else:
                    clean_code = raw_code

                edinet_code = row["ＥＤＩＮＥＴコード"]
                mapping[clean_code] = edinet_code

        self.logger.info(f"✅ Mapped {len(mapping)} EDINET codes.")
        return mapping

    def download_xbrl(self, doc_id: str, save_dir: str) -> str:
        """書類(XBRL)をダウンロードし、一時ディレクトリに保存する"""
        url = f"{self.BASE_URL}/documents/{doc_id}"
        headers, params = self._get_headers_and_params({"type": 1})  # type 1 = XBRL zip

        response = requests.get(
            url, params=params, headers=headers, stream=True, timeout=60
        )
        response.raise_for_status()

        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, f"{doc_id}.zip")
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return file_path

    def backfill_scan(self, days: int = 365) -> List[Dict[str, Any]]:
        """過去 N 日分の書類をスキャンし、対象となる有報・四半報のメタデータリストを返す"""
        target_docs = []
        end_date = datetime.now()

        for i in range(days):
            date_str = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                data = self.fetch_documents_by_date(date_str)
                results = data.get("results", [])

                for doc in results:
                    # 有価証券報告書 (120) または 四半期報告書 (140)
                    if doc.get("docTypeCode") in ["120", "140"] and doc.get("secCode"):
                        # secCode は 5桁 (13760) なので 4桁にする
                        clean_code = doc.get("secCode")[:4]
                        doc["clean_code"] = clean_code
                        target_docs.append(doc)

                import time

                time.sleep(1.0)  # Rate limit

            except Exception as e:
                self.logger.error(f"❌ Error at {date_str}: {e}")

        return target_docs
