"""Scan モードハンドラ (Modernized Tri-Phase)

OrchestrationPipeline を呼び出し、三段階（取得・評価・統合）の
データ駆動型パイプラインを実行する。
"""

from typing import TYPE_CHECKING

from src.orchestration.mode_handler import ModeHandler
from src.orchestration.pipeline import OrchestrationPipeline

if TYPE_CHECKING:
    from src.orchestration.context import OrchestratorContext


class ScanHandler(ModeHandler):
    """Scan定型ルーチンの新ハンドラ。
    全ての実行ロジックを OrchestrationPipeline へ移譲する。
    """

    def get_mode_name(self) -> str:
        return "daily"

    def execute(self, context: "OrchestratorContext") -> None:
        """三段階パイプラインを実行する。"""
        context.logger.info(
            "🌕 Daily 統合スキャンルーチンを開始します (Tri-Phase Modernized)..."
        )

        # パイプラインの初期化と実行
        pipeline = OrchestrationPipeline(context)

        try:
            # 取得 -> 評価 -> 統合 の順で実行
            results = pipeline.run()

            if results is not None:
                context.logger.info(
                    f"✅ パイプライン実行完了。分析銘柄数: {len(results)}"
                )
            else:
                context.logger.warning("⚠️ パイプラインが中途終了、または結果が空です。")

        except Exception as e:
            context.logger.error(f"❌ ScanHandler 実行失敗: {e}")
            raise e

        # セッションサマリの出力
        context.print_session_summary()
        context.logger.info("✅ Daily ルーチンが正常に完了しました。")
