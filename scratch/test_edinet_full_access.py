import os
import sys
import logging
import dotenv

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

dotenv.load_dotenv(os.path.join(project_root, ".env"))
logging.basicConfig(level=logging.INFO)

from src.fetcher.edinet_fetcher import EdinetFetcher
from src.fetcher.xbrl_parser import XbrlParser

def test_full_edinet_access():
    print("=== 1. EDINET API Key 確認 ===")
    ef = EdinetFetcher({})
    print("🔑 EDINET_API_KEY 付与状態:", "OK (キー設定済み)" if ef.api_key else "NG (キーなし)")

    print("\n=== 2. 書類一覧取得 (documents.json) ===")
    target_date = "2024-06-25"
    data = ef.fetch_documents_by_date(target_date)
    results = data.get("results", [])
    print(f"📡 取得結果: {len(results):,} 件の提出書類を取得")

    target_doc = None
    for doc in results:
        if doc.get("docTypeCode") == "120" and doc.get("secCode"):
            target_doc = doc
            break

    if not target_doc:
        print("⚠️ 有価証券報告書が見つかりませんでした。")
        return

    doc_id = target_doc.get("docID") or target_doc.get("docId")
    filer_name = target_doc.get("filerName")
    sec_code = target_doc.get("secCode")[:4]
    doc_desc = target_doc.get("docDescription")

    print(f"\n=== 3. 実書類 (XBRL zip) ダウンロードテスト ===")
    print(f"📄 対象: {filer_name} (コード: {sec_code})")
    print(f"🆔 書類ID: {doc_id} ({doc_desc})")

    tmp_dir = os.path.join(project_root, "scratch/edinet_test_dl")
    zip_path = ef.download_xbrl(doc_id, tmp_dir)
    file_size_kb = os.path.getsize(zip_path) / 1024
    print(f"💾 ダウンロード成功: {zip_path} ({file_size_kb:.1f} KB)")

    print("\n=== 4. XBRL パース解析テスト ===")
    parser = XbrlParser()
    parsed_data = parser.parse_zip(zip_path)
    print("📊 抽出された財務数値:")
    for k, v in parsed_data.items():
        if v is not None:
            print(f"   - {k}: {v:,}")

    print("\n🎉 EDINET API 全工程（一覧取得 → XBRLダウンロード → 財務データ解析）のアクセスが【完全成功】しました！")

if __name__ == "__main__":
    test_full_edinet_access()
