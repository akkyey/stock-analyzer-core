# Scoring Constants and Defaults
from pathlib import Path

# ============================================================
# 設定パス定数 (Config Path Constants)
# すべての設定ファイルパスはここで定義し、各モジュールはこの定数を参照する
# ============================================================
_PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = _PROJECT_ROOT / "config"

CONFIG_PATH = str(CONFIG_DIR / "config.yaml")
AI_PROMPTS_PATH = str(CONFIG_DIR / "ai_prompts.yaml")
THRESHOLDS_PATH = str(CONFIG_DIR / "thresholds.yaml")
MARKET_CONTEXT_PATH = str(CONFIG_DIR / "market_context.txt")

# Google Drive 連携用定数
GDRIVE_ROOT_FOLDER_NAME = "StockAnalyzer_Prod"
GDRIVE_REPORTS_SUBFOLDER = "reports"

# Default Log File (AP-004 fix)
DEFAULT_LOG_FILE = str(_PROJECT_ROOT / "stock_analyzer.log")

# Metric Categories for Breakdown
METRIC_CATEGORY = {
    # Value
    "per": "value",
    "pbr": "value",
    "dividend_yield": "value",
    "peg_ratio": "value",
    # Quality
    "roe": "quality",
    "equity_ratio": "quality",
    "operating_cf": "quality",
    "payout_ratio": "quality",
    # Growth
    "sales_growth": "growth",
    "profit_growth": "growth",
    # Trend (Technical)
    "rsi_oversold": "trend",
    "rsi_overbought": "trend",
    "macd_bullish": "trend",
    "trend_up": "trend",
    "bb_p1sig": "trend",
    "bb_p2sig": "trend",
    "bb_m1sig": "trend",
    "bb_m2sig": "trend",
}

# Metrics where lower value is better
LOWER_IS_BETTER_DEFAULTS = ["per", "pbr", "debt_equity_ratio", "peg_ratio"]

# Scoring Defaults
DEFAULT_BASE_SCORE = 50.0
DEFAULT_SCORING_V2_STYLES = {
    "value_balanced": {"weight_fund": 0.7, "weight_tech": 0.3},
    "short_term_momentum": {"weight_fund": 0.3, "weight_tech": 0.7},
    "long_term_growth": {"weight_fund": 0.8, "weight_tech": 0.2},
}

# Repair Confidence Scores [v16.0]
REPAIR_CONF_PRIMARY = 1.00
REPAIR_CONF_SECONDARY = 0.85
REPAIR_CONF_FALLBACK = 0.60
REPAIR_CONF_FAILURE = 0.00

# Technical Bonus Points [v26.15]
TECH_BONUS_POINTS = {
    "macd_bullish": 5.0,
    "rsi_oversold": 5.0,
    "rsi_overbought": -5.0,
    "trend_up": 5.0,
}
