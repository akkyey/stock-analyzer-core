from pathlib import Path

import pandas as pd
import requests

from src.utils import rotate_file_backup

from .base import FetcherBase


class JPXFetcher(FetcherBase):
    def fetch_jpx_list(self, fallback_on_error=False, save_to_csv=True):
        """JPXリスト取得"""
        print("📥 JPXリストをダウンロード中...", flush=True)

        # 安全なconfig取得 (.get() で KeyError を防止)
        data_config = self.config.get("data", {})
        jp_stock_path = Path(
            data_config.get("jp_stock_list", "data/input/jp_stock_list.csv")
        )

        # Ensure dir exists (pathlib)
        if jp_stock_path.parent:
            jp_stock_path.parent.mkdir(parents=True, exist_ok=True)

        if save_to_csv and jp_stock_path.exists():
            print("   📦 Rotating backup...", flush=True)
            rotate_file_backup(str(jp_stock_path))

        # [v18.0] ローカルキャッシュ優先
        local_df = self.load_local_data()
        if not local_df.empty:
            print("   ✨ Using latest JPX list from Local cache.", flush=True)
            return local_df

        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"

        # Session setup for User-Agent
        print(
            "   🔧 No local data. Initializing Session for direct download...",
            flush=True,
        )
        try:
            # [Optimization] Use context manager regarding requests.Session (Horizontal Rollout)
            with requests.Session() as session:
                session.headers.update(
                    {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    }
                )

                print("   ⏳ Connecting to JPX directly...", flush=True)
                resp = session.get(url, timeout=30)
                resp.raise_for_status()

                import io

                print(f"   📥 Downloading {len(resp.content)} bytes...", flush=True)
                df = pd.read_excel(io.BytesIO(resp.content))

                # [Optimization] 個別株のみに絞り込む
                df = df[df["33業種区分"] != "-"]

                df = df[["コード", "銘柄名", "33業種区分", "市場・商品区分"]]
                df.columns = ["code", "name", "sector", "market"]
                df["code"] = df["code"].astype(str).str[:4]

                # 市場区分名の正規化
                def normalize_market(m):
                    if pd.isna(m):
                        return "Unknown"
                    m_str = str(m)
                    if "プライム" in m_str:
                        return "Prime"
                    if "スタンダード" in m_str:
                        return "Standard"
                    if "グロース" in m_str:
                        return "Growth"
                    return "Other"

                df["market"] = df["market"].apply(normalize_market)

                if save_to_csv:
                    print(f"   💾 Saving to {jp_stock_path}", flush=True)
                    df.to_csv(jp_stock_path, index=False, encoding="utf-8-sig")

                print(f"✅ JPXリスト取得完了: {len(df)} 銘柄", flush=True)
                return df

        except Exception as e:
            self.logger.error(f"❌ Failed to download JPX list: {e}")
            if not fallback_on_error:
                raise e

            self.logger.warning("⚠️ Fallback enabled. Searching backup...")
            search_dir_path = jp_stock_path.parent
            candidates = []

            # Using Path.glob (PTH207)
            # patterns were strings, need to glob against the directory
            for pattern_str in ["stock_master_*.csv", "jp_stock_list_*.csv"]:
                candidates.extend(str(p) for p in search_dir_path.glob(pattern_str))

            if candidates:
                latest = sorted(candidates)[-1]
                self.logger.warning(f"🔄 Using backup: {latest}")
                try:
                    df = pd.read_csv(latest)
                    if "コード" in df.columns:
                        col_map = {
                            "コード": "code",
                            "銘柄名": "name",
                            "33業種区分": "sector",
                            "市場・商品区分": "market",
                        }
                        df.rename(columns=col_map, inplace=True)
                    if "code" in df.columns:
                        df["code"] = (
                            df["code"].astype(str).str.replace(r"\.0$", "", regex=True)
                        )
                    return df
                except Exception as ie:
                    self.logger.error(f"Backup load failed: {ie}")
                    raise ie from e
            else:
                raise FileNotFoundError("No backup found.") from e

    def load_local_data(self):
        """ローカルのJPXリストがあれば読み込む (キャッシュ利用)"""
        data_config = self.config.get("data", {})
        jp_stock_path = Path(
            data_config.get("jp_stock_list", "data/input/jp_stock_list.csv")
        )

        if jp_stock_path.exists():
            try:
                df = pd.read_csv(jp_stock_path)
                # カラム名の正規化 (バックアップ読み込み時と同じロジック)
                if "コード" in df.columns:
                    col_map = {
                        "コード": "code",
                        "銘柄名": "name",
                        "33業種区分": "sector",
                        "市場・商品区分": "market",
                    }
                    df.rename(columns=col_map, inplace=True)
                if "code" in df.columns:
                    df["code"] = (
                        df["code"].astype(str).str.replace(r"\.0$", "", regex=True)
                    )
                return df
            except Exception as e:
                self.logger.warning(f"⚠️ Failed to load local JPX data: {e}")
                return pd.DataFrame()
        return pd.DataFrame()
