"""モードハンドラの基底クラス

Strategyパターンを適用し、各実行モード（Daily/Weekly/Monthly）の
具象ハンドラで実装する抽象インターフェースを定義する。
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestration.context import OrchestratorContext


class ModeHandler(ABC):
    """実行モードのハンドラ基底クラス。

    各モードの処理ロジックを分離し、テスタビリティと
    拡張性を向上させるための抽象基底クラス。
    """

    @abstractmethod
    def execute(self, context: "OrchestratorContext") -> None:
        """モード固有の処理を実行する。

        Args:
            context (OrchestratorContext): オーケストレーションコンテキスト。
        """
        pass

    @abstractmethod
    def get_mode_name(self) -> str:
        """モード名を返す。

        Returns:
            str: モード名（'daily', 'weekly', 'monthly' 等）。
        """
        pass


if __name__ == "__main__":
    from src.__main__ import main

    main()
