"""クオンツ評価モジュール (QuantEvaluator)

個別銘柄データに対し、多変量連続グラデーション（リニア傾斜配点）による
クオンツスコアリングおよび投資判定 (Verdict) を算出する。
"""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class QuantEvaluator:
    """多変量連続グラデーション（リニア傾斜配点）に基づく本格クオンツ評価エンジン。"""

    DEAD_STOCK_SCORE = 45.0
    DEAD_STOCK_VERDICT = "PASS"
    MIN_SCORE = 15.0
    MAX_SCORE = 98.0

    # プリセット定義 (コード内 SSOT: デフォルトは Qiita 記事そのままの 'balanced')
    PRESET_STRATEGIES: Dict[str, Dict[str, float]] = {
        "balanced": {  # 【標準】Qiita 記事そのままの黄金比
            "value_multiplier": 1.0,
            "profitability_multiplier": 1.0,
            "safety_multiplier": 1.0,
            "dividend_multiplier": 1.0,
            "technical_multiplier": 1.0,
        },
        "dividend_focus": {  # 【高配当株ポートフォリオ重視】
            "value_multiplier": 0.8,
            "profitability_multiplier": 1.0,
            "safety_multiplier": 1.2,  # 減配リスク回避のため財務健全性も強化
            "dividend_multiplier": 2.5,  # 配当利回りを最重要視
            "technical_multiplier": 0.5,
        },
        "deep_value": {  # 【グレアム流ディープバリュー重視】
            "value_multiplier": 2.5,  # 低PER・低PBRに最大配点
            "profitability_multiplier": 0.8,
            "safety_multiplier": 1.2,
            "dividend_multiplier": 0.8,
            "technical_multiplier": 0.5,
        },
        "growth_quality": {  # 【高収益クオリティ成長重視】
            "value_multiplier": 0.5,
            "profitability_multiplier": 2.5,  # 高ROEに最大配点
            "safety_multiplier": 1.0,
            "dividend_multiplier": 0.5,
            "technical_multiplier": 1.0,
        },
    }

    # 既知のスコアリング乗数キー
    KNOWN_SCORING_KEYS: set[str] = {
        "value_multiplier",
        "profitability_multiplier",
        "safety_multiplier",
        "dividend_multiplier",
        "technical_multiplier",
    }

    # 各カテゴリの基礎最大配点（合計ジャスト 100.0 点）
    CATEGORY_MAX_POINTS: Dict[str, float] = {
        "profitability_multiplier": 30.0,
        "value_multiplier": 25.0,
        "safety_multiplier": 15.0,
        "dividend_multiplier": 10.0,
        "technical_multiplier": 20.0,
    }

    @classmethod
    def _is_dead_stock(cls, rsi: Optional[float], ma_div: Optional[float]) -> bool:
        """死に株・商い停止（直近で値動きなし）の判定"""
        return (
            rsi is not None
            and abs(rsi - 50.0) < 0.001
            and ma_div is not None
            and abs(ma_div) < 0.001
        )

    @classmethod
    def _score_roe(cls, roe: Optional[float]) -> float:
        """ROE (資本収益性) スコアリング: 最大30点"""
        if roe is None or roe <= 0:
            return 0.0
        if roe >= 25.0:
            return 30.0
        if roe >= 10.0:
            return 15.0 + (roe - 10.0) * (15.0 / 15.0)
        if roe >= 5.0:
            return 5.0 + (roe - 5.0) * (10.0 / 5.0)
        return roe * 1.0

    @classmethod
    def _score_pbr(cls, pbr: Optional[float]) -> float:
        """PBR (割安度) スコアリング: 最大13点"""
        if pbr is None or pbr <= 0:
            return 0.0
        if pbr <= 0.6:
            return 13.0
        if pbr <= 1.0:
            return 10.0 + (1.0 - pbr) * (3.0 / 0.4)
        if pbr <= 2.0:
            return 4.0 + (2.0 - pbr) * (6.0 / 1.0)
        if pbr <= 4.0:
            return max(0.0, 4.0 - (pbr - 2.0) * 2.0)
        return 0.0

    @classmethod
    def _score_per(
        cls,
        per: Optional[float],
        operating_income: Optional[float] = None,
        net_profit: Optional[float] = None,
        operating_margin: Optional[float] = None,
    ) -> float:
        """PER (割安度・過熱ペナルティ・一過性利益抑制) スコアリング: 最大12点"""
        if per is None or per <= 0:
            return 0.0

        # 一過性特別利益（バリュートラップ）の検知・抑制:
        # 1. 本業赤字 (op_margin <= 0 または op_inc <= 0) または営業利益比率が低い (op_inc / net_profit < 0.5) 場合、
        #    低PERは本業収益力ではなく資産売却益等のため、配点を大幅抑制 (0点)
        # 2. 利益詳細が未開示でも PER < 3.0 の極端な低数値は異常値・一過性疑いとして最大2.0点に抑制
        if per < 4.0:
            if operating_margin is not None and operating_margin <= 0:
                return 0.0  # 営業利益率赤字による特別利益トラップ
            if (
                operating_income is not None
                and net_profit is not None
                and net_profit > 0
            ):
                if operating_income <= 0 or (operating_income / net_profit < 0.5):
                    return 0.0  # 本業実力と乖離した特別利益トラップ
            if per < 3.0:
                return 2.0  # 極端な低PERへの安全抑制

        if per <= 6.0:
            return 12.0
        if per <= 10.0:
            return 8.0 + (10.0 - per) * (4.0 / 4.0)
        if per <= 15.0:
            return 4.0 + (15.0 - per) * (4.0 / 5.0)
        if per <= 25.0:
            return max(0.0, 4.0 - (per - 15.0) * 0.4)
        if per > 40.0:
            return -min(3.0, (per - 40.0) * 0.1)
        return 0.0

    @classmethod
    def _score_equity_ratio(cls, eq_ratio: Optional[float]) -> float:
        """自己資本比率 (財務安全性) スコアリング: 最大15点"""
        if eq_ratio is None or eq_ratio <= 0:
            return 0.0
        if eq_ratio >= 80.0:
            return 15.0
        if eq_ratio >= 50.0:
            return 8.0 + (eq_ratio - 50.0) * (7.0 / 30.0)
        if eq_ratio >= 25.0:
            return 2.0 + (eq_ratio - 25.0) * (6.0 / 25.0)
        return max(0.0, eq_ratio * (2.0 / 25.0))

    @classmethod
    def _score_dividend_yield(cls, div_yield: Optional[float]) -> float:
        """配当利回り スコアリング: 最大10点"""
        if div_yield is None or div_yield <= 0:
            return 0.0
        if div_yield >= 5.0:
            return 10.0
        if div_yield >= 3.0:
            return 5.0 + (div_yield - 3.0) * (5.0 / 2.0)
        if div_yield >= 1.5:
            return 1.0 + (div_yield - 1.5) * (4.0 / 1.5)
        return 0.0

    @classmethod
    def _score_macd(cls, macd_hist: Optional[float], macd_status: str) -> float:
        """MACD スコアリング: 最大8点"""
        if macd_hist is None and not macd_status:
            return 3.0
        hist_val = macd_hist if macd_hist is not None else 0.0
        if "Bullish" in macd_status:
            return 6.0 + min(2.0, max(0.0, hist_val * 1.0))
        if "Bearish" in macd_status:
            return max(0.0, 2.0 + min(0.0, hist_val * 0.5))
        return 3.0

    @classmethod
    def _score_rsi(cls, rsi: Optional[float]) -> float:
        """RSI スコアリング: 最大7点 (境界ジャンプなしの滑らかなグラデーション)"""
        if rsi is None:
            return 0.0
        if rsi <= 25.0:
            return 7.0
        if rsi <= 35.0:
            return 5.5 + (35.0 - rsi) * (1.5 / 10.0)
        if rsi <= 50.0:
            return 4.0 + (50.0 - rsi) * (1.5 / 15.0)
        if rsi <= 65.0:
            return 2.5 + (65.0 - rsi) * (1.5 / 15.0)
        if rsi <= 75.0:
            return 2.5 * (75.0 - rsi) / 10.0
        # 75.0超は過熱ペナルティ (75.0の0点から90.0の-3点まで滑らかに減点)
        return max(-3.0, -(rsi - 75.0) * (3.0 / 15.0))

    @classmethod
    def _score_ma_div(cls, ma_div: Optional[float]) -> float:
        """25日移動平均乖離率 スコアリング: 最大5点 (境界ジャンプなしの滑らかなグラデーション)"""
        if ma_div is None:
            return 0.0
        if ma_div < -25.0:
            # -25%未満の大暴落ペナルティ (-25%の0点から-40%の-3点まで滑らかに減点)
            return -min(3.0, (-25.0 - ma_div) * 0.2)
        if ma_div < -15.0:
            # -25%から-15%にかけて反発期待と暴落警戒のグラデーション (0点〜5点)
            return 5.0 - (-15.0 - ma_div) * (5.0 / 10.0)
        if ma_div <= -5.0:
            # -15%で5点満点、-5%で3点
            return 3.0 + (-5.0 - ma_div) * (2.0 / 10.0)
        if ma_div <= 5.0:
            return 3.0
        if ma_div <= 15.0:
            return 3.0 - (ma_div - 5.0) * (1.0 / 10.0)
        if ma_div <= 25.0:
            # 15%の2点から25%の0点まで滑らかに低下
            return 2.0 - (ma_div - 15.0) * (2.0 / 10.0)
        # 25%超の高値掴み過熱ペナルティ (25%の0点から35%の-4点まで滑らかに減点)
        return -min(4.0, (ma_div - 25.0) * 0.4)

    @classmethod
    def _determine_verdict(
        cls,
        score: float,
        roe: Optional[float] = None,
        macd_status: str = "",
        ma_div: Optional[float] = None,
        op_income: Optional[float] = None,
        net_profit: Optional[float] = None,
        operating_margin: Optional[float] = None,
        verdict_mode: str = "grade",
    ) -> str:
        """総合スコアおよびモメンタム・健全性ゲートキーパーに基づく格付けの決定。

        verdict_mode="legacy": STRONG_BUY / BUY / WATCH / PASS (Qiita Part 2 準拠)
        verdict_mode="grade": Grade S / Grade A / Grade B / Grade C (客観的格付け)
        """
        is_grade = (verdict_mode == "grade")

        # ベース判定
        if score >= 80.0:
            verdict = "Grade S" if is_grade else "STRONG_BUY"
        elif score >= 65.0:
            verdict = "Grade A" if is_grade else "BUY"
        elif score >= 50.0:
            verdict = "Grade B" if is_grade else "WATCH"
        else:
            verdict = "Grade C" if is_grade else "PASS"

        # ゲートキーパー 1: 実績赤字（ROE < 0）銘柄のキャップ制限
        if roe is not None and roe < 0 and verdict in ["Grade S", "Grade A", "STRONG_BUY", "BUY"]:
            verdict = "Grade B" if is_grade else "WATCH"

        # ゲートキーパー 2: 本業赤字・一過性特益トラップのキャップ制限
        is_op_loss = False
        if operating_margin is not None and operating_margin <= 0:
            is_op_loss = True
        elif op_income is not None and op_income <= 0:
            is_op_loss = True

        if is_op_loss and verdict in ["Grade S", "Grade A", "STRONG_BUY", "BUY"]:
            verdict = "Grade B" if is_grade else "WATCH"

        # ゲートキーパー 3: テクニカル・モメンタム足切り
        # 下降トレンド中（Bearish）の銘柄は反発確認前のリスクがあるため、最高評価を禁止
        # さらに 25日乖離率が -10.0% を下回る深い下降トレンド中の場合は最大 Grade B / WATCH に制限
        if "Bearish" in macd_status:
            if verdict in ["Grade S", "STRONG_BUY"]:
                verdict = "Grade A" if is_grade else "BUY"
            if ma_div is not None and ma_div < -10.0 and verdict in ["Grade A", "BUY"]:
                verdict = "Grade B" if is_grade else "WATCH"

        return verdict

    @classmethod
    def resolve_multipliers(
        cls, config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, float]:
        """設定から投資戦略プリセットと個別乗数を解決し、検証済みの有効乗数辞書を返す。"""
        preset_name = (config or {}).get("strategy_preset", "balanced")
        if preset_name not in cls.PRESET_STRATEGIES:
            logger.warning(
                f"⚠️ 未知のプリセット '{preset_name}' が指定されました。'balanced' を適用します。"
            )
            preset_name = "balanced"

        base_multipliers = cls.PRESET_STRATEGIES[preset_name].copy()
        custom_multipliers = (config or {}).get("scoring_multipliers", {})

        # 未知キー検知
        unknown_scoring_keys = set(custom_multipliers.keys()) - cls.KNOWN_SCORING_KEYS
        if unknown_scoring_keys:
            logger.warning(
                f"⚠️ scoring_multipliers に未知のキーが含まれています (無視されます): {unknown_scoring_keys}"
            )

        effective_multipliers = {**base_multipliers, **custom_multipliers}

        # 負値ガード
        for k in cls.KNOWN_SCORING_KEYS:
            val = float(effective_multipliers.get(k, 1.0))
            if val < 0.0:
                logger.warning(
                    f"⚠️ 乗数 '{k}' に負の値 ({val}) が指定されたため、0.0 に補正しました。"
                )
                val = 0.0
            effective_multipliers[k] = val

        return effective_multipliers

    @classmethod
    def evaluate(
        cls,
        dossier: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        multipliers: Optional[Dict[str, float]] = None,
    ) -> Tuple[float, str]:
        """銘柄データからスコア・判定を算出する。

        Args:
            dossier (Dict[str, Any]): 銘柄の財務・テクニカル指標群
            config (Optional[Dict[str, Any]]): 投資スタイルプリセットおよび乗数設定
            multipliers (Optional[Dict[str, float]]): 事前計算済みの有効乗数辞書 (ループ最適化用)

        Returns:
            Tuple[float, str]: (クオンツスコア [15.0, 98.0], 投資判断 Verdict)
        """
        f = dossier.get("fundamentals", {})
        t = dossier.get("technicals", {})

        rsi = t.get("rsi_14")
        ma_div = t.get("ma25_divergence")
        macd_hist = t.get("macd_hist")
        macd_status = t.get("macd_status", "")

        per = f.get("per")
        pbr = f.get("pbr")
        roe = f.get("roe")
        eq_ratio = f.get("equity_ratio")
        div_yield = f.get("dividend_yield")
        op_margin = f.get("operating_margin")
        op_income = f.get("operating_income")
        net_profit = f.get("net_profit")

        # 1. 死に株・商い停止判定
        if cls._is_dead_stock(rsi, ma_div):
            return cls.DEAD_STOCK_SCORE, cls.DEAD_STOCK_VERDICT

        # 2. 各カテゴリの基礎スコア計算 (合計 100.0 点)
        # ① 資本収益性 (最大 30.0 点)
        s_prof = cls._score_roe(roe)

        # ② 割安度 (最大 25.0 点: PBR 13点 + PER 12点)
        s_val = cls._score_pbr(pbr) + cls._score_per(
            per,
            operating_income=op_income,
            net_profit=net_profit,
            operating_margin=op_margin,
        )

        # ③ 財務健全性 (最大 15.0 点)
        s_safe = cls._score_equity_ratio(eq_ratio)

        # ④ 株主還元 (最大 10.0 点)
        s_div = cls._score_dividend_yield(div_yield)

        # ⑤ モメンタム・テクニカル (最大 20.0 点: MACD 8点 + RSI 7点 + 乖離率 5点)
        s_tech = (
            cls._score_macd(macd_hist, macd_status)
            + cls._score_rsi(rsi)
            + cls._score_ma_div(ma_div)
        )

        # 3. 乗数設定の解決 (事前解決済み乗数があれば優先し、銘柄ごとの重複警告を根絶)
        effective_multipliers = (
            multipliers
            if multipliers is not None
            else cls.resolve_multipliers(config)
        )

        # 4. スコア計算 (Zero-Break バイパス & 加重平均正規化)
        is_default_weights = all(
            effective_multipliers[k] == 1.0 for k in cls.KNOWN_SCORING_KEYS
        )

        if is_default_weights:
            # 【Zero-Break バイパス】乗数がすべて 1.0 の場合は正規化（除算・乗算）を通さず既存加算をそのまま実行
            raw_score = s_prof + s_val + s_safe + s_div + s_tech
        else:
            total_weighted_max = sum(
                cls.CATEGORY_MAX_POINTS[k] * effective_multipliers[k]
                for k in cls.KNOWN_SCORING_KEYS
            )
            if total_weighted_max <= 0:
                logger.warning(
                    "⚠️ 全乗数の合計配点が0以下です。デフォルト配点を適用します。"
                )
                raw_score = s_prof + s_val + s_safe + s_div + s_tech
            else:
                weighted_score = (
                    s_prof * effective_multipliers["profitability_multiplier"]
                    + s_val * effective_multipliers["value_multiplier"]
                    + s_safe * effective_multipliers["safety_multiplier"]
                    + s_div * effective_multipliers["dividend_multiplier"]
                    + s_tech * effective_multipliers["technical_multiplier"]
                )
                raw_score = (weighted_score / total_weighted_max) * 100.0

        score = round(min(cls.MAX_SCORE, max(cls.MIN_SCORE, raw_score)), 1)

        # 5. 判定 (Verdict) の決定（ゲートキーパー適用）
        verdict_mode = (config or {}).get("verdict_mode", "grade")
        verdict = cls._determine_verdict(
            score,
            roe=roe,
            macd_status=macd_status,
            ma_div=ma_div,
            op_income=op_income,
            net_profit=net_profit,
            operating_margin=op_margin,
            verdict_mode=verdict_mode,
        )

        return score, verdict
