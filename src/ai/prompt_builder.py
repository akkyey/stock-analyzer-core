"""プロンプトビルダー

[v12.0] プロンプトのテンプレート読み込み、変数の準備、最終的なプロンプト文字列の組み立てを担当。
[v2.0] StockAnalysisData の ValidationFlag を活用した検証メタデータの埋め込みに対応。
[v14.0] Refactored: prepare_variables を6メソッドに責務分離し、保守性向上。
"""

from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import yaml

from src.constants import AI_PROMPTS_PATH, THRESHOLDS_PATH
from src.utils import safe_float as s

if TYPE_CHECKING:
    from src.domain.models import StockAnalysisData


class PromptBuilder:
    """プロンプトビルダー。

    テンプレート読み込み、変数準備、プロンプト生成を担当。
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.logger = getLogger(__name__)
        self.config = config or {}
        self.prompt_config = self._load_prompt_template()
        self.thresholds_cfg = self._load_thresholds()
        self.audit_version = self.prompt_config.get("audit_version", 0)

    def _load_prompt_template(self) -> dict[str, Any]:
        """YAMLからプロンプトテンプレートを読み込む"""
        paths_to_try = [
            Path(AI_PROMPTS_PATH),
            Path("stock-analyzer4") / AI_PROMPTS_PATH,
        ]
        for path in paths_to_try:
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:  # noqa: PTH123
                        return yaml.safe_load(f) or {}
                except Exception as e:
                    self.logger.error(f"Failed to load prompt config from {path}: {e}")
        return {}

    def _load_thresholds(self) -> dict[str, Any]:
        """thresholds.yaml から判定閾値を読み込む"""
        paths_to_try = [
            Path(THRESHOLDS_PATH),
            Path("stock-analyzer4") / THRESHOLDS_PATH,
        ]
        for path in paths_to_try:
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:  # noqa: PTH123
                        data = yaml.safe_load(f)
                        return (
                            dict(data.get("thresholds", {}))
                            if isinstance(data, dict)
                            else {}
                        )
                except Exception as e:
                    self.logger.error(
                        f"Failed to load thresholds.yaml from {path}: {e}"
                    )
        return {}

    def prepare_variables(
        self, row: dict[str, Any], strategy_name: str
    ) -> dict[str, Any]:
        """プロンプトに挿入する変数の辞書を作成する（オーケストレーター）。

        Args:
            row: 銘柄データの一行。
            strategy_name: 戦略名。

        Returns:
            テンプレートに適用可能な変数値の辞書。
        """
        # 0. 基本情報
        sector_info = row.get("sector_17", row.get("sector", "Unknown"))
        code = row.get("code", "Unknown")
        name = row.get("name", "Unknown")

        # 1. 設定情報
        strat_cfg = self.config.get("strategies", {}).get(strategy_name, {})
        persona = strat_cfg.get("persona", "Analysis Expert")
        default_horizon = strat_cfg.get("default_horizon", "Wait")
        risk_context_str = self._get_risk_context(sector_info)

        # 2. 除外設定と特別指示
        exclusion_notice = self._build_exclusion_notice(sector_info, strategy_name)
        special_instruction = self._build_special_instruction(row, strategy_name)

        # 3. マーケットコンテキスト
        market_context = self._load_market_context()

        # 4. トレンドメトリクス
        trend_desc = self._calculate_trend_desc(row)

        # 5. ステータス文字列
        status_strings = self._format_status_strings(row)

        # 6. 欠損分類
        deficiency_info = self._classify_data_deficiency(row)

        # 7. 変数辞書構築
        return self._build_variables_dict(
            row=row,
            code=code,
            name=name,
            sector_info=sector_info,
            strategy_name=strategy_name,
            persona=persona,
            default_horizon=default_horizon,
            risk_context_str=risk_context_str,
            exclusion_notice=exclusion_notice,
            special_instruction=special_instruction,
            market_context=market_context,
            trend_desc=trend_desc,
            status_strings=status_strings,
            deficiency_info=deficiency_info,
        )

    # ========================================
    # プライベートメソッド: 責務分離
    # ========================================

    def _get_risk_context(self, sector_info: str) -> str:
        """セクターリスクコンテキストを取得する。"""
        sector_risks = self.config.get("sector_risks", {})
        return str(
            sector_risks.get(
                sector_info, "General Market Risk (No specific sector data)."
            )
        )

    def _build_exclusion_notice(self, sector_info: str, strategy_name: str) -> str:
        """分析除外メトリクスの通知を構築する。"""
        sector_policies = self.config.get("sector_policies", {})
        sector_policy = sector_policies.get(
            sector_info, sector_policies.get("default", {})
        )
        ai_excludes = list(sector_policy.get("ai_prompt_excludes", []))

        # 戦略固有の除外設定をマージ
        strat_policy_key = f"_strategy_{strategy_name}"
        if strat_policy_key in sector_policies:
            strat_excludes = sector_policies[strat_policy_key].get(
                "ai_prompt_excludes", []
            )
            ai_excludes = list(set(ai_excludes + strat_excludes))

        if not ai_excludes:
            return ""

        return (
            "\n        [ANALYSIS NOTICE]\n        "
            "The following metrics are NOT APPLICABLE for this sector/strategy "
            "and should be EXCLUDED from your analysis:\n        - "
            + "\n        - ".join(ai_excludes)
            + "\n        (These metrics may show 'None' or 'N/A' due to structural reasons, not data errors.)\n"
        )

    def _build_special_instruction(
        self, row: dict[str, Any], strategy_name: str
    ) -> str:
        """戦略固有の特別指示を構築する。"""
        if strategy_name != "turnaround_spec":
            return ""

        per_val = row.get("per")
        is_per_none = per_val is None or (
            isinstance(per_val, float) and pd.isna(per_val)
        )
        is_per_negative = (
            isinstance(per_val, (int, float)) and not pd.isna(per_val) and per_val <= 0
        )

        if is_per_none or is_per_negative:
            return """
        [SPECIAL ANALYST INSTRUCTION]
        This company currently has no earnings (PER is NaN/Negative). Focus your analysis on:
        1. Sales Growth sustainability.
        2. Asset value backing (PBR).
        3. Probability of a near-term turnaround (profitability improvements).
        Evaluate it as a high-risk, high-reward turnaround play.
"""
        return ""

    def _load_market_context(self) -> str:
        """マーケットコンテキストファイルを読み込む。"""
        market_context_paths = [
            Path("market_context.txt"),
            Path("stock-analyzer4") / "market_context.txt",
            Path("config") / "market_context.txt",
            Path("stock-analyzer4") / "config" / "market_context.txt",
        ]

        for p in market_context_paths:
            if p.exists():
                try:
                    with open(p, encoding="utf-8") as f:  # noqa: PTH123
                        lines = f.readlines()
                        filtered_lines = [
                            line
                            for line in lines
                            if not line.strip().startswith("[STRATEGY_SIGNAL]")
                        ]
                        return "".join(filtered_lines)
                except Exception:
                    pass

        return "No specific market context available."

    def _calculate_trend_desc(self, row: dict[str, Any]) -> str:
        """トレンド記述文字列を計算する。"""
        trend_score = 0

        if s(row.get("trend_up")) == 1:
            trend_score += 1

        macd_val = s(
            row.get("macd_hist")
            if row.get("macd_hist") is not None
            else row.get("macd")
        )
        if macd_val > 0:
            trend_score += 1

        rsi_val = s(row.get("rsi") if row.get("rsi") is not None else row.get("rsi_14"))
        if rsi_val > 50:
            trend_score += 1

        score_trend = s(row.get("score_trend"))
        if score_trend >= 60:
            trend_score += 1

        trend_signal_str = (
            "Strong Uptrend" if s(row.get("trend_up")) == 1 else "Neutral/Weak"
        )
        return f"{trend_score}/4 ({trend_signal_str})"

    def _format_status_strings(self, row: dict[str, Any]) -> dict[str, str]:
        """ステータス文字列を生成する。"""
        p_status_raw = str(row.get("profit_status", ""))
        t_status_raw = str(row.get("turnaround_status", ""))

        # 利益ステータス文字列
        profit_status_str = p_status_raw
        status_mapping = {
            "turnaround_black": "Turnaround (Moved from Loss to Profit!)",
            "surge": "Surge (Profit Jump)",
            "loss_shrinking": "Loss Shrinking",
            "loss_expanding": "Loss Expanding (Warning)",
            "crash": "Crash (Profit Drop)",
        }

        if t_status_raw == "turnaround_black":
            profit_status_str = status_mapping["turnaround_black"]
        elif p_status_raw in status_mapping:
            profit_status_str = status_mapping[p_status_raw]

        # ハイライトメッセージ
        highlight_msg = ""
        if t_status_raw == "turnaround_black" or p_status_raw == "turnaround":
            highlight_msg = (
                "\n        [🚨 PRIORITY ANALYSIS: TURNAROUND 🚨]\n        "
                "This stock is showing signs of a TURNAROUND (Loss -> Profit). "
                "Validate if this recovery is sustainable!"
            )
        elif p_status_raw == "surge":
            highlight_msg = (
                "\n        [🔥 PRIORITY ANALYSIS: PROFIT SURGE 🔥]\n        "
                "This stock is showing a PROFIT SURGE. "
                "Determine if this is a one-time event or a structural growth phase!"
            )

        # Quant scores
        q_val = row.get("score_value", 0)
        q_gro = row.get("score_growth", 0)
        q_qual = row.get("score_quality", 0)

        return {
            "profit_status_str": profit_status_str,
            "sales_status_str": str(row.get("sales_status", "Unknown")),
            "highlight_msg": highlight_msg,
            "quant_scores_str": f"Value({q_val}), Growth({q_gro}), Quality({q_qual})",
        }

    def _classify_data_deficiency(self, row: dict[str, Any]) -> dict[str, str]:
        """データ不備の分類を行う。"""
        dqf_items_map = {
            "per": "PER",
            "pbr": "PBR",
            "roe": "ROE",
            "operating_cf": "営業CF",
            "free_cf": "フリーCF",
            "sales_growth": "売上成長率",
            "operating_margin": "営業利益率",
            "debt_equity_ratio": "自己資本比率/DEレシオ",
        }

        missing_metrics = []
        for key, label in dqf_items_map.items():
            val = row.get(key)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                missing_metrics.append(label)

        if not missing_metrics:
            return {
                "deficiency_type": "完全データ型",
                "missing_metrics_summary": "なし",
            }

        # 欠損タイプ分類
        cf_metrics = {"営業CF", "フリーCF"}
        pl_metrics = {"PER", "ROE", "売上成長率", "営業利益率"}
        bs_metrics = {"PBR", "自己資本比率/DEレシオ"}

        types = []
        if cf_metrics & set(missing_metrics):
            types.append("CF欠損型")
        if pl_metrics & set(missing_metrics):
            types.append("PL欠損型")
        if bs_metrics & set(missing_metrics):
            types.append("BS欠損型")

        if len(types) >= 2:
            deficiency_type = "複合欠損型"
        elif types:
            deficiency_type = types[0]
        else:
            deficiency_type = "その他欠損型"

        return {
            "deficiency_type": deficiency_type,
            "missing_metrics_summary": "、".join(missing_metrics),
        }

    def _build_variables_dict(
        self,
        row: dict[str, Any],
        code: str,
        name: str,
        sector_info: str,
        strategy_name: str,
        persona: str,
        default_horizon: str,
        risk_context_str: str,
        exclusion_notice: str,
        special_instruction: str,
        market_context: str,
        trend_desc: str,
        status_strings: dict[str, str],
        deficiency_info: dict[str, str],
    ) -> dict[str, Any]:
        """最終的な変数辞書を構築する。"""
        # 値のフォーマッティング
        ocf = s(row.get("operating_cf"))
        sales = s(row.get("sales"))
        ocf_margin_val = f"{(ocf / sales * 100):.2f}" if sales != 0 else "None"

        op_margin_val = (
            row.get("operating_margin")
            if row.get("operating_margin") is not None
            else "None"
        )
        de_ratio = (
            row.get("debt_equity_ratio")
            if row.get("debt_equity_ratio") is not None
            else "None"
        )

        # 閾値変数
        threshold_vars = {}
        for _category, metrics in self.thresholds_cfg.items():
            for m_key, m_val in metrics.items():
                threshold_vars[f"threshold_{m_key}"] = m_val
                threshold_vars[m_key] = m_val

        # 基本変数辞書
        base_vars = row.copy()
        base_vars.update(
            {
                "code": code,
                "name": name,
                "sector": sector_info,
                "strategy_name": strategy_name,
                "persona": persona,
                "default_horizon": default_horizon,
                "highlight_msg": status_strings["highlight_msg"],
                "market_context": market_context,
                "exclusion_notice": exclusion_notice,
                "special_instruction": special_instruction,
                "risk_context_str": risk_context_str,
                "deficiency_type": deficiency_info["deficiency_type"],
                "missing_metrics_summary": deficiency_info["missing_metrics_summary"],
                "current_price": row.get("current_price"),
                "per": row.get("per"),
                "pbr": row.get("pbr"),
                "peg_ratio": row.get("peg_ratio"),
                "ocf_margin_val": ocf_margin_val,
                "free_cf": row.get("free_cf"),
                "de_ratio": de_ratio,
                "equity_ratio": row.get("equity_ratio"),
                "current_ratio": row.get("current_ratio"),
                "cf_status": row.get("cf_status"),
                "roe": row.get("roe"),
                "sales_growth": row.get("sales_growth"),
                "profit_growth": row.get("profit_growth"),
                "op_margin_val": op_margin_val,
                "dividend_yield": row.get("dividend_yield"),
                "payout_ratio": row.get("payout_ratio"),
                "volatility": row.get("volatility"),
                "real_volatility": (
                    f"{s(row.get('real_volatility')):.2f}"
                    if row.get("real_volatility") is not None
                    else "N/A"
                ),
                "rsi": row.get("rsi") or row.get("rsi_14") or "N/A",
                "macd": row.get("macd") or row.get("macd_hist") or "N/A",
                "trend_signal": row.get("trend_signal"),
                "trend_desc": trend_desc,
                "profit_status_str": status_strings["profit_status_str"],
                "sales_status_str": status_strings["sales_status_str"],
                "quant_scores_str": status_strings["quant_scores_str"],
                "ma_divergence": (
                    f"{row.get('ma_divergence', 0):.2f}"
                    if row.get("ma_divergence") is not None
                    else "N/A"
                ),
                "volume_ratio": (
                    f"{row.get('volume_ratio', 0):.2f}"
                    if row.get("volume_ratio") is not None
                    else "N/A"
                ),
                # ValidationFlag 関連
                "tier1_missing_count": row.get("tier1_missing_count", 0),
                "tier2_missing_count": row.get("tier2_missing_count", 0),
                "red_flag_count": row.get("red_flag_count", 0),
                "red_flags_list": row.get("red_flags_list", "なし"),
                "rescue_status": row.get("rescue_status", "非該当"),
                "pydantic_validated": row.get("pydantic_validated", False),
            }
        )
        base_vars.update(threshold_vars)
        return base_vars

    # ========================================
    # パブリックメソッド: プロンプト生成
    # ========================================

    def create_prompt(self, row: dict[str, Any], strategy_name: str) -> str:
        """テンプレートを元に最終的なプロンプトを作成する。

        Args:
            row: 銘柄データ。
            strategy_name: 戦略名。

        Returns:
            生成されたプロンプト文字列。
        """
        vars_dict = self.prepare_variables(row, strategy_name)

        base_tmpl = self.prompt_config.get("base_template", "")
        metrics_tmpl = self.prompt_config.get("metrics_template", "")

        if not base_tmpl:
            return "Error: Prompt template missing."

        try:
            metrics_section = metrics_tmpl.format(**vars_dict)
        except KeyError as e:
            self.logger.warning(f"Missing key in metrics template: {e}")
            metrics_section = "[Metrics Generation Error]"

        # Dynamic Metric Injection
        strategies_cfg = self.config.get("strategies", {})
        strat_cfg = strategies_cfg.get(strategy_name, {})
        points_map = strat_cfg.get("points", {})

        dynamic_metrics = []
        for metric in points_map:
            if f"{{{metric}}}" not in metrics_tmpl:
                val = vars_dict.get(metric, "N/A")
                dynamic_metrics.append(f"- {metric}: {val}")

        if dynamic_metrics:
            metrics_section += "\n" + "\n".join(dynamic_metrics)

        vars_dict["metrics_section"] = metrics_section

        try:
            return str(base_tmpl.format(**vars_dict))
        except Exception as e:
            self.logger.error(f"Failed to format base prompt: {e}")
            return f"Analyze stock {row.get('code')} ({row.get('name')})"

    def create_prompt_from_model(
        self, stock: "StockAnalysisData", strategy_name: str
    ) -> str:
        """StockAnalysisData モデルからプロンプトを生成する。

        ValidationFlag のメタデータを活用し、AI に検証状態を明示する。

        Args:
            stock: StockAnalysisData インスタンス。
            strategy_name: 戦略名。

        Returns:
            生成されたプロンプト文字列。
        """
        row = stock.model_dump()

        flags = stock.validation_flags
        row["tier1_missing_count"] = len(flags.tier1_missing)
        row["tier2_missing_count"] = len(flags.tier2_missing)
        row["red_flag_count"] = len(flags.red_flags)
        row["red_flags_list"] = (
            "、".join(flags.red_flags) if flags.red_flags else "なし"
        )
        row["rescue_status"] = "該当 ✅" if flags.rescue_eligible else "非該当"
        row["pydantic_validated"] = True

        return self.create_prompt(row, strategy_name)

    def get_validation_metadata_section(self, stock: "StockAnalysisData") -> str:
        """ValidationFlag をプロンプト用テキストに変換。

        Args:
            stock: StockAnalysisData インスタンス。

        Returns:
            検証メタデータセクションのテキスト。
        """
        flags = stock.validation_flags
        lines = [
            "【検証メタデータ (Pydantic Validated)】",
            f"- Tier 1 欠損: {len(flags.tier1_missing)}件 ({', '.join(flags.tier1_missing) or 'なし'})",
            f"- Red Flags: {len(flags.red_flags)}件 ({', '.join(flags.red_flags) or 'なし'})",
            f"- 例外救済該当: {'✅ 該当' if flags.rescue_eligible else '非該当'}",
            "※このデータはシステムで事前検証済みです。独自の計算は行わず、提供された数値に基づいて判断してください。",
        ]
        return "\n".join(lines)

    @classmethod
    def build_dossier_analysis_prompt(cls, dossier: dict[str, Any]) -> str:
        """[Agentic Pipeline] 単一の StockDossier から投資判断用プロンプトを生成する。"""
        from src.orchestration.dossier_builder import StockDossierBuilder

        summary_md = StockDossierBuilder.to_markdown_summary(dossier)

        return f"""あなたはプロフェッショナルな株式クオンツ/ファンダメンタルズアナリストです。
提供された銘柄カルテ（データパイプラインにより欠損補完・検証済みのファクトデータ）に基づき、
客観的かつ鋭角な投資判断を行ってください。

{summary_md}

### 分析要件:
1. **ファンダメンタルズの質**: 収益性(ROE, 営業利益率)、割安度(PER, PBR)、財務健全性(自己資本比率)の総合評価。
2. **テクニカルのモメンタム**: RSI(14)や移動平均乖離率、MACD状態から見たエントリータイミング・需給の評価。
3. **リスク要因**: 業種リスク、割高感、業績悪化リスク等の客観的指摘。

### 出力フォーマット (JSON のみを出力してください):
```json
{{
  "code": "{dossier.get("code")}",
  "verdict": "STRONG_BUY | BUY | WATCH | PASS",
  "agent_score": 85.0,
  "investment_thesis": "具体的な投資仮説・推奨理由（150文字程度）",
  "risk_factors": ["リスク要因1", "リスク要因2"],
  "time_horizon": "Short (1〜2週) | Swing (2〜6週) | Mid-term (数ヶ月〜)"
}}
```
"""
