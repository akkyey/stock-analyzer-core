"""
DiscordNotifier (軽量互換スタブ)

[移行メモ]:
旧来の Discord Webhook 送信、リトライ、レートリミット等の完全版コード (290行) は、
以下のアーカイブへ退避されました:
`archive/legacy_orchestrator_v13/notifier_original.py`
"""

from logging import getLogger
from typing import Any

logger = getLogger(__name__)


class DiscordNotifier:
    """Discord 通知スタブ。ログ出力のみを行い、外部ネットワーク依存を排除。"""

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url

    def send_message(self, message: str, **kwargs: Any) -> bool:
        logger.info(f"📢 [Notification Stub] {message}")
        return True

    def send_alert(self, title: str, description: str, **kwargs: Any) -> bool:
        logger.warning(f"🚨 [Notification Alert Stub] {title}: {description}")
        return True
