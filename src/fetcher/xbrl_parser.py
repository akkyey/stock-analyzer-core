import os
import zipfile
import lxml.etree as et
import logging
from typing import Dict, Any, Optional


class XbrlParser:
    """EDINET XBRL パーサー (Enhanced for Turbo Fidelity)"""

    # 主要なタクソノミ名前空間とタグ名のマッピング (J-GAAP想定)
    TAGS = {
        "sales": ["NetSales", "OperatingRevenue", "Revenue"],
        "operating_income": ["OperatingIncome", "OperatingProfit"],
        "net_income": [
            "NetIncome",
            "ProfitLossAttributableToOwnersOfParent",
            "ProfitLoss",
        ],
        "total_assets": ["TotalAssets", "Assets"],
        "net_assets": ["NetAssets", "Equity"],
        "shares_outstanding": ["OrdinarySharesNumber"], # PER修復用
    }

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def parse_zip(self, zip_path: str) -> Dict[str, Any]:
        """ダウンロードした ZIP 内の XBRL 本体を探してパースする"""
        results = {}
        with zipfile.ZipFile(zip_path, "r") as z:
            # .xbrl ファイルを探す (PublicDoc/ 内にあることが多い)
            xbrl_files = [f for f in z.namelist() if f.endswith(".xbrl")]
            if not xbrl_files:
                self.logger.warning(f"⚠️ No XBRL file found in {zip_path}")
                return results

            # 最大のファイルを本番データとみなす (簡易判定)
            target = max(xbrl_files, key=lambda f: z.getinfo(f).file_size)
            with z.open(target) as f:
                content = f.read()
                results = self.parse_content(content)

        return results

    def parse_content(self, content: bytes) -> Dict[str, Any]:
        """XML 文字列をパースして数値を抽出する (Enhanced for Turbo)"""
        try:
            tree = et.fromstring(content)
        except Exception as e:
            self.logger.warning(f"⚠️ XML parse failed: {e}")
            return {}

        ns = tree.nsmap
        extracted = {}

        # コンテキストの定義 (EDINET XBRL 準拠)
        # Instant(時点: 貸借対照表項目), Duration(期間: 損益計算書項目)
        CONTEXTS = {
            "current_i": "CurrentYearInstant",
            "current_d": "CurrentYearDuration",
            "prior_i": "Prior1YearInstant",
            "prior_d": "Prior1YearDuration",
        }

        for key, possible_tags in self.TAGS.items():
            # タグ候補をループ
            for tag in possible_tags:
                elements = tree.xpath(f"//*[local-name()='{tag}']", namespaces=ns)
                if not elements:
                    continue

                # 各コンテキストの値を探索
                for label, target_ctx in CONTEXTS.items():
                    # net_income の場合のみ、current/prior を別キーで保持（比較用）
                    store_key = f"{key}_{label}" if key == "net_income" else key
                    
                    # 既に現在のタグ・コンテキストで値が見つかっているなら最優先を保持
                    if store_key in extracted:
                        continue

                    for el in elements:
                        ctx_ref = el.get("contextRef", "")
                        if target_ctx in ctx_ref:
                            val_str = el.text
                            if val_str and val_str.strip():
                                try:
                                    val = float(val_str.strip())
                                    extracted[store_key] = val
                                    break
                                except ValueError:
                                    continue
                
                # 主要項目で値が埋まったら次のキーへ
                if key in extracted:
                    break
        
        # 後方互換性および計算層への橋渡し
        # 損益計算書の CurrentYearDuration を優先
        if "net_income_current_d" in extracted:
            extracted["net_profit"] = extracted["net_income_current_d"]
            extracted["prev_net_profit"] = extracted.get("net_income_prior_d")
        
        return extracted
