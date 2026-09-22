"""
Colab Utility Tools (軽量互換版)

[移行メモ]:
旧来の Colab 専用環境設定、pip/apt 自動インストール、SQLite 同期等の肥大化コード (828行) は、
以下のアーカイブへ完全退避されました:
`archive/legacy_colab/colab_tools_original.py`

本モジュールは、Google Drive へのレポートアップロード機能および後方互換性のみを提供します。
"""

import os
from pathlib import Path
from typing import Any


class ColabTools:
    """Google Drive アップロードおよび Colab 連携用の軽量ユーティリティ。"""

    def __init__(
        self,
        mount_path: str = "/content/drive",
        project_root_drive: str = "/content/drive/MyDrive/StockAnalyzer_Prod",
    ):
        self.mount_path = Path(mount_path)
        self.shared_folder_id = os.getenv("GDRIVE_SHARED_FOLDER_ID")
        self.project_root_drive = Path(project_root_drive)
        self.is_colab = Path("/content").exists() or bool(os.environ.get("COLAB_GPU"))

    @classmethod
    def upload_file_to_drive(
        cls,
        local_path: str,
        drive_folder_name: str = "reports",
        fixed_file_name: str | None = None,
    ) -> str | None:
        """ローカルファイルを Google Drive にアップロードする。"""
        target_path = Path(local_path)
        if not target_path.exists():
            return None

        # 認証情報またはサービスアカウントの存在確認
        creds_path = os.getenv("GDRIVE_CREDENTIALS_PATH")
        if not creds_path or not Path(creds_path).exists():
            # ローカル実行時や認証なし環境ではスキップ
            return None

        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            )
            service = build("drive", "v3", credentials=creds)

            file_metadata = {
                "name": fixed_file_name or target_path.name,
            }
            media = MediaFileUpload(str(target_path), resumable=True)
            uploaded_file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id, webViewLink")
                .execute()
            )
            return uploaded_file.get("webViewLink")
        except Exception:
            return None

    def export_to_sheets(self, *args: Any, **kwargs: Any) -> None:
        """Google Sheets エクスポート互換スタブ"""
        pass
