import os
import sys
import threading
from logging import getLogger
from typing import Any

from google import genai


class APIKeyManager:
    """
    [v12.0] APIキーの保持、健康状態管理、ローテーションロジックを担当。
    AIAgentからキー管理責務を分離。
    """

    def __init__(self, debug_mode: bool = False):
        self.logger = getLogger(__name__)
        self.debug_mode = debug_mode
        self.api_keys: list[str] = []
        self.key_stats: list[dict[str, Any]] = []
        self.current_key_idx = 0
        self.lock = threading.Lock()

        self._load_keys()

    def _load_keys(self):
        """環境変数からAPIキーを読み込む"""
        if self.debug_mode:
            return

        # Primay key
        key1 = os.getenv("GEMINI_API_KEY")
        if key1:
            self.api_keys.append(key1)

        # Additional keys
        for i in range(2, 10):
            k = os.getenv(f"GEMINI_API_KEY_{i}")
            if k:
                self.api_keys.append(k)

        if self.api_keys:
            self.logger.info(f"Loaded {len(self.api_keys)} API keys.")
            self.key_stats = [
                {
                    "usage": 0,
                    "errors": 0,
                    "status": "active",
                    "is_exhausted": False,
                    "total_calls": 0,  # 全試行回数 (リトライ含む)
                    "success_count": 0,  # 成功回数
                    "retry_count": 0,  # ブラックリストによるリトライ
                    "error_429_count": 0,  # 429エラーによるリトライ
                }
                for _ in self.api_keys
            ]
        else:
            self.logger.warning("GEMINI_API_KEY not found. AI features will fail.")

    def get_current_client(self):
        """現在のインデックスのキーでクライアントを初期化して返す"""
        if self.debug_mode:
            self.logger.info(
                "🔧 Debug Mode: Skipping Gemini Client initialization (Simulation Mode)"
            )
            return None

        if not self.api_keys:
            return None

        current_key = self.api_keys[self.current_key_idx]
        try:
            client = genai.Client(api_key=current_key)
            masked_key = current_key[:4] + "..." + current_key[-4:]
            self.logger.info(
                f"Initialized Gemini Client with Key #{self.current_key_idx + 1} ({masked_key})"
            )
            return client
        except Exception as e:
            self.logger.error(f"Failed to initialize Gemini Client: {e}")
            return None

    def check_key_health(self, idx: int):
        """キーの健康状態をチェックし、必要なら無効化 (Self-Healing)"""
        if idx >= len(self.key_stats):
            return

        stats = self.key_stats[idx]
        if stats["usage"] > 0:
            error_rate = stats["errors"] / stats["usage"]
            if (
                error_rate > 0.5 and stats["usage"] >= 5
            ):  # 閾値: 5回以上の試行でエラー率50%以上
                stats["status"] = "disabled"
                self.logger.warning(
                    f"🚫 Key #{idx + 1} Disabled (Self-Healing). Error Rate: {error_rate:.1%}"
                )

    def rotate_key(self) -> bool:
        """
        次の有効なAPIキーに切り替える (Permanent Switch)
        Returns:
            bool: 切り替え成功時 True
        """
        with self.lock:
            if len(self.api_keys) <= 1:
                if self.key_stats and self.key_stats[self.current_key_idx].get(
                    "is_exhausted"
                ):
                    self.logger.critical(
                        "🚨 SINGLE KEY EXHAUSTED: Daily quota reached. Terminating."
                    )
                    sys.exit(0)
                return False

            attempts = 0
            while attempts < len(self.api_keys):
                idx = (self.current_key_idx + 1) % len(self.api_keys)
                self.current_key_idx = idx

                stats = self.key_stats[idx]
                if stats["status"] == "active" and not stats.get("is_exhausted"):
                    current_key = self.api_keys[idx]
                    masked = current_key[:4] + "..." + current_key[-4:]
                    self.logger.warning(
                        f"🔄 Switching permanently to Key #{idx + 1} (ID: {masked})"
                    )
                    return True

                attempts += 1

            self.logger.critical(
                "🚨 ALL API KEYS EXHAUSTED: Daily quota reached."
                " Terminating analysis session to protect data integrity."
            )
            sys.exit(1)

    def get_total_calls(self) -> int:
        """全キーを通じた累積API呼出回数を取得"""
        if not self.key_stats:
            return 0
        return sum(k["total_calls"] for k in self.key_stats)

    def update_stats(self, idx: int, field: str, amount: int = 1):
        """統計情報の更新"""
        if 0 <= idx < len(self.key_stats) and field in self.key_stats[idx]:
            self.key_stats[idx][field] += amount
