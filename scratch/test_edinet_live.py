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

def main():
    ef = EdinetFetcher({})
    print("🔑 EDINET_API_KEY Present:", bool(ef.api_key))
    target_date = "2024-06-25"
    data = ef.fetch_documents_by_date(target_date)
    results = data.get("results", [])
    print(f"✅ Date {target_date}: {len(results)} documents fetched successfully!")
    if results:
        print(f"📄 Sample document: {results[0].get('docDescription')} ({results[0].get('filerName')})")

if __name__ == "__main__":
    main()
