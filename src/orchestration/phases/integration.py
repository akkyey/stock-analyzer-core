"""統合・報告フェーズ (Phase 3)

分析結果を最終レポートとして出力し、
Google Drive / Google Sheets / Discord 等の外部サービスと連携する。
"""

import os
from typing import Any, Dict, Optional

import polars as pl

from src.orchestration.phases.base import BasePhase


class IntegrationPhase(BasePhase):
    """統合・報告フェーズ（GDrive連携強化版）"""

    def execute(
        self, df: Optional[pl.DataFrame | Dict] = None
    ) -> Optional[pl.DataFrame]:
        # dict (data_map) 形式の場合は内部の df_eval を抽出
        if isinstance(df, dict):
            df = df.get("df_eval")

        if df is None or (isinstance(df, pl.DataFrame) and df.is_empty()):
            self.log_warn("No results to integrate. Reporting skipped.")
            return None

        self.log_info(f"Starting Integration Phase for {len(df)} results...")

        # 1. Reporter 互換データへの変換
        report_data = []
        for row in df.to_dicts():
            report_data.append({"latest": row, "data": row})

        # 2. レポート生成
        self.log_info("Generating standard reports...")
        report_context = self.context.config.get("report", {}).get("context", "daily")
        report_paths = self.context.reporter.generate_reports(
            results=report_data, output_context=report_context
        )

        # 3. 物理検証
        self._verify_physical_outputs(report_paths)

        # 4. 外部連携（Google Drive / Sheets / Discord）
        self._sync_external_services(report_paths, df)

        self.log_info("Integration Phase completed successfully.")

        # [v12] Final Output Guard: レポート出力・外部連携直前にスキーマ汚染がないか最終検閲
        self._verify_integrity(df)

        return df

    def _verify_physical_outputs(self, report_paths: Dict[str, Any]):
        """生成ファイルの存在とサイズの検証"""
        for rtype, rpath in report_paths.items():
            if not rpath or not os.path.exists(rpath):
                self.log_error(f"Missing report file: {rtype} at {rpath}")
                continue
            size = os.path.getsize(rpath)
            if size == 0:
                self.log_error(f"Empty report file: {rtype}")
            else:
                self.log_info(f"Report verified: {rtype} ({size} bytes)")

    def _sync_external_services(self, report_paths: Dict[str, Any], df: pl.DataFrame):
        """外部サービスとの同期処理"""
        from src.colab_tools import ColabTools
        from src.orchestration.report_helper import _upload_summary_to_gspread

        # 4-1. Google Drive アップロード (Mandatory Requirement)
        self.log_info("Uploading reports to Google Drive...")
        try:
            # 代表として main_csv をアップロード
            csv_path = report_paths.get("main_csv")
            if csv_path:
                drive_url = ColabTools.upload_file_to_drive(csv_path)
                if drive_url:
                    self.log_info(f"✅ Report uploaded to GDrive: {drive_url}")
                    self.context.report_url = drive_url
        except Exception as e:
            self.log_error(f"Google Drive upload failed: {e}")

        # 4-2. Google Sheets (GSpread) 同期
        self.log_info("Syncing results to Google Sheets...")
        try:
            _upload_summary_to_gspread(self.context, report_paths)
            self.log_info("✅ Google Sheets sync completed.")
        except Exception as e:
            self.log_error(f"Google Sheets sync failed: {e}")

        # 4-3. Discord 通知 (開始通知は Orchestrator が行っているため、ここでは完了詳細)
        try:
            self.context.notifier.notify_success(
                mode="Integration", count=len(df), report_url=self.context.report_url
            )
        except Exception as e:
            self.log_warn(f"Notification failed: {e}")
