# Scoring Constants and Defaults
from pathlib import Path

# ============================================================
# 設定パス定数 (Config Path Constants)
# すべての設定ファイルパスはここで定義し、各モジュールはこの定数を参照する
# ============================================================
_PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = _PROJECT_ROOT / "config"

CONFIG_PATH = str(CONFIG_DIR / "config.yaml")

# Google Drive 連携用定数
GDRIVE_ROOT_FOLDER_NAME = "StockAnalyzer_Prod"
GDRIVE_REPORTS_SUBFOLDER = "reports"

# Default Log File
DEFAULT_LOG_FILE = str(_PROJECT_ROOT / "stock_analyzer.log")

