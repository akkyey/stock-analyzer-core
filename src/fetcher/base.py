from logging import getLogger

from src.config_singleton import ConfigSingleton


class FetcherBase:
    """
    データ取得クラスの基底クラス。
    ConfigSingleton を使用して設定を一元管理する。
    """

    # Status Constants
    STATUS_SUCCESS = "success"
    STATUS_ERROR_QUOTA = "error_quota"
    STATUS_ERROR_NETWORK = "error_network"
    STATUS_ERROR_DATA = "error_data"
    STATUS_ERROR_OTHER = "error_other"

    def __init__(self, config_source=None):
        """
        FetcherBaseの初期化。

        Args:
            config_source: 設定ソース。以下のいずれか:
                - None: ConfigSingleton から取得
                - dict: 直接設定辞書を渡す（テスト用）
        """
        self.logger = getLogger(__name__)

        if isinstance(config_source, dict):
            # 辞書が直接渡された場合はそのまま使用（テスト用）
            self.config = config_source
        else:
            # ConfigSingleton からグローバル設定を取得
            self.config = ConfigSingleton.get_config()
