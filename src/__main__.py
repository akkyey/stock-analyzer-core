"""stock-analyzer-core CLI エントリポイント

使用例:
    python3 -m src scan
    python3 -m src --mode scan
"""

import argparse
import sys

from src.orchestration.context import OrchestratorContext
from src.orchestration.scan_handler import ScanHandler


def main() -> int:
    parser = argparse.ArgumentParser(
        description="stock-analyzer-core: クオンツ株式分析パイプライン"
    )
    parser.add_argument(
        "positional_mode",
        nargs="?",
        default=None,
        help="実行モード (例: scan, daily)",
    )
    parser.add_argument(
        "--mode",
        "-m",
        default="scan",
        help="実行モード (既定: scan)",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="対象銘柄数の上限 (テスト・検証用)",
    )

    args = parser.parse_args()
    mode = args.positional_mode or args.mode

    if mode not in ["scan", "daily"]:
        print(f"❌ 不明な実行モードです: {mode} (利用可能: scan, daily)", file=sys.stderr)
        return 1

    context = OrchestratorContext()
    context.limit = args.limit

    handler = ScanHandler()
    handler.execute(context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
