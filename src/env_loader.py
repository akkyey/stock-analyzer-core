import logging
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def load_env_file():
    """
    Find and load .env file from predetermined paths or parent directories.
    """
    import os

    # 1. Environment variable override
    env_path_custom = os.getenv("STOCK_ENV_PATH")
    if env_path_custom and Path(env_path_custom).exists():
        logger.info(f"🔑 Loading .env from STOCK_ENV_PATH: {env_path_custom}")
        load_dotenv(env_path_custom, override=False)
        return True

    # 2. Search up from current file's project root
    # Starting from current directory
    current_dir = Path.cwd().resolve()

    # Candidate search depth: up to 3 levels (./, ../, ../../)
    for _ in range(4):
        candidate = current_dir / ".env"
        if candidate.exists():
            logger.info(f"🔑 Found .env at: {candidate}")
            load_dotenv(str(candidate), override=False)
            return True

        # Also check common subdirs in parents if needed, but standard is parent root
        # Move up
        if current_dir.parent == current_dir:
            break
        current_dir = current_dir.parent

    logger.debug(
        "ℹ️ No .env file found in search paths (this is normal if using Colab Secrets or Env Vars)."
    )
    return False


def get_stock_config_env() -> str | None:
    """
    Get STOCK_CONFIG environment variable (AP-001 Refactoring).
    Centralizes access to the configuration file path environment variable.
    """
    import os

    return os.environ.get("STOCK_CONFIG")


def get_stock_env() -> str:
    """
    Get the current STOCK_ENV value.
    Defaults to 'development' if not set.
    """
    import os

    return os.environ.get("STOCK_ENV", "production")


def validate_required_env() -> None:
    """
    必須の環境変数が設定されているかバリデーションを行う。
    不足している場合は、プログラムの不具合やデータ不足を防ぐためにエラーを送出する。
    """
    import os

    required_vars = [
        "GEMINI_API_KEY",
        "DISCORD_WEBHOOK_URL",
    ]

    missing = []
    for var in required_vars:
        val = os.environ.get(var)
        if not val or val.strip() == "":
            missing.append(var)

    if missing:
        error_msg = (
            f"⚠️ 必須環境変数が設定されていません: {', '.join(missing)}\n"
            "AI機能を利用する場合は .env ファイルまたは環境変数の設定を確認してください。"
        )
        logger.warning(error_msg)
        # 実行を継続させるために raise は行わない

    logger.debug("✅ All required environment variables are set.")
