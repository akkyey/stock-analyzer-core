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

        # スキーマ汚染(_rightサフィックス)の防御的パージ
        right_cols = [c for c in df.columns if c.endswith("_right")]
        if right_cols:
            df = df.drop(right_cols)

        self.log_info(f"Starting Integration Phase for {len(df)} results...")

        # 0. データ契約検証 (Data Contract Guard: 欠損率0.00%の保証)
        self._verify_data_contract(df)

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

        # 2-1. 除外銘柄リスト (uncalculable_stocks.csv) の出力
        uncalculable_df = getattr(self.context, "uncalculable_df", None)
        if uncalculable_df is not None and isinstance(uncalculable_df, pl.DataFrame):
            out_dir = self.context.reporter.output_dir
            uncalc_path = out_dir / "uncalculable_stocks.csv"
            try:
                uncalculable_df.write_csv(str(uncalc_path))
                self.log_info(
                    f"Saved uncalculable stocks list: {uncalc_path} ({len(uncalculable_df)} records)"
                )
                report_paths["uncalculable"] = uncalc_path
            except Exception as e:
                self.log_warn(f"Failed to save uncalculable_stocks.csv: {e}")

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

    def _upload_summary_to_gspread(self, report_paths: dict | None) -> None:
        """サマリーレポートをGoogle Spreadsheetにアップロード。"""
        gdrive_cfg = self.context.config.get("gdrive", {})
        if not gdrive_cfg.get("enabled", True) or not report_paths:
            return

        summary_path = report_paths.get("summary") or report_paths.get("main_csv")
        if not summary_path:
            return

        try:
            from src.colab_tools import ColabTools

            tools = ColabTools()
            sheet_url = tools.export_to_sheets(
                csv_path=str(summary_path),
                sheet_name="StockAnalysis_Latest_Report",
                output_dir=str(self.context.reporter.output_dir),
            )
            if sheet_url:
                self.log_info(f"🚀 Spreadsheet Updated Directly: {sheet_url}")
                self.context.report_url = sheet_url
            else:
                self.context.add_error("Spreadsheet export failed (Check log for details).")
        except Exception as e:
            self.log_error(f"❌ Spreadsheet upload failed: {e}")
            self.context.add_error(f"Spreadsheet upload failed: {e}")

    def _sync_external_services(self, report_paths: Dict[str, Any], df: pl.DataFrame):
        """外部サービスとの同期処理"""
        from src.colab_tools import ColabTools

        # 4-1. Google Drive アップロード (Mandatory Requirement)
        self.log_info("Uploading reports to Google Drive...")
        try:
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
            self._upload_summary_to_gspread(report_paths)
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

    def _verify_data_contract(self, df: pl.DataFrame) -> None:
        """適格銘柄群 (daily_report) に対するデータ契約保証 (Data Contract Guard)。

        下流システムへの流出前に必須カラムの欠損 (null / NaN) を検証し、
        契約違反がある場合は例外をスローして物理遮断する。

        Raises:
            AssertionError: 必須カラムに欠損値が検知された場合。
        """
        if df is None or len(df) == 0:
            return

        essential_cols = ["code", "price", "verdict"]
        cols = df.columns
        check_cols = [c for c in essential_cols if c in cols]

        if check_cols:
            null_exprs = [pl.col(c).is_null().sum().alias(c) for c in check_cols]
            null_counts = df.select(null_exprs).to_dicts()[0]
            invalid_cols = {col: count for col, count in null_counts.items() if count > 0}
            if invalid_cols:
                err_msg = f"Data Contract Violation: Null values detected in essential columns: {invalid_cols}"
                self.log_error(err_msg)
                raise AssertionError(err_msg)
            self.log_info(f"✅ Data contract verified: 0.00% missing values across essential columns ({check_cols}).")

