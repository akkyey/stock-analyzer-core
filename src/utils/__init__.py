import hashlib
import math
import shutil
import time
import warnings
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from typing import Any

import pandas as pd

# JST Definition
JST = timezone(timedelta(hours=9), "JST")


class DateManager:
    """日付形式の正規化とバリデーションを統括するクラス。"""

    @staticmethod
    def normalize(val: Any) -> str:
        """
        様々な形式の日付・時刻データを 'YYYY-MM-DD' 文字列に正規化する。
        - pd.Timestamp, datetime: .strftime('%Y-%m-%d')
        - str: 複数の形式（ISO-8601, '/'区切り, ミリ秒付等）を処理
        - None/NaN: 空文字を返す
        """
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return ""

        # pd.Timestamp / datetime 対応
        if hasattr(val, "strftime"):
            return str(val.strftime("%Y-%m-%d"))

        # 文字列対応
        s = str(val).strip()
        if not s:
            return ""

        try:
            # pd.to_datetime を使用して柔軟にパース
            # [v21.8] 警告抑制のため errors='coerce' を明示
            dt = pd.to_datetime(s, errors="coerce")
            if pd.notna(dt) and hasattr(dt, "strftime"):
                return str(dt.strftime("%Y-%m-%d"))
        except (ValueError, TypeError):
            pass

        # フォールバック: 最初の10文字から抽出（YYYY-MM-DD 形式を想定）
        if len(s) >= 10:
            candidate = s[:10].replace("/", "-")
            if candidate.count("-") == 2:
                # 数字とハイフンの妥当性を簡易チェック
                if all(c.isdigit() or c == "-" for c in candidate):
                    return candidate

        # [v21.9] 解釈不能な場合は空文字を返し、DB側のバリデーションエラーを防ぐ
        return ""

    @staticmethod
    def is_valid(val: str) -> bool:
        """形式が YYYY-MM-DD かつ妥当な日付かチェックする。"""
        if not isinstance(val, str) or len(val) != 10:
            return False
        try:
            datetime.strptime(val, "%Y-%m-%d")
            return True
        except ValueError:
            return False


def get_current_time() -> datetime:
    """Returns the current time in JST (Always offset-aware)."""
    return datetime.now(JST)


def get_today_str() -> str:
    """Returns today's date string (YYYY-MM-DD) in JST."""
    return DateManager.normalize(get_current_time())


def is_empty(val: Any) -> bool:
    """
    値が None, NaN, または空文字列であるか判定する。

    Args:
        val: 判定対象の値。

    Returns:
        bool: 空（None, NaN, 空文字）であれば True。
    """
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return bool(isinstance(val, float) and math.isnan(val))


def clean_nan_dict(data: dict[str, Any]) -> dict[str, Any]:
    """
    辞書内の NaN 値 (float) を None に変換する。
    辞書がネストしている場合も再帰的に処理する。

    Args:
        data: 変換対象の辞書。

    Returns:
        変換後の辞書。元の辞書は変更しない。
    """
    result: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = clean_nan_dict(v)
        elif isinstance(v, float) and math.isnan(v):
            result[k] = None
        else:
            result[k] = v
    return result


def safe_float(val: Any) -> float:
    """
    NaN、None、カンマ入り文字列等を安全に float に変換する。
    変換不能な場合は 0.0 を返す。

    Args:
        val: 変換対象の値。

    Returns:
        float: 変換後の数値。
    """
    try:
        if val is None:
            return 0.0
        # 文字列からのクレンジング
        s = str(val).replace(",", "").replace("%", "").strip()
        if s == "" or s.lower() == "nan" or s.lower() == "none":
            return 0.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def safe_float_or_none(val: Any) -> float | None:
    """
    安全に float に変換する。変換不能な場合は None を返す。
    スコア計算等で「値が存在しない」と「0」を区別したい場合に使用。

    Args:
        val: 変換対象の値。

    Returns:
        float | None: 変換後の数値、または None。
    """
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").replace("%", "").strip()
        if s == "" or s.lower() == "nan" or s.lower() == "none":
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def ensure_dir(path: str) -> None:
    """
    [DEPRECATED] pathlib.Path().mkdir(parents=True, exist_ok=True) を使用してください。
    ディレクトリが存在しなければ作成する。

    Args:
        path: ディレクトリパス。
    """
    warnings.warn(
        "ensure_dir is deprecated. Use pathlib.Path().mkdir(parents=True, exist_ok=True) instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)


def save_dataframe_to_csv(
    df: pd.DataFrame,
    path: str,
    encoding: str = "utf-8-sig",
    index: bool = False,
    include_timestamp_header: bool = False,
) -> bool:
    """
    DataFrame を CSV ファイルに保存する共通ヘルパー。
    標準的なCSVパーサー(Polars/Pandas)との100%互換性を維持するため、コメント行を挿入せずクリーンに出力する。
    """
    logger = getLogger(__name__)
    import csv

    try:
        file_path = Path(path)
        if file_path.parent:
            file_path.parent.mkdir(parents=True, exist_ok=True)

        now_str = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
        if include_timestamp_header:
            with open(
                file_path, "w", encoding=encoding, newline="", errors="replace"
            ) as f:
                f.write(f"# Generated At: {now_str}\n")
            df.to_csv(
                file_path,
                mode="a",
                index=index,
                encoding=encoding,
                errors="replace",
                quoting=csv.QUOTE_MINIMAL,
            )
        else:
            df.to_csv(
                file_path,
                index=index,
                encoding=encoding,
                errors="replace",
                quoting=csv.QUOTE_MINIMAL,
            )

        logger.info(f"✅ CSV saved: {path} (Generated At: {now_str})")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save CSV: {e}")
        return False


def safe_display_value(val: Any, fallback: str = "-") -> Any:
    """
    表示用に値をクレンジングする。None, NaN, 空文字の場合はフォールバック文字を返す。

    Args:
        val: 変換対象の値。
        fallback: 空の場合に返す文字列。デフォルトは "-"。

    Returns:
        Any: クレンジング後の値。
    """
    if val is None or val == "" or str(val).lower() in ("nan", "none"):
        return fallback
    return val


def generate_row_hash(row: pd.Series) -> str:
    """Generate an MD5 hash from a DataFrame row."""
    keys_to_hash = [
        "code",
        "name",
        "per",
        "pbr",
        "roe",
        "dividend_yield",
        "current_ratio",
        "rsi",
        "quant_score",
    ]
    values = []
    for key in keys_to_hash:
        val = row.get(key)
        if pd.isna(val):
            val_str = "NaN"
        elif isinstance(val, float) and val.is_integer():
            val_str = str(int(val))
        else:
            val_str = str(val)
        values.append(val_str)
    raw_string = "|".join(values)
    return hashlib.md5(raw_string.encode("utf-8")).hexdigest()


def retry_with_backoff(
    max_retries=3, base_delay=1, backoff_factor=2, exceptions=(Exception,)
):
    from functools import wraps

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger = getLogger(__name__)
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    err_msg = str(e)
                    # 429 ResourceExhausted の場合は簡潔に表示 (巨大な JSON メッセージを抑制)
                    is_quota_error = (
                        "429" in err_msg
                        or "ResourceExhausted" in err_msg
                        or "quota" in err_msg.lower()
                    )

                    if attempt == max_retries:
                        if is_quota_error:
                            logger.error(
                                "❌ Max retries reached. Quota still exceeded."
                            )
                        else:
                            logger.error(
                                f"❌ Max retries ({max_retries}) reached. Last error: {err_msg[:200]}..."
                            )
                        raise e

                    wait_time = base_delay * (backoff_factor**attempt)
                    if is_quota_error:
                        logger.warning(
                            f"⚠️ Quota Exceeded (429). Retry {attempt + 1}/{max_retries} in {wait_time}s..."
                        )
                    else:
                        logger.warning(
                            f"⚠️ Attempt {attempt + 1} failed: {err_msg[:100]}... Retrying in {wait_time}s..."
                        )
                    time.sleep(wait_time)

        return wrapper

    return decorator


# [追加] ファイルのローテーション・バックアップ機能
def rotate_file_backup(file_path: str) -> None:
    """
    ファイルが存在する場合、更新日時に基づいてリネーム退避させる。
    形式: filename_YYYYMMDD_HHMM.csv (同刻重複時は _01, _02 などを付与)
    """
    path = Path(file_path)
    if not path.exists():
        return

    try:
        # ファイルの最終更新日時（mtime）を取得
        mtime = datetime.fromtimestamp(path.stat().st_mtime, JST)
        timestamp = mtime.strftime("%Y%m%d_%H%M")
    except Exception:
        timestamp = get_current_time().strftime("%Y%m%d_%H%M")

    new_name = f"{path.stem}_{timestamp}{path.suffix}"
    new_path = path.parent / new_name

    # 通番を付与して重複回避
    counter = 1
    while new_path.exists():
        new_name = f"{path.stem}_{timestamp}_{counter:02d}{path.suffix}"
        new_path = path.parent / new_name
        counter += 1

    try:
        shutil.move(str(path), str(new_path))
        logger = getLogger(__name__)
        logger.info(f"📦 Backed up existing file to: {new_name}")
    except Exception as e:
        logger = getLogger(__name__)
        logger.warning(f"⚠️ Failed to backup file: {e}")
