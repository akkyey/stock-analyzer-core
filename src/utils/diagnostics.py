"""調査用サマリ (Run Diagnostics)

利用者の環境でトラブルが起きたとき、サポートに添付できる 1 つのテキストを生成する。
Colab / CLI 共通。取得・集計の失敗で本処理が止まった場合でも出力できるよう、
各セクションは独立して例外を握りつぶし、収集できた範囲だけを書き出す。

含めない情報: API キー、メールアドレス、認証情報、設定ファイル全体、DB の中身 (件数・日付範囲のみ)。
"""

import os
import platform
import re
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DIAGNOSTICS_FILENAME = "diagnostics_summary.txt"

# Colab のシステム時刻は UTC のため、表示は JST に統一する
JST = timezone(timedelta(hours=9), "JST")

# サマリの生成元 (結果欄の表記を決める)
SOURCE_COMPLETED = "step3_completed"
SOURCE_FAILED = "step3_failed"
SOURCE_MANUAL = "manual"

# サマリに載せてよい設定キー (ホワイトリスト。設定全体は出さない)
_CONFIG_WHITELIST = (
    "fetch_profile",
    "is_first_run",
    "strategy_preset",
    "verdict_mode",
    "hard_filters",
    "target_date",
    "debug_mode",
)

_SECRET_PATTERNS = (
    (re.compile(r"(Subscription-Key=)[^&\s]+", re.I), r"\1***"),
    (re.compile(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^\s'\",&]+", re.I), r"\1***"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "***"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***"),
)


def scrub(text: str) -> str:
    """API キー・メールアドレス等の機微情報を伏せる。"""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _now() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def code_version() -> str:
    """リポジトリのコード版 (git describe / 短縮ハッシュ)。取得できなければ "?"。"""
    root = Path(__file__).resolve().parent.parent.parent

    def git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=5
            )
            return out.stdout.strip() or "?"
        except Exception:
            return "?"

    return f"{git('describe', '--tags', '--always')} / {git('rev-parse', '--short', 'HEAD')}"


def stamp_run(context: Any) -> None:
    """Step 3 (パイプライン) 開始時に、実行時点のコード版と開始時刻を記録する。

    後から「困ったときは」セルでサマリを作っても、実際に動いたコード版が分かるようにする。
    """
    cfg = getattr(context, "config", None)
    if isinstance(cfg, dict):
        cfg["run_code_version"] = code_version()
        cfg["run_started_at"] = _now()


def _safe(section: str, fn: Callable[[], str]) -> str:
    """セクション収集を失敗させない (失敗理由だけを書く)。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - 診断は本処理を妨げない
        return f"(収集失敗: {type(e).__name__}: {e})"


def _env_section(extra: dict) -> str:
    def pkg(name: str) -> str:
        try:
            from importlib.metadata import version

            return version(name)
        except Exception:
            return "?"

    is_colab = "google.colab" in sys.modules or bool(os.environ.get("COLAB_RELEASE_TAG"))
    lines = [
        f"- 実行環境: {'Google Colab' if is_colab else 'ローカル / CLI'}",
        f"- Python: {platform.python_version()} ({platform.system()})",
        f"- コード版 (サマリ作成時点): {code_version()}",
        "- ライブラリ: "
        + ", ".join(f"{p}={pkg(p)}" for p in ("yfinance", "polars", "duckdb", "pandas")),
    ]
    for k, v in extra.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _config_section(context: Any) -> str:
    cfg = getattr(context, "config", None) or {}
    if "run_started_at" not in cfg:
        return "(Step 3 の実行記録なし)"
    lines = [
        f"- 開始時刻: {cfg['run_started_at']}",
        f"- 実行時のコード版: {cfg.get('run_code_version', '?')}",
    ]
    lines += [f"- {k}: {cfg[k]}" for k in _CONFIG_WHITELIST if k in cfg]
    return "\n".join(lines)


def _fetch_section(context: Any) -> str:
    cfg = getattr(context, "config", None) or {}
    stats = cfg.get("fetch_stats") or {}
    missing = sorted(set(cfg.get("fetch_missing_codes") or []))
    lines = [f"- {k}: {v}" for k, v in sorted(stats.items())] or ["- (取得統計なし)"]
    lines.append(f"- 取得できなかった銘柄: {len(missing)} 件")
    if missing:
        shown = ", ".join(missing[:200])
        more = f" ... 他 {len(missing) - 200} 件" if len(missing) > 200 else ""
        lines.append(f"  {shown}{more}")
    perf = getattr(context, "perf_stats", None)
    if isinstance(perf, dict):
        lines.append(
            "- 時間内訳(秒): "
            + ", ".join(f"{k}={v:.0f}" for k, v in perf.items() if v and v > 0)
        )
    return "\n".join(lines)


def _db_section(working_dir: Optional[Path], drive_dir: Optional[Path]) -> str:
    lines = []
    if working_dir is not None:
        # 読み取り専用 (PathResolver はディレクトリを作成する副作用があるため使わない)
        db = Path(working_dir) / "cache" / "stock_analyzer.duckdb"
        if not db.exists():
            db = Path(working_dir) / "stock_analyzer.duckdb"
        if db.exists():
            lines.append(f"- 作業層DB: {db.stat().st_size / 1e6:.1f} MB")
            import duckdb

            with duckdb.connect(str(db), read_only=True) as con:
                for tbl in ("stocks", "fundamentals", "daily_metrics"):
                    n = con.execute(f"SELECT count(*) FROM {tbl}").fetchall()[0][0]
                    lines.append(f"  - {tbl}: {n} 行")
                row = con.execute(
                    "SELECT count(distinct entry_date), min(entry_date), max(entry_date) "
                    "FROM daily_metrics"
                ).fetchall()[0]
                lines.append(f"  - daily_metrics: {row[0]} 日分 ({row[1]} 〜 {row[2]})")

                # 財務データの充足状況 (各指標が空でない銘柄数)。再計算の入力 (eps 等) が空だと、
                # 指標は計算できず保存値頼みになるため、環境ごとの違いを判別する手掛かりになる
                fcols = {r[1] for r in con.execute("PRAGMA table_info('fundamentals')").fetchall()}
                wanted = [
                    c for c in (
                        "per", "pbr", "dividend_yield", "market_cap", "roe", "equity_ratio",
                        "eps", "bps", "dps", "shares_outstanding", "sales_growth", "profit_growth",
                    ) if c in fcols
                ]
                if wanted:
                    exprs = ", ".join(f"count({c})" for c in wanted)
                    counts = con.execute(f"SELECT {exprs} FROM fundamentals").fetchall()[0]
                    lines.append(
                        "  - fundamentals の充足 (空でない銘柄数): "
                        + ", ".join(f"{c}={n}" for c, n in zip(wanted, counts, strict=True))
                    )
        else:
            lines.append("- 作業層DB: なし")
    if drive_dir is not None and str(drive_dir).startswith("/content/drive") and not Path(
        "/content/drive/MyDrive"
    ).exists():
        lines.append("- Google Drive: 未マウント (Drive 上のファイルは確認できません)")
    elif drive_dir is not None:
        cache = Path(drive_dir) / "cache"
        for name in ("stock_analyzer.duckdb", "stock_analyzer.duckdb.bak"):
            f = cache / name
            if f.exists():
                st = f.stat()
                lines.append(
                    f"- Drive {name}: {st.st_size / 1e6:.1f} MB, "
                    f"更新 {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}"
                )
            else:
                lines.append(f"- Drive {name}: なし")
    return "\n".join(lines) or "(対象なし)"


def _errors_section(context: Any, error: Optional[BaseException]) -> str:
    lines = []
    errs = getattr(context, "errors", None) or []
    lines.append(f"- 部分失敗フラグ: {bool(getattr(context, 'has_partial_failure', False))}")
    lines.append(f"- 記録されたエラー: {len(errs)} 件")
    lines.extend(f"  - {e}" for e in errs[:30])
    if error is not None:
        lines.append("- 中断した例外:")
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        lines.extend("    " + ln for ln in tb.rstrip().splitlines()[-25:])
    return "\n".join(lines)


def build_run_summary(
    context: Any = None,
    *,
    working_dir: Optional[Path] = None,
    drive_dir: Optional[Path] = None,
    elapsed_sec: Optional[float] = None,
    error: Optional[BaseException] = None,
    extra: Optional[dict] = None,
    source: Optional[str] = None,
) -> str:
    """調査用サマリのテキストを生成する (機微情報は伏せ済み)。

    source: SOURCE_COMPLETED / SOURCE_FAILED / SOURCE_MANUAL。
        未指定時は error の有無から SOURCE_FAILED / SOURCE_MANUAL を選ぶ。
    """
    extra = extra or {}
    if source is None:
        source = SOURCE_FAILED if error is not None else SOURCE_MANUAL
    if source == SOURCE_COMPLETED:
        result = "Step 3 完了"
    elif source == SOURCE_FAILED:
        result = "Step 3 失敗 (中断)"
    else:
        cfg = getattr(context, "config", None) or {}
        result = (
            "不明 (「困ったときは」セルで作成。下記は直近の Step 3 実行記録)"
            if "run_started_at" in cfg
            else "不明 (「困ったときは」セルで作成。Step 3 の実行記録なし)"
        )
    head = [
        "# Stock Analyzer 調査用サマリ",
        f"生成日時: {_now()}",
        f"結果: {result}"
        + (f" / 所要 {elapsed_sec:.0f} 秒" if elapsed_sec is not None else ""),
        "",
        "※ APIキー・メールアドレス・認証情報・DBの中身は含まれません (件数と日付範囲のみ)。",
        "  添付前に内容をご確認ください。",
    ]
    sections = [
        ("実行環境", _safe("env", lambda: _env_section(extra))),
        ("Step 3 実行時の設定 (主要項目のみ)", _safe("cfg", lambda: _config_section(context))),
        ("市場データ取得", _safe("fetch", lambda: _fetch_section(context))),
        ("データベース", _safe("db", lambda: _db_section(working_dir, drive_dir))),
        ("エラー", _safe("err", lambda: _errors_section(context, error))),
    ]
    body = "\n".join(head)
    for title, text in sections:
        body += f"\n\n## {title}\n{text}"
    return scrub(body) + "\n"


def write_run_summary(path: Path, **kwargs: Any) -> Path:
    """サマリを path に書き出す。書き出せなくても本処理は止めない。"""
    text = build_run_summary(**kwargs)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return path


def record_fetch_stat(context: Any, key: str, n: int = 1) -> None:
    """context.config["fetch_stats"] のカウンタを加算する (context 不在・非 dict は無視)。"""
    cfg = getattr(context, "config", None)
    if not isinstance(cfg, dict):
        return
    stats = cfg.setdefault("fetch_stats", {})
    stats[key] = stats.get(key, 0) + n
