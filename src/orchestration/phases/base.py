"""パイプラインフェーズの基底クラス

全てのフェーズ（取得・評価・報告）はこのクラスを継承する。
"""

from abc import ABC, abstractmethod
from typing import Any, Optional
import polars as pl

from src.orchestration.context import OrchestratorContext

class BasePhase(ABC):
    """パイプラインフェーズの共通インターフェース"""
    
    def __init__(self, context: OrchestratorContext):
        """
        フェーズを初期化する。
        
        Args:
            context (OrchestratorContext): オーケストレーションの共有コンテキスト。
        """
        self.context = context
        self.logger = context.logger

    @abstractmethod
    def execute(self, df: Optional[pl.DataFrame] = None) -> Optional[pl.DataFrame]:
        """
        フェーズのメイン処理を実行する。
        
        Args:
            df (Optional[pl.DataFrame]): 前のフェーズから渡されたデータ（必要な場合）。
            
        Returns:
            Optional[pl.DataFrame]: 次のフェーズへ渡すデータ（必要な場合）。
        """
        pass

    def log_info(self, message: str):
        """ログ出力（INFOレベル）のヘルパーメソッド"""
        self.logger.info(f"[{self.__class__.__name__}] {message}")

    def log_warn(self, message: str):
        """ログ出力（WARNレベル）のヘルパーメソッド"""
        self.logger.warning(f"[{self.__class__.__name__}] {message}")

    def log_error(self, message: str):
        """ログ出力（ERRORレベル）のヘルパーメソッド"""
        self.logger.error(f"[{self.__class__.__name__}] {message}")

    def _clean_columns(self, df: pl.DataFrame, targets: list[str]) -> pl.DataFrame:
        """指定されたカラムが存在する場合のみ削除するユーティリティ。
        
        Args:
            df (pl.DataFrame): 対象の DataFrame。
            targets (list[str]): 削除候補のカラム名リスト。
            
        Returns:
            pl.DataFrame: クレンジング後の DataFrame。
        """
        if df is None or df.is_empty():
            return df
        
        if isinstance(df, pl.LazyFrame):
            existing_cols = df.collect_schema().names()
        else:
            existing_cols = df.columns
            
        cols_to_drop = [c for c in targets if c in existing_cols]
        if cols_to_drop:
            return df.drop(cols_to_drop)
        return df
    def _verify_integrity(self, df: pl.DataFrame) -> None:
        """DataFrame のスキーマ整合性を検証する。
        
        カラム名に '_left' または '_right' サフィックスが含まれている場合、
        スキーマ隔離の失敗とみなして例外をスローする。
        
        Args:
            df (pl.DataFrame): 検証対象の DataFrame。
            
        Raises:
            RuntimeError: サフィックス付きカラムを検知した場合。
        """
        if df is None:
            return

        if isinstance(df, pl.LazyFrame):
            cols = df.collect_schema().names()
        else:
            cols = df.columns
            
        suffix_cols = [c for c in cols if c.endswith(("_left", "_right"))]
        if suffix_cols:
            self.log_error(f"Schema isolation failure: Suffix detected in {suffix_cols}")
            raise RuntimeError(f"Schema isolation failure: Suffix detected in {suffix_cols}")
