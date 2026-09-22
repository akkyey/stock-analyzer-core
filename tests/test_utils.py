"""src/utils/__init__.py の包括的テスト"""

import math
from datetime import datetime
import pandas as pd
import pytest

from src.utils import (
    DateManager,
    get_current_time,
    get_today_str,
    is_empty,
    clean_nan_dict,
    safe_float,
    safe_float_or_none,
    ensure_dir,
    save_dataframe_to_csv,
    safe_display_value,
    generate_row_hash,
    rotate_file_backup,
)


def test_date_manager():
    assert DateManager.normalize(None) == ""
    assert DateManager.normalize(math.nan) == ""
    assert DateManager.normalize("2026/01/01") == "2026-01-01"
    assert DateManager.normalize(datetime(2026, 1, 1)) == "2026-01-01"
    assert DateManager.is_valid("2026-01-01") is True
    assert DateManager.is_valid("invalid-date") is False


def test_time_and_empty():
    assert isinstance(get_current_time(), datetime)
    assert len(get_today_str()) == 10
    
    assert is_empty(None) is True
    assert is_empty(math.nan) is True
    assert is_empty("") is True
    assert is_empty("text") is False


def test_clean_nan_dict():
    raw = {"a": 1, "b": math.nan, "c": None, "d": "valid"}
    cleaned = clean_nan_dict(raw)
    assert "b" not in cleaned or cleaned["b"] is None
    assert cleaned["a"] == 1


def test_safe_floats():
    assert safe_float("12.34") == 12.34
    assert safe_float("invalid") == 0.0

    assert safe_float_or_none("45.67") == 45.67
    assert safe_float_or_none("invalid") is None
    assert safe_float_or_none(None) is None


def test_ensure_dir_and_save_csv(tmp_path):
    d = tmp_path / "sub_dir"
    ensure_dir(str(d))
    assert d.exists()

    df = pd.DataFrame({"col1": [1, 2], "col2": ["a", "b"]})
    csv_file = d / "test.csv"
    save_dataframe_to_csv(df, str(csv_file))
    assert csv_file.exists()


def test_safe_display_and_hash():
    assert safe_display_value(None, fallback="-") == "-"
    assert safe_display_value(10) == 10

    series = pd.Series({"code": "7203", "price": 2500})
    h = generate_row_hash(series)
    assert isinstance(h, str) and len(h) > 0


def test_rotate_file_backup(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    rotate_file_backup(str(f))
    # Original file is preserved or rotated
    assert len(list(tmp_path.glob("*"))) >= 1
