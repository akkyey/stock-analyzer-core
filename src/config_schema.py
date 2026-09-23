"""設定スキーマ定義

Pydantic を使用して設定ファイルの構造とバリデーションを定義する。
具体的なデフォルト値は config/defaults.yaml に定義し、本ファイルは型定義に専念する。

[v13.0] pydantic-settings への移行準備
- BaseSettings 対応可能な構造に維持
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class DataConfig(BaseModel):
    """データパス設定。"""

    jp_stock_list: str
    output_path: str


class FilterConfig(BaseModel):
    """フィルタ設定。"""

    max_rsi: int | None = None
    min_quant_score: int | None = None
    min_trading_value: int | None = None


class CsvMappingConfig(BaseModel):
    """CSVマッピング設定。"""

    col_map: dict[str, str]
    numeric_cols: list[str]


class MetricMetadata(BaseModel):
    """指標メタデータ定義。"""

    direction: str = "higher"
    category: str = "quality"
    missing_penalty: float = 0.0


class StrategyConfig(BaseModel):
    """投資戦略設定。"""

    default_style: str
    persona: str
    default_horizon: str
    base_score: int
    min_requirements: dict[str, float]
    points: dict[str, int]
    thresholds: dict[str, float]
    metrics_metadata: dict[str, MetricMetadata] = Field(default_factory=dict)
    status_bonuses: dict[str, dict[str, float]] = Field(default_factory=dict)


class AIConfig(BaseModel):
    """AI分析設定。"""

    model_name: str
    max_concurrency: int = Field(ge=1)
    interval_sec: float = Field(ge=0.0)
    validity_days: int
    refresh_triggers: dict[str, float] = Field(default_factory=dict)


class CircuitBreakerConfig(BaseModel):
    """サーキットブレーカー設定。"""

    consecutive_failure_threshold: int
    reset_timeout: int


class DatabaseConfig(BaseModel):
    """データベース設定。"""

    retention_days: int


class APISettingsConfig(BaseModel):
    """API設定。"""

    gemini_tier: str


class GDriveConfig(BaseModel):
    """Google Drive 連携設定。"""

    enabled: bool = True
    use_shared_drive: bool = False
    shared_folder_id: str | None = None
    report_spreadsheet_id: str | None = None


class SectorPolicy(BaseModel):
    """セクターごとのポリシー設定。"""

    na_allowed: list[str] = Field(default_factory=list)
    score_exemptions: list[str] = Field(default_factory=list)
    ai_prompt_excludes: list[str] = Field(default_factory=list)


class ScoringConfig(BaseModel):
    """スコアリング設定。"""

    lower_is_better: list[str] = Field(default_factory=list)
    min_coverage_pct: int | None = None


class ScoringV2Config(BaseModel):
    """スコアリング v2 設定。"""

    macro: dict[str, str] = Field(default_factory=dict)
    styles: dict[str, dict[str, float]] = Field(default_factory=dict)
    tech_points: dict[str, int] = Field(default_factory=dict)
    penalty_rules: dict[str, Any] = Field(default_factory=dict)


class MetadataMappingConfig(BaseModel):
    """メタデータマッピング設定。"""

    metrics: dict[str, str]
    validation: dict[str, Any]


class PathsConfig(BaseModel):
    """パス設定。"""

    db_file: str | None = None
    output_dir: str | None = None


class FinancialRepairConfig(BaseModel):
    """財務修復ロジック設定。"""

    ratio_scaling_threshold: float = Field(default=10.0, ge=0.0)


class ConfigModel(BaseModel):
    """アプリケーション全体の設定モデル。

    YAMLファイルからロードされた設定の型安全性を保証する。
    """

    api_settings: APISettingsConfig
    current_strategy: str
    use_polars: bool = False
    data: DataConfig
    paths: PathsConfig | None = None
    filter: FilterConfig
    hard_filters: dict[str, float] = Field(default_factory=dict)
    csv_mapping: CsvMappingConfig
    scoring: ScoringConfig
    scoring_v2: ScoringV2Config | None = None
    strategies: dict[str, StrategyConfig]
    ai: AIConfig
    circuit_breaker: CircuitBreakerConfig
    database: DatabaseConfig
    financial_repair: FinancialRepairConfig = Field(
        default_factory=FinancialRepairConfig
    )
    gdrive: GDriveConfig | None = None
    sector_policies: dict[str, SectorPolicy] = Field(default_factory=dict)
    sector_risks: dict[str, str] = Field(default_factory=dict)
    metadata_mapping: MetadataMappingConfig | None = None

    @field_validator("sector_policies")
    @classmethod
    def validate_sector_policies(cls, v):
        """セクターポリシーのバリデーション。"""
        # default キーがなくても許容
        return v

    model_config = {
        "extra": "ignore",  # 未知のフィールドは無視
    }
