"""市場データ取得プロファイル (標準 / Colab)

Colab は共用 IP のため Yahoo Finance の 429 を受けやすく、標準 (専用 IP 想定) と
同じ取得経路では 429 → 事後バックオフが多発して遅く、バッチ欠落も起きやすい。
そこで Colab 専用に「バッチ間の事前インターバル」と「リトライ拡大」を持つ。

選択順: context.config["fetch_profile"] → 環境変数 STOCK_ANALYZER_FETCH_PROFILE → standard
(Colab ノートブックが config で明示する。環境の推測判定は行わない)
"""

import os
from dataclasses import dataclass
from typing import Any, Optional

ENV_VAR = "STOCK_ANALYZER_FETCH_PROFILE"


@dataclass(frozen=True)
class FetchProfile:
    name: str
    inter_batch_sleep: float  # 成功したサブバッチ後の事前インターバル (秒)
    max_retries: int  # サブバッチあたりの試行回数
    empty_backoff_base: float  # 空応答 (429 疑い) 時の指数バックオフ基準 (秒)
    error_backoff_base: float  # 例外時の指数バックオフ基準 (秒)


STANDARD = FetchProfile("standard", 0.2, 3, 5.0, 10.0)
# インターバル初期値はベンチマーク設計書 §6.2 の推奨 (1〜2秒) の下限。Colab 実測で調整する。
COLAB = FetchProfile("colab", 1.0, 5, 8.0, 15.0)

_PROFILES = {p.name: p for p in (STANDARD, COLAB)}


def resolve_fetch_profile(context: Optional[Any] = None) -> FetchProfile:
    """context.config → 環境変数 → standard の順で取得プロファイルを解決する。"""
    name = None
    config = getattr(context, "config", None)
    if isinstance(config, dict):
        name = config.get("fetch_profile")
    name = (name or os.getenv(ENV_VAR) or "standard").strip().lower()
    return _PROFILES.get(name, STANDARD)
