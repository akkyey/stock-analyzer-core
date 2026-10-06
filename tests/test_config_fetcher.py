"""設定ファイルの fetcher: セクションが、実行時の設定に届くこと"""

from src.config_loader import ConfigLoader
from src.config_schema import ConfigModel


def _fetcher(tmp_path, user_yaml: str) -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(user_yaml, encoding="utf-8")
    return ConfigLoader(str(path)).validate_config().fetcher


def test_defaults_fetcher_section_reaches_the_validated_config(tmp_path):
    """defaults.yaml の fetcher: が、検証後の設定に残る (スキーマに無かったため、`extra: ignore` で
    捨てられ、CLI では、設定ファイルでの切り替えが一切効かなかった)"""
    fetcher = _fetcher(tmp_path, "")
    assert fetcher["edinet_scan_days"] == 30
    assert fetcher["refresh_fundamentals_from_seed"] is True


def test_user_config_can_turn_off_seed_refresh(tmp_path):
    fetcher = _fetcher(tmp_path, "fetcher:\n  refresh_fundamentals_from_seed: false\n  edinet_scan_days: 45\n")
    assert fetcher["refresh_fundamentals_from_seed"] is False
    assert fetcher["edinet_scan_days"] == 45  # ほかの項目は、defaults と再帰的にマージされる
    assert fetcher["skip_heavy_repair"] is True


def test_fetcher_section_is_optional_in_the_schema():
    assert "fetcher" in ConfigModel.model_fields
    assert ConfigModel.model_fields["fetcher"].default_factory is not None
