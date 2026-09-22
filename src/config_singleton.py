"""
ConfigSingleton - 設定の一元管理とシングルトンパターン

起動時に設定ファイルをパース・検証し、メモリに保持する。
全モジュールはこのシングルトンから設定を取得する。

使用方法:
    # アプリケーション起動時（main / entrypoint）
    from src.config_singleton import ConfigSingleton
    ConfigSingleton.initialize()  # 失敗時は例外

    # 各モジュール
    config = ConfigSingleton.get_config()
"""

import threading
from logging import getLogger
from typing import Any, Optional

from src.config_loader import ConfigLoader
from src.constants import CONFIG_PATH

# [AP-001] Centralized env loader access
from src.env_loader import get_stock_config_env, load_env_file


class ConfigurationError(Exception):
    """設定の読み込みまたは検証に失敗した場合の例外"""

    pass


class ConfigSingleton:
    """
    設定のシングルトン管理クラス。

    スレッドセーフで、一度初期化されると設定はイミュータブルとして扱われる。
    """

    _instance: Optional["ConfigSingleton"] = None
    _lock: threading.Lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 初回のみ初期化
        if not hasattr(self, "_config"):
            self._config: dict[str, Any] = {}
            self._raw_config: dict[str, Any] = {}
            self._validated: bool = False
            self._config_path: str = ""
            self.logger = getLogger(__name__)

    @classmethod
    def initialize(cls, config_path: str | None = None, force: bool = False) -> None:
        """
        設定を初期化する。アプリケーション起動時に一度だけ呼び出す。

        Args:
            config_path: 設定ファイルのパス。Noneの場合はデフォルトパスを使用。
            force: Trueの場合、既に初期化済みでも再初期化する。

        Raises:
            ConfigurationError: 設定の読み込みまたは検証に失敗した場合。
        """
        instance = cls()

        with cls._lock:
            if cls._initialized and not force:
                return

            # 環境変数をロード
            load_env_file()

            # パスを決定
            env_path = get_stock_config_env()
            path = config_path or env_path or CONFIG_PATH
            instance._config_path = path

            # ConfigLoader に委譲
            try:
                # ConfigLoader内で load -> merge defaults -> validate が行われる
                loader = ConfigLoader(str(path))

                instance._config = loader.config
                instance._raw_config = loader.raw_config
            except Exception as e:
                raise ConfigurationError(f"設定初期化エラー: {e}") from e

            # 必須キーの存在確認 (ConfigLoaderのPydantic検証で十分だが、念のため維持)
            cls._validate_required_keys(instance._config)

            instance._validated = True
            cls._initialized = True
            instance.logger.info(f"設定を初期化しました: {path}")

    @classmethod
    def _validate_required_keys(cls, config: dict[str, Any]) -> None:
        """必須キーの存在を検証"""
        required_keys = [
            ("data", "jp_stock_list"),
        ]

        for section, key in required_keys:
            if section not in config:
                raise ConfigurationError(f"必須セクションがありません: {section}")
            if key not in config[section]:
                raise ConfigurationError(f"必須キーがありません: {section}.{key}")

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        """
        設定辞書を取得する。

        Returns:
            検証済みの設定辞書。

        Raises:
            ConfigurationError: 設定が初期化されていない場合。
        """
        instance = cls()

        if not cls._initialized:
            # 自動初期化を試みる（後方互換性のため）
            try:
                cls.initialize()
            except ConfigurationError as e:
                raise ConfigurationError(
                    "設定が初期化されていません。"
                    "アプリケーション起動時に ConfigSingleton.initialize() を呼び出してください。"
                ) from e

        return instance._config.copy()  # コピーを返してイミュータブル性を保つ

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """
        設定値を安全に取得する。

        Args:
            key: ドット区切りのキー（例: "data.jp_stock_list"）
            default: キーが存在しない場合のデフォルト値

        Returns:
            設定値またはデフォルト値。
        """
        config = cls.get_config()

        keys = key.split(".")
        value = config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)  # type: ignore
                if value is None:
                    return default
            else:
                return default

        return value

    @classmethod
    def is_initialized(cls) -> bool:
        """設定が初期化済みかどうかを返す"""
        return cls._initialized

    @classmethod
    def reset(cls) -> None:
        """
        設定をリセットする（テスト用）。

        本番環境では使用しないこと。
        """
        with cls._lock:
            cls._initialized = False
            if cls._instance:
                cls._instance._config = {}
                cls._instance._validated = False


# 便利関数: 既存コードとの後方互換性
def get_config() -> dict[str, Any]:
    """ConfigSingleton.get_config() のショートカット"""
    return ConfigSingleton.get_config()


def get_config_value(key: str, default: Any = None) -> Any:
    """ConfigSingleton.get() のショートカット"""
    return ConfigSingleton.get(key, default)
