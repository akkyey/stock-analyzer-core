import json
import re
from logging import getLogger
from pathlib import Path
from typing import Any


class ResponseParser:
    """
    [v12.0] LLMの回答パース、構文・構造バリデーション、DQFアラート生成を担当。
    """

    def __init__(self):
        self.logger = getLogger(__name__)
        self.blacklist = self._load_blacklist()

    def _load_blacklist(self) -> list[str]:
        """ブラックリスト・フレーズを読み込む"""
        path = Path("config/blacklist.txt")
        blacklist = []
        if path.exists():
            with open(path, encoding="utf-8") as f:  # noqa: PTH123
                for raw_line in f:
                    line = raw_line.strip()
                    if line and not line.startswith("#"):
                        blacklist.append(line)
        # 固定ブラックリストの追加 (AIAgent.bak L285)
        blacklist.extend(["安定した業績推移", "マクロ経済の影響", "AI言語モデル"])
        return blacklist

    def parse_response(self, text: str) -> dict[str, Any]:
        """
        LLMの回答(JSON)を辞書に変換し、正規化する。
        """
        try:
            cleaned_text = text.strip()
            # Remove Markdown code blocks if present
            if "```" in cleaned_text:
                match = re.search(
                    r"```(?:json)?\s*({.*?})\s*```", cleaned_text, re.DOTALL
                )
                if match:
                    cleaned_text = match.group(1)

            # Basic cleanup: remove everything before first { and after last }
            # if json.loads still fails.
            try:
                data = json.loads(cleaned_text)
            except json.JSONDecodeError:
                # [v14.6] Try aggressive extraction & Fix common syntax errors
                # 1. Extract outermost JSON object
                json_match = re.search(r"({.*})", cleaned_text, re.DOTALL)
                target_text = json_match.group(1) if json_match else cleaned_text

                try:
                    # 2. Fix missing commas between values and keys (e.g., "value" "next_key":)
                    # Pattern: " (end of string) followed by whitespace/newlines then " (start of next key)
                    target_text = re.sub(r"\"\s*(\r\n|\n|\r)\s*\"", '", "', target_text)
                    target_text = re.sub(r"\"\s+\"", '", "', target_text)

                    # 3. Fix trailing commas before closing braces
                    target_text = re.sub(r",\s*([}\]])", r"\1", target_text)

                    data = json.loads(target_text)
                    self.logger.info("🔧 Auto-repaired malformed JSON response.")
                except json.JSONDecodeError as e2:
                    # re-raise original if repair fails, to be caught by outer exception
                    raise e2

            # [v12.0] ai_agent.py.bak L360-382 の構造に合わせる
            return {
                "ai_sentiment": data.get("ai_sentiment", "Neutral"),
                "ai_reason": data.get(
                    "ai_reason", data.get("ai_summary", "No reason provided.")
                ),
                "ai_risk": data.get("ai_risk", "Unknown"),
                "ai_horizon": data.get("ai_horizon", "Wait"),
                "ai_detail": data.get("ai_detail", ""),
                "audit_version": data.get("audit_version", 0),
            }
        except Exception as e:
            # [Fix] Downgrade to WARNING to avoid alarming user during retries
            self.logger.warning(
                f"⚠️ Failed to parse AI response (Attempting retry): {e}"
            )
            self.logger.debug(f"Raw response text: {text}")
            return {
                "ai_sentiment": "Error",
                "ai_reason": "Failed to parse JSON response.",
                "ai_risk": "Unknown",
                "ai_horizon": "Wait",
                "_parse_error": True,
            }

    def validate_response(self, result: dict[str, Any]) -> tuple[bool, str | None]:
        """JSON構造とサマリー構文、およびブラックリストフレーズを検証する。"""
        # 1. Sentiment Enum Check
        ok, msg = self._check_sentiment_enum(result.get("ai_sentiment"))
        if not ok:
            return False, msg

        # 2. Summary (ai_reason) Syntax Check
        reason = result.get("ai_reason", "")
        ok, msg = self._check_summary_syntax(reason)
        if not ok:
            return False, msg

        ok, msg = self._check_summary_tags(reason)
        if not ok:
            return False, msg

        # 3. Detail Body Structural Check (①-⑦)
        ok, msg = self._check_detail_structure(result.get("ai_detail", ""))
        if not ok:
            return False, msg

        # 4. Blacklist Check
        ok, msg = self._check_blacklist(reason, result.get("ai_risk", ""))
        if not ok:
            return False, msg

        return True, None

    def _check_sentiment_enum(self, sentiment: str | None) -> tuple[bool, str | None]:
        """感情Enumの有効性チェック"""
        valid = [
            "Bullish",
            "Bullish (Aggressive)",
            "Bullish (Defensive)",
            "Bearish",
            "Neutral",
            "Neutral (Positive)",
            "Neutral (Wait)",
            "Neutral (Caution)",
        ]
        if sentiment not in valid:
            return False, f"Invalid sentiment: {sentiment}"
        return True, None

    def _check_summary_syntax(self, reason: str) -> tuple[bool, str | None]:
        """サマリー構文（1行制限等）のチェック"""
        if not reason:
            return False, "Empty ai_summary"
        if "\n" in reason or "\r" in reason:
            return False, "Summary contains newlines (Must be single line)"
        return True, None

    def _check_summary_tags(self, reason: str) -> tuple[bool, str | None]:
        """サマリー内の必須タグチェック"""
        has_conclusion = "【結論】" in reason or "【総評】" in reason
        if not (has_conclusion and "｜【強み】" in reason and "｜【懸念】" in reason):
            return False, "Summary format/order violation"

        count_conclusion = reason.count("【結論】") + reason.count("【総評】")
        if (
            count_conclusion != 1
            or reason.count("【強み】") != 1
            or reason.count("【懸念】") != 1
        ):
            return False, "Summary tags duplicated"
        return True, None

    def _check_detail_structure(self, detail: str) -> tuple[bool, str | None]:
        """詳細セクション（①-⑦）のチェック"""
        if not detail or "①" not in detail or "⑦" not in detail:
            return False, "Detail body missing numbered sections ①-⑦"
        return True, None

    def _check_blacklist(self, reason: str, risk: str) -> tuple[bool, str | None]:
        """ブラックリスト・フレーズのチェック"""
        for phrase in self.blacklist:
            if phrase in reason or phrase in risk:
                return False, f"Blacklist violation: '{phrase}' detected."
        return True, None

    def generate_dqf_alert(self, row: dict[str, Any]) -> str:
        """重要なデータ欠損に対する DQF (Data Quality Flag) アラートを生成する"""
        import pandas as pd

        dqf_items = {
            "debt_equity_ratio": "Debt/Equity Ratio",
            "free_cf": "Free CF",
            "operating_cf": "Operating CF",
            "roe": "ROE",
            "per": "PER",
        }
        missing = []
        for key, label in dqf_items.items():
            val = row.get(key)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                missing.append(label)

        if missing:
            return f"\n[DQF ALERT: 欠損項目 - {', '.join(missing)}]\nデータ不足を考慮した慎重な評価を行ってください。具体的な数値や根拠に基づいた分析を心がけてください。\n"
        return ""
