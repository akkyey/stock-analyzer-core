"""ValidationEngine: セクター別バリデーションポリシーエンジン

責務:
- タスクデータのバリデーション（プロンプト内容の不備チェック）
- セクター別ポリシー（欠損許容 na_allowed 等）の適用
- AI分析前のデータ品質チェック (旧 DataValidator の機能)
- 並列バッチ処理によるバリデーション実行
"""

from logging import getLogger
from typing import TYPE_CHECKING, Any, Optional, cast

if TYPE_CHECKING:
    from src.config_schema import SectorPolicy
    from src.domain.models import StockAnalysisData


class ValidationEngine:
    """設定に基づいてバリデーションポリシーを管理するエンジン。

    Attributes:
        config (Dict[str, Any]): システム設定。
        sector_policies (Dict[str, Any]): セクターごとのバリデーションルール。
        default_policy (Dict[str, Any]): デフォルトのポリシー。
        mapping (Dict[str, Any]): メタデータマッピング定義。
        metrics_map (Dict[str, str]): ラベル名と内部フィールド名の対応マップ。
        val_config (Dict[str, Any]): バリデーションに関する設定値。
    """

    def __init__(
        self,
        context: Any = None,
        debug_mode: bool = False,
    ):
        """ValidationEngine を初期化する。

        Args:
            context (Any): OrchestratorContext または config 辞書。
        """
        self.logger = getLogger(__name__)
        self.context = context
        self.debug_mode = debug_mode
        self._policy_map: dict[str, "SectorPolicy"] = {}
        self.config: dict[str, Any] = {}

        if self.context:
            if isinstance(self.context, dict):
                self.config = self.context
            else:
                self.config = getattr(self.context, "config", {})
        else:
            self.config = {}

        self.sector_policies = self.config.get("sector_policies", {})
        self.default_policy = self.sector_policies.get(
            "default",
            {"na_allowed": [], "score_exemptions": [], "ai_prompt_excludes": []},
        )

        # 設定からマッピング情報をロード
        self.mapping = self.config.get("metadata_mapping") or {}
        self.metrics_map = self.mapping.get("metrics", {})
        self.val_config = self.mapping.get("validation", {})

        # 設定が空の場合のフォールバック (後方互換性)
        if not self.metrics_map:
            self.metrics_map = {
                "Op CF Margin": "operating_cf",
                "Debt/Equity Ratio": "debt_equity_ratio",
                "Free CF": "free_cf",
                "Operating Margin": "operating_margin",
                "ROE": "roe",
                "PER": "per",
                "PBR": "pbr",
            }

    def get_policy(self, sector: str) -> dict[str, Any]:
        """指定したセクターのバリデーションポリシーを取得する。

        Args:
            sector (str): セクター名。

        Returns:
            Dict[str, Any]: 該当セクターのポリシー辞書（未定義ならデフォルトを返す）。
        """
        return cast(
            dict[str, Any],
            self.sector_policies.get(sector, self.sector_policies.get("default", {})),
        )

    def get_ai_excludes(self, sector: str) -> list[str]:
        policy = self.get_policy(sector)
        return cast(list[str], policy.get("ai_prompt_excludes", []))

    def get_score_exemptions(self, sector: str) -> list[str]:
        policy = self.get_policy(sector)
        return cast(list[str], policy.get("score_exemptions", []))

    def check_sector_coverage(self, db_sectors: list[str]) -> None:
        defined_sectors = set(self.sector_policies.keys()) - {"default"}
        db_sector_set = set(db_sectors)
        undefined = db_sector_set - defined_sectors
        if undefined:
            self.logger.warning(
                f"⚠️ The following sectors are not defined in sector_policies: {undefined}"
            )

    # ============================================================
    # [v2.2] Polars によるベクトル化バリデーション (High Performance)
    # ============================================================
    def validate_batch_polars(self, df: Any, strategy: str | None = None) -> Any:
        """Polars DataFrame に対して一括でバリデーションとフィルタリングを行う。

        Args:
            df (pl.DataFrame): 入力データ
            strategy (str): 戦略名

        Returns:
            pl.DataFrame: 有効な銘柄のみを含む DataFrame (検知結果を列として追加)
        """
        import polars as pl

        # 1. 基本的な足切り条件 (SkipReason) のベクトル化判定
        # INSOLVENT: 自己資本比率 < 0
        # EXTREME_VALUATION: PER > 500 or PBR > 20
        # NEGATIVE_OCF_EXTREME: 営業CFマージン < -10
        # PAYOUT_UNSUSTAINABLE: 配当性向 > 300

        # 営業CFマージンの計算 (モデル側にあったロジックを式に展開)
        # ocf_margin = (operating_cf / sales) * 100 などの想定だが、
        # 入力DFに既にある場合はそれを活用

        # テクニカル異常値チェック (RSI 範囲外等)
        anomalous_rsi = (
            pl.col("rsi_14").is_not_null()
            & ((pl.col("rsi_14") < 0) | (pl.col("rsi_14") > 100))
            if "rsi_14" in df.columns
            else pl.lit(False)
        )

        condition_skip = (
            (pl.col("equity_ratio").is_not_null() & (pl.col("equity_ratio") < 0))
            | (pl.col("per").is_not_null() & (pl.col("per") > 500))
            | (pl.col("pbr").is_not_null() & (pl.col("pbr") > 20))
            | (pl.col("payout_ratio").is_not_null() & (pl.col("payout_ratio") > 300))
            | anomalous_rsi
        )

        # 2. Critical項目の欠損チェック (Tier 1)
        tier1_fields = [
            "price",
            "operating_cf",
            "operating_margin",
            "per",
            "pbr",
            "roe",
        ]
        condition_missing = pl.any_horizontal(pl.col(tier1_fields).is_null())

        # デバッグモード時は緩和
        if self.debug_mode:
            condition_valid = pl.lit(True)  # 全パス
        else:
            condition_valid = ~condition_skip & ~condition_missing

        # 3. Red Flag 判定
        # negative_ocf_margin: 営業CF < 0
        # declining_sales: 売上成長率 < 0
        # extreme_overvaluation: PER > 30 and PBR > 5
        df = df.with_columns(
            [
                (pl.col("operating_cf") < 0).alias("flag_negative_ocf"),
                (pl.col("sales_growth") < 0).alias("flag_declining_sales"),
                ((pl.col("per") > 30) & (pl.col("pbr") > 5)).alias("flag_overvalued"),
            ]
        )

        # 4. スコア整合性チェック (戦略別)
        # 簡易的に各行に整合性不備があるかのフラグを立てる
        if strategy == "growth_quality":
            mismatch = (pl.col("score_growth") < 10) & (pl.col("score_trend") > 70)
        elif strategy == "value_strict":
            mismatch = pl.col("score_value") < 15
        elif strategy == "value_growth_hybrid":
            mismatch = (pl.col("score_value") < 10) & (pl.col("score_growth") < 10)
        else:
            mismatch = pl.lit(False)

        # 最終フィルタリング
        return df.filter(condition_valid & ~mismatch)

    def validate_stock_data(
        self,
        data: dict[str, Any],
        stock: Optional["StockAnalysisData"] = None,
        strategy: str | None = None,
    ) -> tuple[bool, list[str]]:
        """株価データのバリデーションを行う。 (互換性維持用)"""
        from src.domain.models import StockAnalysisData

        if stock is None:
            try:
                stock = StockAnalysisData(**data)
            except Exception as e:
                return False, [f"Data Validation Error: {e}"]

        na_allowed = self._apply_sector_and_strat_exemptions(stock, data, strategy)

        # 1. Critical & Skip Checks
        ok, reasons = self._check_tier1_and_skip(stock, na_allowed)
        if not ok:
            return False, reasons

        # 2. Warnings & Consistency
        issues = self._build_warning_issues(stock, na_allowed)
        issues.extend(self._check_score_consistency(data, strategy))

        if any("Score Mismatch" in i for i in issues):
            return False, issues

        return True, issues

    def _apply_sector_and_strat_exemptions(
        self, stock: "StockAnalysisData", data: dict, strategy: str | None
    ) -> set[str]:
        """セクターと戦略に応じた免除フィールドの集合を取得"""
        sector = data.get("sector", stock.sector)
        policy = self.get_policy(sector)
        na_allowed = set(policy.get("na_allowed", []))
        if strategy:
            strat_policy_key = f"_strategy_{strategy}"
            if strat_policy_key in self.sector_policies:
                na_allowed.update(
                    self.sector_policies[strat_policy_key].get("na_allowed", [])
                )
        return na_allowed

    def _check_tier1_and_skip(
        self, stock: "StockAnalysisData", na_allowed: set[str]
    ) -> tuple[bool, list[str]]:
        """Tier 1 欠損と足切り条件のチェック"""
        flags = stock.validation_flags
        tier1_missing = [f for f in flags.tier1_missing if f not in na_allowed]
        if tier1_missing:
            # [v17.7] デバッグモード（かつ AI 分析スキップ時など）はバリデーションを緩和
            if self.debug_mode:
                if self.context:
                    self.context.logger.debug(
                        f"  [v17.7] (SKIP VALIDATION) Missing Critical in debug mode: {', '.join(tier1_missing)}"
                    )
                return True, []
            return False, [f"Missing Critical: {', '.join(tier1_missing)}"]
        if stock.should_skip_analysis:
            # [v17.7] デバッグモード時は足切り理由も記録のみしてパスさせる
            if self.debug_mode:
                if self.context:
                    self.context.logger.debug(
                        f"  [v17.7] (SKIP SKIP_REASON) Skipping skip-reason in debug mode: {', '.join([r.value for r in flags.skip_reasons])}"
                    )
                return True, []
            return False, [r.value for r in flags.skip_reasons]
        return True, []

    def _build_warning_issues(
        self, stock: "StockAnalysisData", na_allowed: set[str]
    ) -> list[str]:
        """Tier 2 欠損と Red Flag の警告リスト作成"""
        issues = []
        flags = stock.validation_flags
        for field in flags.tier2_missing:
            if field not in na_allowed:
                issues.append(f"Missing Reference: {field}")
        for flag in flags.red_flags:
            issues.append(f"Red Flag: {flag}")
        return issues

    def _check_score_consistency(
        self, data: dict[str, Any], strategy: str | None
    ) -> list[str]:
        """スコアの整合性をチェックする。

        Args:
            data: 検証対象のデータ辞書。
            strategy: 戦略名。

        Returns:
            List[str]: 検出された問題点リスト。
        """
        issues: list[str] = []
        s_val = float(data.get("score_value") or 0)
        s_gro = float(data.get("score_growth") or 0)
        s_trd = float(data.get("score_trend") or 0)
        is_repaired = data.get("is_repaired", False)

        if not strategy or is_repaired:
            return issues

        if strategy == "growth_quality":
            if s_gro < 10 and s_trd > 70:
                issues.append(
                    f"Score Mismatch: Low Growth({s_gro}) vs High Trend({s_trd})"
                )
        elif strategy == "value_strict":
            if s_val < 15:
                issues.append(
                    f"Score Mismatch: Low Value Score ({s_val}) for Value Strategy"
                )
        elif strategy == "value_growth_hybrid" and s_val < 10 and s_gro < 10:
            issues.append(
                f"Score Mismatch: Low Hybrid Scores (Val:{s_val}, Gro:{s_gro})"
            )

        return issues
