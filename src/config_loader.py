import os
import re
from pathlib import Path
from typing import Any

import yaml

from src.config_schema import ConfigModel
from src.constants import CONFIG_PATH
from src.env_loader import load_env_file


class ConfigLoader:
    def __init__(self, config_path: str | None = None):
        # Ensure environment variables are loaded (auto-discovery)
        load_env_file()

        # 定数からデフォルトパスを取得
        self.config_path: str = config_path if config_path else CONFIG_PATH
        # デフォルト環境を最も安全な 'test' に設定 (未指定時の事故防止)
        self.env = os.getenv("STOCK_ENV", "production").lower()
        print(f"🌐 Loaded Project Environment: {self.env.upper()}")

        # Load Defaults + User Config
        self.config = self._load_merged_config_and_validate()

    def _load_merged_config_and_validate(self) -> dict[str, Any]:
        """設定ロードとバリデーション実行。後方互換性のため辞書を返す。"""
        # 1. Load Defaults
        defaults = self._load_defaults()

        # 2. Load User Config
        user_config = self._load_user_config()

        # 3. Merge (User overrides Defaults)
        if not user_config:
            self.raw_config = defaults
        else:
            self.raw_config = self._recursive_merge(defaults, user_config)

        # [Safety] If NOT production env, override critical paths to avoid polluting production
        if self.env != "production":
            self._apply_test_overrides()

        # Macro sync before validation
        self._sync_macro_context()

        # Pydantic Validation
        self.config_model = self.validate_config()

        # [v12.6] Smart Directory Setup (Ensure output dirs exist)
        self._ensure_directories()

        # [v12.8] Inject runtime info into dict for backward compatibility/verification
        config_dict = self.config_model.model_dump()
        config_dict["env"] = self.env
        return config_dict

    def _load_defaults(self) -> dict[str, Any]:
        """config/defaults.yaml を読み込む"""
        # Resolving paths:
        # 1. Module location based: src/../config/defaults.yaml (works if installed as package)
        # 2. CWD based (works if running from repo root)

        module_dir = Path(__file__).resolve().parent  # stock-analyzer4/src
        repo_root_sub = module_dir.parent  # stock-analyzer4
        repo_root_main = repo_root_sub.parent  # project-stock2 (if submodule)

        candidates = [
            repo_root_sub / "config" / "defaults.yaml",
            repo_root_main / "stock-analyzer4" / "config" / "defaults.yaml",
            # Fallback relative to CWD
            Path("stock-analyzer4/config/defaults.yaml"),
            Path("config/defaults.yaml"),
        ]

        for p in candidates:
            if p.exists():
                try:
                    with open(p, encoding="utf-8") as f:  # noqa: PTH123
                        print(f"✅ Loaded defaults from: {p.resolve()}")
                        return yaml.safe_load(f) or {}
                except Exception as e:
                    print(f"⚠️ Failed to load defaults from {p}: {e}")

        print(
            f"⚠️ defaults.yaml not found. Checked locations: {[str(c) for c in candidates]}"
        )
        return {}

    def _load_user_config(self) -> dict[str, Any]:
        """ユーザー指定の設定ファイルを読み込む"""
        paths_to_try = [
            Path(self.config_path),
            Path("stock-analyzer4") / self.config_path,
        ]

        actual_path = None
        for p in paths_to_try:
            if p.exists():
                actual_path = p
                break

        if not actual_path:
            # If explicit path was given but missing, warn.
            # If default CONFIG_PATH missing, it's okay if defaults exist.
            print(
                f"ℹ️ User config file not found in: {paths_to_try}. Using defaults only."
            )
            return {}

        self.config_path = actual_path
        try:
            with open(actual_path, encoding="utf-8") as f:  # noqa: PTH123
                print(f"✅ Loaded user config from: {actual_path.resolve()}")
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"❌ Error loading user config: {e}")
            return {}

    def _recursive_merge(self, base: dict, override: dict) -> dict:
        """辞書を再帰的にマージする"""
        result = base.copy()
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._recursive_merge(result[k], v)
            else:
                result[k] = v
        return result

    def _ensure_directories(self):
        """設定されたパスに基づいて必要なディレクトリを自動生成する (自律的パス管理)"""
        # 1. Outputディレクトリの作成
        # data section should not be None based on defaults, but safe check is good
        data_cfg = self.raw_config.get("data") or {}
        output_path = data_cfg.get("output_path")
        if output_path:
            out_dir = Path(output_path).parent
            if out_dir and not out_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                print(f"📁 Created directory: {out_dir}")

        # 2. paths.output_dir の作成
        paths_cfg = self.raw_config.get("paths") or {}
        paths_out = paths_cfg.get("output_dir")
        if paths_out:
            paths_out_path = Path(paths_out)
            if not paths_out_path.exists():
                paths_out_path.mkdir(parents=True, exist_ok=True)
                print(f"📁 Created directory: {paths_out}")

        # 3. データベースディレクトリの作成
        db_file = paths_cfg.get("db_file")
        if db_file and db_file != ":memory:":
            db_dir = Path(db_file).parent
            if db_dir and not db_dir.exists():
                db_dir.mkdir(parents=True, exist_ok=True)
                print(f"📁 Created directory: {db_dir}")

    def _apply_test_overrides(self):
        """テスト・開発環境用のオーバーライド設定（明示的な指示がある場合のみ隔離）"""

        # 以前は env != production で強制的に /tmp に飛ばしていたが、
        # ユーザーの混乱を招くため、明示的な環境変数がある場合のみに限定する。

        # DBの隔離 (環境変数が指定されている場合のみ。それ以外は config.yaml を尊重)
        test_db = os.getenv("STOCK_TEST_DB_PATH")
        if test_db:
            print(
                f"🛡️ [DATA GUARD] DB Path redirected by environment variable: {test_db}"
            )
            if "paths" not in self.raw_config or self.raw_config["paths"] is None:
                self.raw_config["paths"] = {}
            self.raw_config["paths"]["db_file"] = test_db

        # 出力ディレクトリの隔離
        test_out = os.getenv("STOCK_TEST_OUTPUT_PATH")
        if test_out:
            print(
                f"🛡️ [DATA GUARD] Output Dir redirected by environment variable: {test_out}"
            )
            if "paths" not in self.raw_config:
                self.raw_config["paths"] = {}
            self.raw_config["paths"]["output_dir"] = test_out
            if "data" not in self.raw_config:
                self.raw_config["data"] = {}
            self.raw_config["data"]["output_path"] = str(Path(test_out) / "result.csv")

        # [v20.9] Production 認定をより明示的に（混乱回避）
        is_prod = self.env in ["production", "prod"]
        if not is_prod:
            # 共有ドライブ連携などの「本番のみのフラグ」をデフォルトOFF
            if "gdrive" not in self.raw_config:
                self.raw_config["gdrive"] = {"enabled": False}

        print(
            f"🔒 [ENV] {self.env.upper()} (DB: {self.raw_config.get('paths', {}).get('db_file')})"
        )

    def validate_config(self) -> ConfigModel:
        """ConfigをPydanticモデルで検証する"""
        try:
            model = ConfigModel(**self.raw_config)
            return model
        except Exception as e:
            print(f"❌ Config Validation Failed: {e}")
            raise ValueError(f"Invalid Configuration: {e}") from e

    def _sync_macro_context(self) -> None:
        """market_context.txt からマクロ環境設定を読み込み Config をオーバーライドする"""
        config_dir = Path(self.config_path).parent
        context_path = config_dir / "market_context.txt"
        if not context_path.exists():
            return

        try:
            with open(context_path, encoding="utf-8") as f:  # noqa: PTH123
                content = f.read()

            if (
                "scoring_v2" not in self.raw_config
                or self.raw_config["scoring_v2"] is None
            ):
                self.raw_config["scoring_v2"] = {}

            if "macro" not in self.raw_config["scoring_v2"]:
                self.raw_config["scoring_v2"]["macro"] = {}

            macro_cfg = self.raw_config["scoring_v2"]["macro"]

            match_sentiment = re.search(r"\[MACRO_SENTIMENT:([a-zA-Z0-9_]+)\]", content)
            if match_sentiment:
                val = match_sentiment.group(1).lower()
                macro_cfg["sentiment"] = val

            match_rate = re.search(r"\[INTEREST_RATE:([a-zA-Z0-9_]+)\]", content)
            if match_rate:
                val = match_rate.group(1).lower()
                macro_cfg["interest_rate"] = val

            match_sector = re.search(r"\[ACTIVE_SECTOR:([^\]]+)\]", content)
            if match_sector:
                val = match_sector.group(1).strip()
                macro_cfg["active_sector"] = val

        except Exception as e:
            print(f"⚠️ Failed to sync macro context: {e}")


# --- 旧コードとの互換性用 ---
def load_config(config_path=None):
    loader = ConfigLoader(config_path)
    return loader.config
