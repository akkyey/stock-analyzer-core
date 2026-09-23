"""分析エージェントのメインクラス

各コンポーネント（KeyManager, PromptBuilder, ResponseParser）を統合して、
株式銘柄の AI 分析ワークフローを実行する。

[v13.0] tenacity ライブラリによるリトライ処理の標準化
"""

import time
from logging import getLogger
from typing import Any

from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.ai.key_manager import APIKeyManager
from src.ai.prompt_builder import PromptBuilder
from src.ai.response_parser import ResponseParser
from src.validation_engine import ValidationEngine


# カスタム例外: リトライ対象
class RateLimitError(Exception):
    """APIクォータ制限エラー (429)"""

    pass


class TransientAPIError(Exception):
    """一時的なAPIエラー（リトライ対象）"""

    pass


class AIAgent:
    """分析エージェントのメインクラス。

    各コンポーネント（KeyManager, PromptBuilder, ResponseParser）を統合して、
    株式銘柄の AI 分析ワークフローを実行する。

    Attributes:
        model_name (str): 使用する AI モデル名。
        interval_sec (float): リクエスト間のインターバル（秒）。
        debug_mode (bool): デバッグモード（APIコールをスキップ）のフラグ。
        config (Dict[str, Any]): システム設定の辞書。
        key_manager (APIKeyManager): APIキーの管理モジュール。
        prompt_builder (PromptBuilder): プロンプト構築モジュール。
        response_parser (ResponseParser): 回答パース・検証モジュール。
        validator (ValidationEngine): データ検証モジュール。
    """

    def __init__(
        self, model_name: str, interval_sec: float = 2.0, debug_mode: bool = False
    ) -> None:
        """AIAgent を初期化する。

        Args:
            model_name (str): AI モデルの名。
            interval_sec (float, optional): リクエスト間隔。デフォルトは 2.0。
            debug_mode (bool, optional): デバッグモード。デフォルトは False。
        """
        self.logger = getLogger(__name__)
        self.model_name = model_name
        self.interval_sec = interval_sec
        self.debug_mode = debug_mode
        self.config: dict[str, Any] = {}
        self.key_manager = APIKeyManager(debug_mode=debug_mode)
        self.prompt_builder = PromptBuilder()
        self.response_parser = ResponseParser()
        self.validator: ValidationEngine | None = None

        # 内部定数とフォールバック
        self.MAX_RETRIES = 3
        self.FALLBACK_REASON = (
            "[ANALYSIS FAILED]: 判断不能。3回のリトライ後も具体的根拠を生成できず。"
            "財務データの欠損または材料不足のため、投資判断には手動確認が必須。"
        )

        # クライアントの初期化 (KeyManager経由)
        self.client = self.key_manager.get_current_client()
        self.audit_version = 1

    # ------------------------------------------------------------------
    # プロパティ（後方互換性のため）
    # ------------------------------------------------------------------
    @property
    def api_keys(self) -> Any:
        return self.key_manager.api_keys

    @api_keys.setter
    def api_keys(self, value: Any) -> None:
        self.key_manager.api_keys = value

    @property
    def current_key_idx(self) -> int:
        return self.key_manager.current_key_idx

    @current_key_idx.setter
    def current_key_idx(self, value: int) -> None:
        self.key_manager.current_key_idx = value

    @property
    def key_stats(self) -> Any:
        return self.key_manager.key_stats

    @key_stats.setter
    def key_stats(self, value: Any) -> None:
        self.key_manager.key_stats = value

    @property
    def thresholds_cfg(self) -> dict[str, Any]:
        return self.prompt_builder.thresholds_cfg

    @property
    def sector_policies(self) -> dict[str, Any]:
        """互換性のためのエイリアス"""
        return dict(self.config.get("sector_policies", {}))

    @sector_policies.setter
    def sector_policies(self, value: dict[str, Any]) -> None:
        self.config["sector_policies"] = value

    def set_config(self, config: dict[str, Any]) -> None:
        """システム設定を注入し、内部コンポーネントを最新化する。

        Args:
            config (Dict[str, Any]): アプリケーション全体の設定。
        """
        self.config = config
        self.prompt_builder.config = config
        if self.validator is None:
            self.validator = ValidationEngine(config)

    def get_total_calls(self) -> int:
        """全APIキーを通じた累積呼び出し回数を取得する。"""
        return self.key_manager.get_total_calls()

    def generate_usage_report(self) -> str:
        """API 使用状況の監査レポートを文字列として生成する。

        Returns:
            str: 構造化された使用状況レポート。
        """
        lines = [
            "\n" + "=" * 40,
            "       📊 Batch API Usage Audit Report",
            "       (Process-Local Statistics)",
            "=" * 40,
        ]
        for i, stats in enumerate(self.key_stats):
            status_icon = "✅" if stats["status"] == "active" else "🚫"
            lines.append(f"Key #{i + 1}: {status_icon} {stats['status']}")
            lines.append(f"  - Total Calls: {stats['total_calls']}")
            lines.append(f"  - Success:     {stats['success_count']}")
            lines.append(f"  - Retries(BL): {stats['retry_count']}")
            lines.append(f"  - Errors(429): {stats['error_429_count']}")
        lines.append("=" * 40)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # API呼び出し（tenacity によるリトライ処理）
    # ------------------------------------------------------------------
    def _call_api_once(self, prompt: str) -> Any:
        """単一のAPI呼び出しを実行する。

        Args:
            prompt (str): AI に送信するプロンプト。

        Returns:
            Any: API レスポンスオブジェクト。

        Raises:
            RateLimitError: クォータ制限エラー (429)。
            TransientAPIError: 一時的なAPIエラー。
        """
        if self.client is None:
            self.client = self.key_manager.get_current_client()

        if not self.client:
            raise TransientAPIError("Client initialization failed")

        idx = self.key_manager.current_key_idx
        self.key_manager.update_stats(idx, "total_calls")
        self.key_manager.update_stats(idx, "usage")

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            self.key_manager.update_stats(idx, "success_count")
            return response
        except Exception as e:
            err_msg = str(e)
            self.key_manager.update_stats(idx, "errors")

            # クォータ制限（429 / ResourceExhausted）のチェック
            if "429" in err_msg or "ResourceExhausted" in err_msg:
                self.logger.warning(
                    f"⚠️ Key #{idx + 1} hit rate limit (429). Rotating..."
                )
                self.key_manager.update_stats(idx, "error_429_count")
                self.key_manager.key_stats[idx]["is_exhausted"] = True
                if self._rotate_key():
                    raise RateLimitError(err_msg) from e
                else:
                    raise RateLimitError("All keys exhausted: " + err_msg) from e

            # その他のエラー
            self.key_manager.check_key_health(idx)
            raise TransientAPIError(err_msg) from e

    def _generate_content_with_retry(self, prompt: str) -> tuple[Any | None, int]:
        """API 呼び出しのリトライ処理（tenacity使用）。

        Args:
            prompt (str): AI に送信するプロンプト。

        Returns:
            tuple[Optional[Any], int]: (API レスポンスオブジェクト, 試行回数)
        """
        if self.debug_mode:
            return None, 0

        # リトライ回数を追跡するためのカウンタ (リストで参照渡し)
        attempt_counter = [0]

        @retry(
            stop=stop_after_attempt(len(self.api_keys) + 2),
            wait=wait_exponential(multiplier=2, min=5, max=60),
            retry=retry_if_exception_type((RateLimitError, TransientAPIError)),
            before_sleep=before_sleep_log(self.logger, log_level=30),  # WARNING
            reraise=True,
        )
        def _retry_call() -> Any:
            attempt_counter[0] += 1
            return self._call_api_once(prompt)

        try:
            return _retry_call(), attempt_counter[0]
        except RetryError:
            self.logger.error("❌ All retry attempts exhausted.")
            return None, attempt_counter[0]
        except Exception as e:
            self.logger.error(f"❌ Unexpected error: {e}")
            return None, attempt_counter[0]

    # ------------------------------------------------------------------
    # メイン分析メソッド
    # ------------------------------------------------------------------
    def analyze(
        self, row: dict[str, Any], strategy_name: str = "Unknown"
    ) -> dict[str, Any]:
        """銘柄データを受け取り、AI 分析プロセスを実行するメインメソッド。

        Args:
            row (Dict[str, Any]): 銘柄の財務・マーケットデータ。
            strategy_name (str, optional): 使用する投資戦略。デフォルトは "Unknown"。

        Returns:
            Dict[str, Any]: 分析結果（感情、理由、リスク、期間等を含む辞書）。
        """
        target_code = row.get("code")

        # [v16.3] 環境判定を冒頭に移動
        from src.env_loader import get_stock_env

        is_production = get_stock_env() == "production"

        # 1. データのバリデーション
        if self.validator:
            is_valid, data_issues = self.validator.validate_stock_data(
                row, strategy=strategy_name
            )
        else:
            is_valid = target_code is not None
            data_issues = [] if is_valid else ["Missing Code"]

        # [v17.0] PRODUCTION ではバリデーション不備があってもスキップしない (プロンプト側の救済ロジックに委ねる)
        if not is_valid and not is_production:
            self.logger.warning(
                f"🚫 Analysis Skipped for {target_code}: "
                f"Critical Data Defects -> {data_issues}"
            )
            return {
                "ai_sentiment": "Neutral",
                "ai_reason": (
                    f"[ANALYSIS SKIPPED]: データ不備のため分析対象外 "
                    f"(理由: {', '.join(data_issues)})"
                ),
                "ai_risk": "High (Unknown)",
                "ai_horizon": "Wait",
                "_analysis_failed": True,
                "audit_version": 0,
            }

        # [Debug Mode Bypass]
        # [v16.3] Enforce real analysis in PRODUCTION even if debug_mode is True
        if self.debug_mode and not is_production:
            from src.utils import get_current_time

            return {
                "code": target_code,
                "market_data_id": row.get("market_data_id"),
                "ai_sentiment": "Neutral",
                "ai_summary": "【結論】MOCK ANALYSIS: データ不備なし。安定成長を期待。",
                "ai_reason": "【結論】MOCK ANALYSIS: データ不備なし。安定成長を期待。",
                "ai_detail": "これはデバッグモード用のモックレスポンスです。",
                "ai_risk": "Low (Reason: Mock)",
                "ai_horizon": "Wait",
                "audit_version": 1,
                "analyzed_at": get_current_time(),
                "api_call_count": 0,
            }

        # 2. プロンプト作成
        prompt = self._create_prompt(row, strategy_name)
        dqf_alert = self._generate_dqf_alert(row)
        if dqf_alert:
            prompt += f"\n\n注意: {dqf_alert}"

        # 3. 実行とリトライループ
        final_result = None
        current_api_calls = 0  # [v14.6] Track API calls for this specific analysis

        for attempt in range(self.MAX_RETRIES):
            try:
                if attempt > 0:
                    time.sleep(self.interval_sec)

                idx = self.key_manager.current_key_idx

                # attempt counts handled inside _generate_content_with_retry now
                response, calls_made = self._generate_content_with_retry(prompt)
                current_api_calls += calls_made

                if not response:
                    self.logger.warning(
                        f"⚠️ Attempt {attempt + 1} failed: No response from API."
                    )
                    continue

                res_dict = self._parse_response(response.text)
                is_valid_res, err_reason = self._validate_response(res_dict)
                if is_valid_res:
                    final_result = res_dict
                    if (
                        "audit_version" not in final_result
                        or not final_result["audit_version"]
                    ):
                        final_result["audit_version"] = self.audit_version
                    break
                else:
                    self.logger.warning(
                        f"⚠️ Attempt {attempt + 1} failed quality check: {err_reason}"
                    )
                    self.key_manager.update_stats(idx, "retry_count")
            except Exception as e:
                self.logger.error(f"Attempt {attempt + 1} failed with error: {e}")

        if not final_result:
            return {
                "ai_sentiment": "Neutral",
                "ai_reason": self.FALLBACK_REASON,
                "ai_risk": "High (Unknown)",
                "ai_horizon": "Wait",
                "_analysis_failed": True,
                "audit_version": 0,
                "api_call_count": current_api_calls,  # [v14.6]
            }

        # [v14.6] Attach API call count to result
        final_result["api_call_count"] = current_api_calls
        return final_result

    def analyze_from_text(self, prompt: str) -> dict[str, Any]:
        """生のテキストプロンプトから AI 分析を実行する（Runner/Debug向け）。

        Args:
            prompt (str): 自作または加工済みのプロンプト文字列。

        Returns:
            Dict[str, Any]: パースされた分析結果。
        """
        if self.debug_mode:
            return {
                "ai_sentiment": "Neutral",
                "ai_reason": "【結論】MOCK ANALYSIS: データ不備なし。安定成長を期待。",
                "ai_summary": "【結論】MOCK ANALYSIS: データ不備なし。安定成長を期待。",
                "ai_risk": "Low (Reason: Mock)",
                "ai_horizon": "Wait",
                "audit_version": 1,
            }

        response, _ = self._generate_content_with_retry(prompt)
        return self._parse_response(response.text if response else "{}")

    # ------------------------------------------------------------------
    # [Agentic Pipeline] StockDossier 分析メソッド
    # ------------------------------------------------------------------
    def analyze_dossier(self, dossier: dict[str, Any]) -> dict[str, Any]:
        """[Agentic Pipeline] StockDossier を受け取り、投資判断 (InvestmentVerdict) を行う。"""
        code = dossier.get("code", "Unknown")
        name = dossier.get("name", "Unknown")

        if self.debug_mode:
            return {
                "code": code,
                "name": name,
                "verdict": "BUY",
                "agent_score": 85.0,
                "strategy_match": "Value & Momentum",
                "investment_thesis": f"【MOCK】{name} ({code}) は良好な財務基盤と反発モメンタムを維持しています。",
                "risk_factors": ["市場全体のボラティリティ"],
                "time_horizon": "Swing (2〜6週)",
                "_mock": True,
            }

        prompt = PromptBuilder.build_dossier_analysis_prompt(dossier)
        response, _ = self._generate_content_with_retry(prompt)

        if not response or not hasattr(response, "text"):
            return {
                "code": code,
                "name": name,
                "verdict": "WATCH",
                "agent_score": 50.0,
                "investment_thesis": "AI推論がタイムアウトまたは失敗したため、手動確認を推奨。",
                "risk_factors": ["APIレスポンス欠落"],
                "time_horizon": "Wait",
                "_analysis_failed": True,
            }

        import json
        import re

        text = response.text
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                parsed["name"] = name
                return dict(parsed)
            except Exception as e:
                self.logger.warning(f"Failed to parse verdict JSON for {code}: {e}")

        return {
            "code": code,
            "name": name,
            "verdict": "WATCH",
            "agent_score": 60.0,
            "investment_thesis": text[:200] if text else "分析テキストなし",
            "risk_factors": [],
            "time_horizon": "Unknown",
        }

    def analyze_dossiers_batch(
        self, dossiers: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """複数件の StockDossier を分析し、エージェントスコア順にランク付けする。"""
        results = []
        for d in dossiers:
            res = self.analyze_dossier(d)
            results.append(res)
            if not self.debug_mode and self.interval_sec > 0:
                time.sleep(self.interval_sec)

        # エージェントスコアで降順ソートして rank を付与
        results.sort(key=lambda x: x.get("agent_score", 0.0), reverse=True)
        for i, r in enumerate(results, start=1):
            r["rank"] = i
        return results

    # ------------------------------------------------------------------
    # 後方互換性およびテストのためのラッパーメソッド
    # ------------------------------------------------------------------
    def _rotate_key(self) -> bool:
        success = self.key_manager.rotate_key()
        if success:
            self._init_client()
        return success

    def _check_key_health(self, idx: int) -> None:
        self.key_manager.check_key_health(idx)

    def _prepare_variables(
        self, row: dict[str, Any], strategy_name: str
    ) -> dict[str, Any]:
        return self.prompt_builder.prepare_variables(row, strategy_name)

    def _create_prompt(self, row: dict[str, Any], strategy_name: str) -> str:
        return self.prompt_builder.create_prompt(row, strategy_name)

    def _parse_response(self, text: str) -> dict[str, Any]:
        return self.response_parser.parse_response(text)

    def _validate_response(self, result: dict[str, Any]) -> tuple[bool, str | None]:
        return self.response_parser.validate_response(result)

    def _generate_dqf_alert(self, row: dict[str, Any]) -> str | None:
        return self.response_parser.generate_dqf_alert(row)

    def _init_client(self) -> None:
        self.client = self.key_manager.get_current_client()

    def _load_prompt_template(self) -> dict[str, Any]:
        """プロンプトテンプレートの読み込み (互換性のためのラッパー)"""
        if not hasattr(self, "prompt_builder"):
            return {}
        self.prompt_builder.prompt_config = self.prompt_builder._load_prompt_template()
        return self.prompt_builder.prompt_config
