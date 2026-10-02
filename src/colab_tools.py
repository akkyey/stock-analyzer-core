"""
Colab Utility Tools (軽量互換版)

[移行メモ]:
旧来の Colab 専用環境設定、pip/apt 自動インストール、SQLite 同期等の肥大化コード (828行) は、
退避されました (参照用のアーカイブはリポジトリ内に残っていません。履歴は git log を参照)。

本モジュールは、Google Drive へのレポートアップロード機能 (CLI / サーバー向け) および
後方互換性のみを提供します。Colab の Drive 同期は src/utils/colab_sync.py が担当します。
"""

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _find_or_create_folder(service: Any, name: str, parent_id: str | None) -> str:
        """親フォルダ配下に name のフォルダを探し、無ければ作成して ID を返す。"""
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
            "and trashed = false"
        )
        if parent_id:
            q += f" and '{parent_id}' in parents"
        found = (
            service.files()
            .list(
                q=q,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )
        if found:
            return str(found[0]["id"])
        meta: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            meta["parents"] = [parent_id]
        created = (
            service.files()
            .create(body=meta, fields="id", supportsAllDrives=True)
            .execute()
        )
        return str(created["id"])

    @classmethod
    def upload_file_to_drive(
        cls,
        local_path: str,
        drive_folder_name: str | None = None,
        fixed_file_name: str | None = None,
        parent_folder_id: str | None = None,
    ) -> str | None:
        """ローカルファイルを Google Drive (サービスアカウント経由) にアップロードする。

        CLI / サーバー実行向けの経路 (Colab は ColabSyncManager が Drive マウント経由で同期する)。

        Args:
            drive_folder_name: アップロード先サブフォルダ名 (親フォルダ配下に無ければ作成)。
            parent_folder_id: 親フォルダ ID。未指定なら環境変数 GDRIVE_SHARED_FOLDER_ID。
                どちらも無い場合はサービスアカウント自身のドライブ直下に作成されるため、
                利用者から見えない点に注意 (警告ログを出す)。

        Returns:
            アップロードしたファイルの URL。認証情報なし等でスキップした場合や失敗時は None。
        """
        target_path = Path(local_path)
        if not target_path.exists():
            return None

        # 認証情報の存在確認 (ローカル実行時や認証なし環境ではスキップ)
        creds_path = os.getenv("GDRIVE_CREDENTIALS_PATH")
        if not creds_path or not Path(creds_path).exists():
            return None

        parent_id = parent_folder_id or os.getenv("GDRIVE_SHARED_FOLDER_ID")
        if not parent_id:
            logger.warning(
                "GDRIVE_SHARED_FOLDER_ID が未設定のため、サービスアカウント自身のドライブに保存されます "
                "(利用者から見えない可能性があります)。"
            )

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            creds = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/drive.file"],
            )
            service = build("drive", "v3", credentials=creds)

            if drive_folder_name:
                parent_id = cls._find_or_create_folder(
                    service, drive_folder_name, parent_id
                )

            file_metadata: dict[str, Any] = {
                "name": fixed_file_name or target_path.name,
            }
            if parent_id:
                file_metadata["parents"] = [parent_id]
            media = MediaFileUpload(str(target_path), resumable=True)
            uploaded_file = (
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            val = uploaded_file.get("webViewLink")
            return str(val) if val is not None else None
        except Exception as e:
            logger.warning(f"Google Drive へのアップロードに失敗しました: {e}")
            return None

    def export_to_sheets(self, *args: Any, **kwargs: Any) -> str | None:
        """Google Sheets エクスポート互換スタブ"""
        return None
