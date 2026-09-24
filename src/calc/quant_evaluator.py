"""クオンツ評価モジュール (QuantEvaluator)

個別銘柄データに対し、多変量連続グラデーション（リニア傾斜配点）による
クオンツスコアリングおよび投資判定 (Verdict) を算出する。
"""

from typing import Any, Dict, Optional, Tuple


class QuantEvaluator:
    """多変量連続グラデーション（リニア傾斜配点）に基づく本格クオンツ評価エンジン。"""

    DEAD_STOCK_SCORE = 45.0
    DEAD_STOCK_VERDICT = "PASS"
    MIN_SCORE = 15.0
    MAX_SCORE = 98.0

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
        roe: Optional[float],
        macd_status: str,
        ma_div: Optional[float],
        op_income: Optional[float] = None,
        net_profit: Optional[float] = None,
        operating_margin: Optional[float] = None,
    ) -> str:
        """総合スコアおよびモメンタム・健全性ゲートキーパーに基づく投資判断 (Verdict) の決定"""
        # ベース判定
        if score >= 80.0:
            verdict = "STRONG_BUY"
        elif score >= 65.0:
            verdict = "BUY"
        elif score >= 50.0:
            verdict = "WATCH"
        else:
            verdict = "PASS"

        # ゲートキーパー 1: 実績赤字（ROE < 0）銘柄のキャップ制限
        # 会社予想で黒字転換見込み（PER算出可能）であっても、実績赤字の銘柄は BUY / STRONG_BUY を禁止し最大 WATCH に制限
        if roe is not None and roe < 0 and verdict in ["STRONG_BUY", "BUY"]:
            verdict = "WATCH"

        # ゲートキーパー 2: 本業赤字・一過性特益トラップのキャップ制限
        # 営業利益率または営業利益が赤字かつ純利益が黒字（または低PER割安に見える）銘柄は、
        # 特別利益による見かけの黒字・高ROEであるため BUY / STRONG_BUY を禁止し WATCH に制限
        is_op_loss = False
        if operating_margin is not None and operating_margin <= 0:
            is_op_loss = True
        elif op_income is not None and op_income <= 0:
            is_op_loss = True

        if is_op_loss and verdict in ["STRONG_BUY", "BUY"]:
            verdict = "WATCH"

        # ゲートキーパー 3: テクニカル・モメンタム足切り
        # 下降トレンド中（Bearish）の銘柄は反発確認前の押し目リスクがあるため、STRONG_BUY を禁止（最大 BUY 止まり）
        # さらに 25日乖離率が -10.0% を下回る深い下降トレンド中の場合は最大 WATCH に制限
        if "Bearish" in macd_status:
            if verdict == "STRONG_BUY":
                verdict = "BUY"
            if ma_div is not None and ma_div < -10.0 and verdict == "BUY":
                verdict = "WATCH"

        return verdict

    @classmethod
    def evaluate(cls, dossier: Dict[str, Any]) -> Tuple[float, str]:
        """銘柄データからスコア・判定を算出する。"""
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

        # 2. 各カテゴリのスコア計算
        raw_score = (
            cls._score_roe(roe)
            + cls._score_pbr(pbr)
            + cls._score_per(
                per,
                operating_income=op_income,
                net_profit=net_profit,
                operating_margin=op_margin,
            )
            + cls._score_equity_ratio(eq_ratio)
            + cls._score_dividend_yield(div_yield)
            + cls._score_macd(macd_hist, macd_status)
            + cls._score_rsi(rsi)
            + cls._score_ma_div(ma_div)
        )
        score = round(min(cls.MAX_SCORE, max(cls.MIN_SCORE, raw_score)), 1)

        # 3. 判定 (Verdict) の決定（ゲートキーパー適用）
        verdict = cls._determine_verdict(
            score,
            roe=roe,
            macd_status=macd_status,
            ma_div=ma_div,
            op_income=op_income,
            net_profit=net_profit,
            operating_margin=op_margin,
        )

        return score, verdict
