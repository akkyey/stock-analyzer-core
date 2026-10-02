"""Google Colab 二層ストレージ (Stage-and-Sync) 管理モジュール

作業層 (ローカル超高速SSD) と 永続層 (Google Drive) のデータ同期を司る。
- Pull Phase: 3段構えの自動復元検証 (メインDB -> .bak -> 新規初期化)
- Push Phase: WALフラッシュ、.bak世代退避、.tmpアトミック置換、flush_and_unmount
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


class ColabSyncManager:
    """Colab Stage-and-Sync ライフサイクルマネージャー"""

    DB_FILENAME = "stock_analyzer.duckdb"
    DAILY_REPORT_FILENAME = "daily_report.csv"
    UNPROCESSED_FILENAME = "uncalculable_stocks.csv"

    DEFAULT_DRIVE_FOLDER = "StockAnalyzer"
    DEFAULT_DRIVE_BASE = Path("/content/drive/MyDrive")

    @classmethod
    def resolve_drive_dir(
        cls,
        drive_path_or_name: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Google Drive 上の作業・保存先ディレクトリを安全かつ柔軟に解決する。

        解決ロジック:
        1. 引数 drive_path_or_name が指定されている場合:
           - 絶対パス（/ で始まる）: そのまま Path オブジェクトとして採用（共有ドライブ等も対応）
           - 相対パス（フォルダ名）: /content/drive/MyDrive/<名前> として結合
        2. 環境変数 STOCK_ANALYZER_DRIVE_DIR が設定されている場合:
           - そのパスを採用
        3. デフォルト:
           - /content/drive/MyDrive/StockAnalyzer
        """
        raw = drive_path_or_name or os.getenv("STOCK_ANALYZER_DRIVE_DIR")
        if not raw:
            return cls.DEFAULT_DRIVE_BASE / cls.DEFAULT_DRIVE_FOLDER

        p = Path(raw)
        if p.is_absolute():
            return p

        # 相対パスの場合は MyDrive 配下として解決
        return cls.DEFAULT_DRIVE_BASE / p

    @classmethod
    def is_duckdb_healthy(cls, db_path: Path) -> bool:
        """DuckDB ファイルが正常にオープンでき、テーブルが読み書き可能か検証する"""
        if not db_path.exists() or db_path.stat().st_size == 0:
            return False

        try:
            import duckdb

            # 読み取り専用で接続テスト (コンテキストマネージャで確実に切断)
            with duckdb.connect(str(db_path), read_only=True) as conn:
                # 1. 基本的なカタログ整合性確認
                conn.execute("SELECT count(*) FROM information_schema.tables").fetchall()
                # 2. 主要テーブルのデータブロック読み込み検証
                tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
                for tbl in ["stocks", "fundamentals"]:
                    if tbl in tables:
                        conn.execute(f"SELECT count(*) FROM {tbl}").fetchall()
            return True
        except Exception as e:
            logger.warning(f"⚠️ DuckDB 健全性検証エラー ({db_path}): {e}")
            return False

    @classmethod
    def reset_cache(
        cls,
        drive_dir: Path,
        working_dir: Path,
        db_filename: Optional[str] = None,
    ) -> None:
        """Google Drive および作業層 SSD の DB キャッシュを安全に完全クリアする。"""
        filename = db_filename or cls.DB_FILENAME
        targets = [
            drive_dir / "cache" / filename,
            drive_dir / "cache" / f"{filename}.bak",
            drive_dir / filename,
            drive_dir / f"{filename}.bak",
            working_dir / "cache" / filename,
            working_dir / filename,
            working_dir / "cache" / f"{filename}.bak",
            working_dir / "output",
        ]
        for t in targets:
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
            elif t.exists():
                try:
                    t.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"⚠️ キャッシュ削除失敗 ({t}): {e}")

    @classmethod
    def verify_database_integrity(
        cls,
        db_path: Path,
        min_stocks: int = 1000,
        min_history_dates: int = 5,
    ) -> tuple[bool, str]:
        """実行前 DB の整合性を厳格に検証する。

        Returns:
            tuple[bool, str]: (is_consistent, message)
        """
        if not db_path.exists() or db_path.stat().st_size == 0:
            return True, "新規初期化（キャッシュなし）"

        try:
            import duckdb

            with duckdb.connect(str(db_path), read_only=True) as conn:
                # 1. カタログ検証
                conn.execute("SELECT count(*) FROM information_schema.tables").fetchall()
                tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}

                # 2. 必須テーブル存在確認
                required_tables = {"stocks", "fundamentals", "daily_metrics"}
                missing = required_tables - tables
                if missing:
                    return False, f"必須テーブル {missing} が欠落しています"

                # 3. データの最低件数チェック
                stocks_count = conn.execute("SELECT count(*) FROM stocks").fetchone()[0]
                funda_count = conn.execute("SELECT count(*) FROM fundamentals").fetchone()[0]

                if stocks_count < min_stocks:
                    return False, f"銘柄マスタ件数が異常に少なくなっています ({stocks_count} 件 / 期待値: {min_stocks}件以上)"

                if funda_count < min_stocks:
                    return False, f"財務データ件数が異常に少なくなっています ({funda_count} 件 / 期待値: {min_stocks}件以上)"

                # 4. 株価履歴データの蓄積日数・件数チェック (差分更新の前提条件)
                metrics_count = conn.execute("SELECT count(*) FROM daily_metrics").fetchone()[0]
                dates_count = conn.execute("SELECT count(distinct entry_date) FROM daily_metrics").fetchone()[0]

                if dates_count < min_history_dates:
                    return False, (
                        f"株価時系列履歴 (daily_metrics) が不足しています "
                        f"(現在: {dates_count}日分・{metrics_count}行 / 差分モード必要条件: 最低{min_history_dates}日分以上)"
                    )

            return True, f"正常 (stocks: {stocks_count}社, fundamentals: {funda_count}件, 株価履歴: {dates_count}日分・{metrics_count}行)"
        except Exception as e:
            return False, f"DuckDBファイル破損または読み込みエラー: {e}"

    @classmethod
    def is_first_run(
        cls, drive_dir: Path, db_filename: Optional[str] = None
    ) -> bool:
        """Google Drive 上に健全な既存 DB が存在するか確認し、初回実行（キャッシュなし）か否かを判定する"""
        filename = db_filename or cls.DB_FILENAME
        drive_db = drive_dir / "cache" / filename
        if not drive_db.exists() and (drive_dir / filename).exists():
            drive_db = drive_dir / filename
        return not cls.is_duckdb_healthy(drive_db)

    @classmethod
    def pull_database(
        cls,
        drive_dir: Path,
        working_dir: Path,
        db_filename: Optional[str] = None,
    ) -> Path:
        """【Pull Phase】Google Drive から作業層 SSD へ 3段構えの復元検証を行い DB を配置する。

        復元順序:
        1. 第1段: Drive 上のメイン DB をコピーし、健全性を検証
        2. 第2段: メイン破損時、Drive 上の .bak をコピーし、健全性を検証
        3. 第3段: 両方破損または未存在時、新規初期化フォールバック

        Returns:
            Path: 作業層上の準備完了した DB パス
        """
        from src.utils.path_resolver import PathResolver

        filename = db_filename or cls.DB_FILENAME
        drive_db = drive_dir / "cache" / filename
        drive_bak = drive_dir / "cache" / f"{filename}.bak"

        # Drive 直下のフォールバック確認
        if not drive_db.exists() and (drive_dir / filename).exists():
            drive_db = drive_dir / filename
        if not drive_bak.exists() and (drive_dir / f"{filename}.bak").exists():
            drive_bak = drive_dir / f"{filename}.bak"

        working_db = PathResolver.get_duckdb_path(working_dir)
        working_cache = working_dir / "cache"
        working_cache.mkdir(parents=True, exist_ok=True)
        working_cache_db = working_cache / filename

        chosen = False
        # 第1段: メインDBの検証と採用
        if drive_db.exists():
            try:
                shutil.copy2(drive_db, working_db)
                if cls.is_duckdb_healthy(working_db):
                    logger.info(
                        f"✅ [Pull: 第1段] Google Drive から健全なメイン DB をロードしました: {working_db}"
                    )
                    chosen = True
                else:
                    logger.warning(
                        "⚠️ [Pull] Drive 上のメイン DB が破損しているか不正です。バックアップからの復旧を試みます..."
                    )
                    if working_db.exists():
                        working_db.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(
                    f"⚠️ [Pull] Drive メイン DB のコピー失敗 ({e})。バックアップ復旧を試みます..."
                )
                if working_db.exists():
                    working_db.unlink(missing_ok=True)

        # 第2段: バックアップ (.bak) の検証と採用
        if not chosen and drive_bak.exists():
            try:
                shutil.copy2(drive_bak, working_db)
                if cls.is_duckdb_healthy(working_db):
                    logger.warning(
                        f"⚠️ [Pull: 第2段] メインDB破損を検知したため、健全なバックアップ (.bak) から復元しました: {working_db}"
                    )
                    chosen = True
                else:
                    logger.warning("⚠️ [Pull] バックアップ DB (.bak) も破損しています。")
                    if working_db.exists():
                        working_db.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(
                    f"⚠️ [Pull] Drive バックアップ DB のコピー失敗 ({e})。"
                )
                if working_db.exists():
                    working_db.unlink(missing_ok=True)

        # 第3段: 新規初期化フォールバック
        if not chosen:
            logger.warning(
                f"ℹ️ [Pull: 第3段] 既存DB未存在または全破損のため、新規に空の DB を初期化します: {working_db}"
            )
            if working_db.exists():
                working_db.unlink(missing_ok=True)
            if working_cache_db.exists():
                working_cache_db.unlink(missing_ok=True)
            return working_db

        # working_db と working_cache_db の両方を同期（二重参照の完全安全化）
        if working_db != working_cache_db and working_db.exists():
            try:
                shutil.copy2(working_db, working_cache_db)
            except Exception as e:
                logger.debug(f"working_cache_db sync notice: {e}")

        # 実行前 DB 整合性検証 (破損・不整合時のフェイルセーフ)
        if chosen and working_db.exists():
            is_valid, reason = cls.verify_database_integrity(working_db)
            if not is_valid:
                print("\n" + "❌" * 35)
                print("⚠️ 【データベース不整合を検知しました】")
                print(f"   詳細: {reason}")
                print("   破損または不整合が生じた状態のまま実行すると、誤った分析やデータ破壊につながる恐れがあります。")
                print("   安全のために処理を中断しました。")
                print("\n👉 【対処方法】")
                print("   Google Drive 上の古いキャッシュをリセットしてください。")
                print("   Colab のセルで以下を実行してキャッシュをリセットし、再実行してください:")
                print("   >>> from src.utils.colab_sync import ColabSyncManager")
                print("   >>> ColabSyncManager.reset_cache(DRIVE_DIR, WORKING_DIR)")
                print("❌" * 35 + "\n")
                raise RuntimeError(f"データベース不整合を検知したため安全に中断しました: {reason}")

        return working_db

    @classmethod
    def push_artifacts(
        cls,
        working_dir: Path,
        drive_dir: Path,
        db_filename: Optional[str] = None,
        flush_unmount: bool = True,
    ) -> bool:
        """【Push Phase】作業層 SSD の最新 DB および CSV レポートを Google Drive へ安全にアトミック同期する。

        同期手順:
        1. Drive 側で既存の健全 DB を .bak へコピー退避 (shutil.copy2)
        2. 作業層の最新 DB を Drive の .tmp へ書き出し、os.replace でアトミック置換
        3. 出力 CSV (daily_report.csv, uncalculable_stocks.csv) を同期
        4. drive.flush_and_unmount() によるクラウド同期保証

        Returns:
            bool: 同期成功時 True
        """
        from src.utils.path_resolver import PathResolver

        filename = db_filename or cls.DB_FILENAME
        working_db = PathResolver.get_duckdb_path(working_dir)
        if not working_db.exists() or working_db.stat().st_size == 0:
            working_db = working_dir / "cache" / filename

        drive_cache = drive_dir / "cache"
        drive_output = drive_dir / "output"
        drive_db = drive_cache / filename
        drive_bak = drive_cache / f"{filename}.bak"
        drive_tmp = drive_cache / f"{filename}.tmp"

        try:
            drive_cache.mkdir(parents=True, exist_ok=True)
            drive_output.mkdir(parents=True, exist_ok=True)

            # 1. DB の同期
            if working_db.exists() and working_db.stat().st_size > 0:
                # WAL を確実にメイン DB ファイルへフラッシュ
                try:
                    import duckdb

                    with duckdb.connect(str(working_db)) as con:
                        con.execute("CHECKPOINT")
                except Exception as e:
                    logger.debug(f"DuckDB CHECKPOINT notice: {e}")

                # 作業層 DB の健全性を最終確認
                if not cls.is_duckdb_healthy(working_db):
                    logger.error(
                        f"❌ [Push] 作業層 DB が破損しているため、Drive への同期を中断しました: {working_db}"
                    )
                    return False

                # 既存 Drive DB を .bak へ退避
                if drive_db.exists():
                    try:
                        shutil.copy2(drive_db, drive_bak)
                        logger.info(
                            "🛡️ [Push] Drive 上の既存 DB を 1 世代バックアップ (.bak) に退避しました。"
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ [Push] .bak 退避に失敗しましたが処理を続行します: {e}")

                # .tmp 経由のアトミック置換
                shutil.copy2(working_db, drive_tmp)
                os.replace(drive_tmp, drive_db)
                logger.info(
                    f"✅ [Push] 最新 DB をアトミックに Google Drive へ同期しました: {drive_db}"
                )

            # 2. CSV レポートの同期 (ファイルごとに安全処理)
            working_output = working_dir / "output"
            for csv_name in [cls.DAILY_REPORT_FILENAME, cls.UNPROCESSED_FILENAME]:
                w_csv = working_output / csv_name
                if w_csv.exists():
                    d_csv = drive_output / csv_name
                    d_tmp = drive_output / f"{csv_name}.tmp"
                    try:
                        shutil.copy2(w_csv, d_tmp)
                        os.replace(d_tmp, d_csv)
                        logger.info(f"✅ [Push] レポート CSV を同期しました: {d_csv}")
                    except Exception as csv_err:
                        logger.warning(f"⚠️ [Push] {csv_name} の同期中に警告: {csv_err}")
                        if d_tmp.exists():
                            d_tmp.unlink(missing_ok=True)

            # 3. Colab 環境でのフラッシュとアンマウント
            if flush_unmount:
                cls.flush_and_unmount_if_colab()

            return True

        except Exception as e:
            logger.error(
                f"❌ [Push] Google Drive への同期中にエラーが発生しました: {e}",
                exc_info=True,
            )
            # 異常系: 中途半端に残った .tmp ファイルを確実に掃除
            for tmp_file in [drive_tmp] + [
                drive_output / f"{csv_name}.tmp"
                for csv_name in [cls.DAILY_REPORT_FILENAME, cls.UNPROCESSED_FILENAME]
            ]:
                try:
                    if tmp_file.exists():
                        tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass
            return False

    @classmethod
    def flush_and_unmount_if_colab(cls) -> None:
        """Colab 環境下であれば Google Drive の非同期フラッシュとアンマウントを実行する"""
        try:
            from google.colab import drive

            logger.info(
                "⏳ [Push] Google Drive のクラウド非同期同期をフラッシュ中 (flush_and_unmount)..."
            )
            drive.flush_and_unmount()
            logger.info(
                "✨ [Push] Google Drive への書き込みがクラウド側で安全に完了しました。"
            )
        except ImportError:
            # ローカル環境等の場合は何もしない
            pass
        except Exception as e:
            logger.warning(f"⚠️ drive.flush_and_unmount() 警告: {e}")
