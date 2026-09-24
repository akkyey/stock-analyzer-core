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
    min_trading_value: int | None = None


class CsvMappingConfig(BaseModel):
    """CSVマッピング設定。"""

    col_map: dict[str, str]
    numeric_cols: list[str]


class CircuitBreakerConfig(BaseModel):
    """サーキットブレーカー設定。"""

    consecutive_failure_threshold: int
    reset_timeout: int = 60


class DatabaseConfig(BaseModel):
    """データベース設定。"""

    retention_days: int


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


class MetadataMappingConfig(BaseModel):
    """メタデータマッピング設定。"""

    metrics: dict[str, str] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)


class PathsConfig(BaseModel):
    """パス設定。"""

    db_file: str | None = None
    output_dir: str | None = None


class FinancialRepairConfig(BaseModel):
    """財務修復ロジック設定。"""

    ratio_scaling_threshold: float = Field(default=1.0, ge=0.0)


class ConfigModel(BaseModel):
    """アプリケーション全体の設定モデル。

    YAMLファイルからロードされた設定の型安全性を保証する。
    """

    data: DataConfig
    paths: PathsConfig | None = None
    filter: FilterConfig
    hard_filters: dict[str, float] = Field(default_factory=dict)
    csv_mapping: CsvMappingConfig
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
        return v

    model_config = {
        "extra": "ignore",  # 未知のフィールドは無視
    }

