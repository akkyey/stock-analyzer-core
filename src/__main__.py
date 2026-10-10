"""stock-analyzer-core CLI エントリポイント

使用例:
    python3 -m src scan
    python3 -m src --mode scan
    python3 -m src scan --refetch-edinet-year   # メンテナンス用: EDINET を過去 1 年分取り直す (約 2 時間)
"""

import argparse
import sys

from src.orchestration.context import OrchestratorContext
from src.orchestration.scan_handler import ScanHandler

REFETCH_SCAN_DAYS = 365


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
    parser.add_argument(
        "--refetch-edinet-year",
        action="store_true",
        help="メンテナンス用: この実行だけ、EDINET の開示を過去 365 日分取り直す "
        "(同梱の財務データが古くなってから初めて使うときなど。設定ファイルは変更しない)",
    )

    args = parser.parse_args()
    mode = args.positional_mode or args.mode

    if mode not in ["scan", "daily"]:
        print(
            f"❌ 不明な実行モードです: {mode} (利用可能: scan, daily)", file=sys.stderr
        )
        return 1

    context = OrchestratorContext()
    context.limit = args.limit
    if args.refetch_edinet_year:
        context.config.setdefault("fetcher", {})["edinet_scan_days"] = REFETCH_SCAN_DAYS

    handler = ScanHandler()
    handler.execute(context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
