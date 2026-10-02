"""市場データ取得プロファイル (標準 / Colab)

Colab は共用 IP のため Yahoo Finance の 429 を受けるリスクがあるとして、Colab 専用に
リトライ拡大 (回数・バックオフ) を持つ。実測では 429 は発生しておらず、事前インターバルは
標準と同値 (下記 COLAB の注記を参照)。

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
# Colab 実測 (2026-10-02, 差分モード 197 サブバッチ) で、事前インターバル 1.0 秒と 0.2 秒の
# いずれも 429 (空応答・例外) は 0 件。1.0 秒は取得時間を約 150 秒延ばすだけだったため標準と同値に戻す。
# Colab の遅さの主因は 429 ではなく通信 1 回あたりの遅延 (約 2.3 秒/サブバッチ。専用環境は約 0.5 秒)。
# リトライ回数・バックオフの拡大は、429 が発生しない限りコストが無いため保険として残す。
COLAB = FetchProfile("colab", 0.2, 5, 8.0, 15.0)

_PROFILES = {p.name: p for p in (STANDARD, COLAB)}


def resolve_fetch_profile(context: Optional[Any] = None) -> FetchProfile:
    """context.config → 環境変数 → standard の順で取得プロファイルを解決する。"""
    name = None
    config = getattr(context, "config", None)
    if isinstance(config, dict):
        name = config.get("fetch_profile")
    name = (name or os.getenv(ENV_VAR) or "standard").strip().lower()
    return _PROFILES.get(name, STANDARD)
