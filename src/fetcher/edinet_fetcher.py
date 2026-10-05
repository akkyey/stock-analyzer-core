import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

import pandas as pd
import requests


class EdinetAuthenticationError(PermissionError):
    """EDINET APIの認証失敗エラー (401/403)"""

    pass


def mask_api_key(text: str) -> str:
    """エラーメッセージやURL、ヘッダー文字列内の API キーを伏字化する"""
    if not text:
        return text
    # 1. URL クエリパラメータ: Subscription-Key=xyz
    text = re.sub(r"(Subscription-Key=)[^&\s'\"]+", r"\1***", text, flags=re.IGNORECASE)
    # 2. ヘッダー / JSON: 'Ocp-Apim-Subscription-Key': 'xyz'
    text = re.sub(
        r"(['\"]?Ocp-Apim-Subscription-Key['\"]?\s*[:=]\s*['\"])[^'\"]+(['\"])",
        r"\g<1>***\2",
        text,
        flags=re.IGNORECASE,
    )
    return text


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

    def _get_headers_and_params(
        self, extra_params: Optional[Dict[str, Any]] = None
    ) -> tuple:
        params = extra_params.copy() if extra_params else {}
        headers = self.DEFAULT_HEADERS.copy()

        if self.api_key:
            params["Subscription-Key"] = self.api_key
            headers["Ocp-Apim-Subscription-Key"] = self.api_key

        return headers, params

    @staticmethod
    def _validate_auth_response(
        status_code: int,
        data: Optional[Dict[str, Any]] = None,
        text: Optional[str] = None,
    ) -> None:
        """EDINET API の認証エラーを厳密に判定し、検知時は EdinetAuthenticationError を発生させる。

        EDINET API v2 は無効な API キーに対して HTTP 200 で
        {"StatusCode": 401, "message": "Access denied due to invalid subscription key..."}
        を返すため、HTTP ステータスコードだけでなくレスポンスボディのトップレベル StatusCode / statusCode も検証する。
        """
        if status_code in (401, 403):
            raise EdinetAuthenticationError(
                f"EDINET APIキーの認証に失敗しました (HTTP {status_code})。APIキーを確認してください。"
            )

        if data and isinstance(data, dict):
            # 1. トップレベル StatusCode / statusCode (EDINET v2 API の実応答)
            body_code = data.get("StatusCode") or data.get("statusCode")
            if str(body_code) in ("401", "403"):
                msg = data.get("message", "Invalid subscription key")
                raise EdinetAuthenticationError(
                    f"EDINET APIキーの認証に失敗しました (StatusCode {body_code}: {msg})。APIキーを確認してください。"
                )

            # 2. metadata.status
            meta_status = (
                data.get("metadata", {}).get("status")
                if isinstance(data.get("metadata"), dict)
                else None
            )
            if str(meta_status) in ("401", "403"):
                raise EdinetAuthenticationError(
                    f"EDINET APIキーの認証に失敗しました (metadata status {meta_status})。APIキーを確認してください。"
                )

            # 3. message 内の認証拒否キーワード
            msg_str = str(data.get("message", "")).lower()
            if "access denied" in msg_str or "invalid subscription key" in msg_str:
                raise EdinetAuthenticationError(
                    f"EDINET APIキーの認証に失敗しました ({data.get('message')})。APIキーを確認してください。"
                )

        if text:
            text_lower = text.lower()
            if (
                "access denied due to invalid subscription key" in text_lower
                or '"statuscode": 401' in text_lower
                or '"statuscode": 403' in text_lower
            ):
                raise EdinetAuthenticationError(
                    "EDINET APIキーの認証に失敗しました (レスポンスに認証拒否が含まれています)。APIキーを確認してください。"
                )

    def fetch_documents_by_date(self, date_str: str) -> Dict[str, Any]:
        """指定日の書類一覧を取得する (v2 API)"""
        url = f"{self.BASE_URL}/documents.json"
        headers, params = self._get_headers_and_params({"date": date_str, "type": 2})

        self.logger.info(f"🌐 Fetching EDINET documents for {date_str}...")
        max_retries = 3
        backoff_sec = 2.0

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=30)
                self._validate_auth_response(response.status_code, text=response.text)

                # 一時的な 429 または 5xx サーバーエラーの場合はリトライ
                if (
                    response.status_code in (429, 500, 502, 503, 504)
                    and attempt < max_retries
                ):
                    self.logger.warning(
                        f"⚠️ EDINET API returned HTTP {response.status_code} for {date_str}. Retrying in {backoff_sec:.1f}s (attempt {attempt}/{max_retries})..."
                    )
                    time.sleep(backoff_sec)
                    backoff_sec *= 2.0
                    continue

                response.raise_for_status()
                data = cast(Dict[str, Any], response.json())
                self._validate_auth_response(response.status_code, data=data)
                return data
            except EdinetAuthenticationError:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries:
                    self.logger.warning(
                        f"⚠️ EDINET API connection/timeout error for {date_str}: {e}. Retrying in {backoff_sec:.1f}s (attempt {attempt}/{max_retries})..."
                    )
                    time.sleep(backoff_sec)
                    backoff_sec *= 2.0
                    continue
                else:
                    self.logger.error(
                        f"❌ Failed to fetch EDINET documents for {date_str} after {max_retries} attempts: {e}"
                    )
                    return {"results": [], "failed": True}
            except requests.exceptions.JSONDecodeError as e:
                self.logger.warning(
                    f"⚠️ EDINET API returned non-JSON response for {date_str} (HTTP {response.status_code}): {e}. Content preview: {response.text[:150]}"
                )
                return {"results": [], "failed": True}
            except Exception as e:
                masked_err = mask_api_key(str(e))
                self.logger.error(
                    f"❌ Failed to fetch EDINET documents for {date_str}: {masked_err}"
                )
                return {"results": [], "failed": True}

        return {"results": [], "failed": True}

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

        file_path: Optional[str] = None
        try:
            response = requests.get(
                url, params=params, headers=headers, stream=True, timeout=60
            )
            self._check_download_response(response)

            os.makedirs(save_dir, exist_ok=True)
            file_path = os.path.join(save_dir, f"{doc_id}.zip")
            self._write_download(response, file_path)
            self._check_downloaded_file(response, file_path)
            return file_path
        except EdinetAuthenticationError:
            self._remove_quietly(file_path)
            raise
        except Exception as e:
            self._remove_quietly(file_path)
            masked_err = mask_api_key(str(e))
            raise RuntimeError(
                f"Failed to download XBRL for {doc_id}: {masked_err}"
            ) from None

    def _check_download_response(self, response: Any) -> None:
        """認証エラー (HTTP 401/403、HTTP 200 で返されるエラー JSON) と HTTP エラーを例外にする。"""
        if response.status_code in (401, 403):
            raise EdinetAuthenticationError(
                f"EDINET APIキーの認証に失敗しました (HTTP {response.status_code})。APIキーを確認してください。"
            )

        # Content-Type が JSON の場合（HTTP 200 で返されるエラー JSON を検知）
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type or "text/json" in content_type:
            try:
                data = response.json()
                self._validate_auth_response(response.status_code, data=data)
            except EdinetAuthenticationError:
                raise
            except Exception:
                pass

        response.raise_for_status()

    def _write_download(self, response: Any, file_path: str) -> None:
        """応答を保存する。ストリーミング開始部分が JSON エラー文字列なら認証エラーを検知する。"""
        with open(file_path, "wb") as f:
            for i, chunk in enumerate(response.iter_content(chunk_size=8192)):
                if i == 0:
                    self._check_first_chunk(response, chunk)
                f.write(chunk)

    def _check_first_chunk(self, response: Any, chunk: bytes) -> None:
        stripped = chunk.strip()
        if not (stripped.startswith(b'{"StatusCode"') or stripped.startswith(b'{"statusCode"')):
            return
        try:
            import json

            data = json.loads(chunk.decode("utf-8", errors="ignore"))
            self._validate_auth_response(response.status_code, data=data)
        except EdinetAuthenticationError:
            raise
        except Exception:
            pass

    def _check_downloaded_file(self, response: Any, file_path: str) -> None:
        """ファイルサイズが小さく、有効な ZIP ヘッダー (PK\\x03\\x04) でない場合も検証"""
        if not os.path.exists(file_path) or os.path.getsize(file_path) >= 1024:
            return
        with open(file_path, "rb") as test_f:
            header_bytes = test_f.read(4)
        if header_bytes != b"PK\x03\x04":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as err_f:
                content_str = err_f.read()
            self._validate_auth_response(response.status_code, text=content_str)

    @staticmethod
    def _remove_quietly(file_path: Optional[str]) -> None:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

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
