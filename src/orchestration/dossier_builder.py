"""StockDossier 生成モジュール (Agentic Pipeline)

スクリプト層（Polars / DuckDB）で補完・確定された財務・テクニカル指標データを、
AI エージェント（アナリスト）が最も解釈・推論しやすい「高密度銘柄カルテ (Stock Dossier)」
へと構造化・変換する。
"""

from typing import Any, Dict, List, Optional

import polars as pl


class StockDossierBuilder:
    """銘柄カルテ (Stock Dossier) 構築クラス。"""

    @classmethod
    def _extract_technical_triggers(cls, row: Dict[str, Any]) -> List[str]:
        """テクニカル指標からのトリガー抽出"""
        triggers: List[str] = []
        rsi = row.get("rsi_14")
        if rsi is not None:
            if rsi <= 30.0:
                triggers.append(f"RSI売られすぎ圏 ({rsi:.1f})")
            elif rsi >= 70.0:
                triggers.append(f"RSI買われすぎ圏・過熱感 ({rsi:.1f})")

        vol_ratio = row.get("volume_ratio")
        if vol_ratio is not None and vol_ratio >= 2.0:
            triggers.append(f"出来高急増 ({vol_ratio:.1f}倍)")

        ma_div = row.get("ma_divergence")
        if ma_div is not None and abs(ma_div) >= 10.0:
            direction = "上方" if ma_div > 0 else "下方"
            triggers.append(f"25日移動平均乖離 ({direction}{abs(ma_div):.1f}%)")

        macd = row.get("macd")
        macd_sig = row.get("macd_signal")
        if macd is not None and macd_sig is not None and macd > macd_sig:
            triggers.append("MACDゴールデンクロス圏")

        return triggers

    @classmethod
    def _extract_fundamental_triggers(cls, row: Dict[str, Any]) -> List[str]:
        """ファンダメンタルズ指標からのトリガー抽出"""
        triggers: List[str] = []
        roe = row.get("roe")
        if roe is not None and roe >= 10.0:
            triggers.append(f"高ROE ({roe:.1f}%)")

        equity_ratio = row.get("equity_ratio")
        if equity_ratio is not None and equity_ratio >= 50.0:
            triggers.append(f"健全財務 (自己資本比率 {equity_ratio:.1f}%)")

        per = row.get("per")
        if per is not None and 0.0 < per < 3.0:
            triggers.append(f"極端な低PER・一過性益疑い ({per:.1f}倍)")
        elif per is not None and 3.0 <= per <= 12.0:
            triggers.append(f"低PER割安水準 ({per:.1f}倍)")

        pbr = row.get("pbr")
        if pbr is not None and 0.0 < pbr < 1.0:
            triggers.append(f"PBR1倍割れ ({pbr:.2f}倍)")

        if row.get("is_turnaround"):
            triggers.append("営業利益黒字転換 (ターンアラウンド)")

        return triggers

    @classmethod
    def build_trigger_reasons(cls, row: Dict[str, Any]) -> List[str]:
        """各種指標から、一次選定された理由（着目トリガー）を人間・AIに分かりやすくタグ化する。"""
        return cls._extract_technical_triggers(row) + cls._extract_fundamental_triggers(
            row
        )

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        """DataFrame の 1 行（辞書形式）から単一の StockDossier を構築する。"""

        # 数値の丸め・Null 安全処理
        def _round(val: Any, digits: int = 2) -> Optional[float]:
            if val is None:
                return None
            try:
                f = float(val)
                import math

                if math.isnan(f) or math.isinf(f):
                    return None
                return round(f, digits)
            except (ValueError, TypeError):
                return None

        # MACD 状態テキスト (macd / macd_signal または macd_hist から判定)
        macd = row.get("macd")
        macd_sig = row.get("macd_signal")
        macd_hist = row.get("macd_hist")

        macd_status = "Neutral"
        if macd is not None and macd_sig is not None:
            if macd > macd_sig:
                macd_status = "Bullish (Cross Above)"
            elif macd < macd_sig:
                macd_status = "Bearish (Below Signal)"
        elif macd_hist is not None:
            try:
                hist_val = float(macd_hist)
                if hist_val > 0.0001:
                    macd_status = "Bullish (Above Signal)"
                elif hist_val < -0.0001:
                    macd_status = "Bearish (Below Signal)"
            except (ValueError, TypeError):
                pass

        return {
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "Unknown")),
            "sector": str(row.get("sector", "その他")),
            "market": str(row.get("market", "TSE")),
            "fundamentals": {
                "market_cap": _round(row.get("market_cap"), 1),
                "per": _round(row.get("per"), 1),
                "pbr": _round(row.get("pbr"), 2),
                "roe": _round(row.get("roe"), 1),
                "dividend_yield": _round(row.get("dividend_yield"), 2),
                "equity_ratio": _round(row.get("equity_ratio"), 1),
                "operating_margin": _round(row.get("operating_margin"), 1),
                "sales": _round(row.get("sales"), 0),
                "operating_income": _round(row.get("operating_income"), 0),
                "net_profit": _round(row.get("net_profit"), 0),
                "net_profit_growth": _round(row.get("profit_growth_raw"), 1),
            },
            "technicals": {
                "price": _round(row.get("price"), 1),
                "rsi_14": _round(row.get("rsi_14"), 1),
                "macd_hist": _round(row.get("macd_hist"), 2),
                "macd_status": macd_status,
                "ma25_divergence": _round(row.get("ma_divergence"), 1),
                "volume_ratio": _round(row.get("volume_ratio"), 2),
                "trend_signal": row.get("trend_signal"),
            },
            "trigger_reasons": cls.build_trigger_reasons(row),
        }

    @classmethod
    def from_dataframe(
        cls, df: pl.DataFrame, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Polars DataFrame から StockDossier リストを構築する。limit 未指定時は全件。"""
        if df.is_empty():
            return []

        target_df = df.head(limit) if limit is not None and limit > 0 else df
        records = target_df.to_dicts()
        return [cls.from_row(r) for r in records]

    @classmethod
    def to_markdown_summary(cls, dossier: Dict[str, Any]) -> str:
        """エージェントのプロンプトに埋め込みやすい Markdown 形式の銘柄サマリを生成する。"""
        f = dossier.get("fundamentals", {})
        t = dossier.get("technicals", {})
        triggers = ", ".join(dossier.get("trigger_reasons", [])) or "特記事項なし"

        md = f"""### 銘柄: {dossier.get("name")} ({dossier.get("code")}) [{dossier.get("sector")} / {dossier.get("market")}]
- **現在株価**: {t.get("price")} 円 (出来高倍率: {t.get("volume_ratio")}x)
- **テクニカル**: RSI(14)={t.get("rsi_14")}, 25日乖離率={t.get("ma25_divergence")}%, MACD={t.get("macd_status")}
- **ファンダメンタルズ**: PER={f.get("per")}倍, PBR={f.get("pbr")}倍, ROE={f.get("roe")}%, 配当利回り={f.get("dividend_yield")}%, 自己資本比率={f.get("equity_ratio")}%
- **業績水準**: 営業利益率={f.get("operating_margin")}%, 純利益成長率={f.get("net_profit_growth")}%
- **着目トリガー**: {triggers}
"""
        return md
