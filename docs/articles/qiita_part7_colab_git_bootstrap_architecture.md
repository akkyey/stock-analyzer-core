---
title: "Colabで再実行時の「fatal: destination path already exists」を防ぐ：GitHubリポジトリの安全なクローン・実行セットアップ設計"
tags:
  - Python
  - GoogleColaboratory
  - Git
  - GitHub
private: false
---

## TL;DR: 結論だけ知りたい方へ（今すぐエラーを防ぐ最小コード）

セルの再実行で `fatal: destination path 'my-tool' already exists and is not an empty directory.` が出て困っている場合は、クローン直前に **カレントディレクトリを `/content` に戻して既存フォルダを削除する** 以下の数行をセルの先頭に追加してください。

```python
import os, shutil

# 1. カレントディレクトリを必ず /content に戻す（迷子防止）
os.chdir("/content")

# 2. 既にフォルダが存在していれば削除する（再実行時の衝突防止）
TARGET_DIR = "/content/my-tool"
if os.path.exists(TARGET_DIR):
    shutil.rmtree(TARGET_DIR, ignore_errors=True)

# 3. クローン実行
!git clone https://github.com/my-org/my-tool.git {TARGET_DIR}
```

> **これだけでは不十分なケースとは？**  
> 個人利用の単発スクリプトであれば上記で足りますが、**「モジュール更新がメモリに残って反映されない（`sys.modules` キャッシュ問題）」「配布相手の環境でタグ不在や通信切断で止まる」「プライベートリポジトリの認証トークンが残る」** といった実践的な課題を解決するには、追加の考慮が必要です。本稿ではこれらを堅牢に解決する設計と完全なコードを解説します。

:::note info
**【Colab実運用基盤シリーズ】**
- **第1弾（データ永続化編）**: [Google ColabでGoogle Drive上のSQLite / DuckDBが遅い・ロック破損する問題と、ローカルディスクを使ったデータステージング設計](qiita_part6_colab_ephemeral_storage_architecture.md)
<!-- ※ 第1弾公開後に実際のQiita URLへ差し替えてください -->
- **第2弾（コード配布編・本作）**: Colabで再実行時の「fatal: destination path already exists」を防ぐ：GitHubリポジトリの安全なクローン・実行セットアップ設計
:::

---

## はじめに

Google Colaboratory（以下、Colab）は、ブラウザだけで Python コードを実行できるため、オープンソースツールや分析パイプライン（前編で触れた株価スクリーナーなど）をチームメンバーや一般ユーザーに配布する手段として広く使われています。

その際、最も一般的な配布方法は「ノートブック内に `!git clone https://github.com/...` を記述し、リポジトリのコードをそのまま実行させる」という構成です。しかし、この単純な `git clone` をそのまま配ると、セルの再実行時やリリースのタイミングでエラーが発生することがあり、パイプラインが停止します。

本稿では、Colab 上で GitHub リポジトリのコードと依存ライブラリを確実に立ち上げるための環境セットアップ処理をまとめます。

---

## 第1章：よくある書き方と、見落としていたこと

GitHub で管理されているコードを Colab 上で利用者に実行させる際、多くのチュートリアルでは以下のようなシンプルなセルが提示されます。

```python:naive_bootstrap.py
# 初期の典型的なセットアップセル
!git clone -b v1.0.0 https://github.com/my-org/my-tool.git
%cd my-tool
!pip install -r requirements.txt

from src.main import run_pipeline
run_pipeline()
```

この書き方には、次のような都合のよい想定（思い込み）がありました。

- **「セルを上から順に1度だけ実行する」という想定**: セルの再実行や、途中でエラーになってやり直す操作を考えていない
- **「指定したタグは必ず存在する」という想定**: リモートにそのタグが確実にプッシュされていると思い込んでいる（push 漏れやタイポを考慮していない）
- **「環境は最初から綺麗である」という想定**: `%cd` によるカレントディレクトリの移動やインストール状態が、再実行時にも都合よく保たれると思い込んでいる

---

## 第2章：本番運用で起きた3つのトラブル

配布を想定した動作検証や、テスト配布を行った際、以下のトラブルに遭遇しました。

### 1. セル再実行時のディレクトリ衝突エラー
エラー発生やパラメータ変更で利用者がセットアップセルをもう一度実行した際、Git が衝突エラーを起こして停止しました。
```text:git_conflict_error.log
fatal: destination path 'my-tool' already exists and is not an empty directory.
```
利用者は手動でファイルを消す方法がわからず、ランタイムの全初期化を強いられます。

### 2. リリース直後のタグ不一致によるエラー中断
新バージョン（例: `v1.3.1`）のリリース告知直後に、利用者がクローンを実行すると以下のエラーで停止しました。
```text:git_tag_not_found.log
fatal: Remote branch v1.3.1 not found in upstream origin
```
この原因の多くは、開発者側で `git push` 時にタグの送信を忘れていた（`git push --tags` の実行漏れ）か、ノートブック側のパラメータに入力したタグ名のタイポでした。このような人為的ミスや指定の不整合により、パイプライン全体が中断し、手動対応が必要になりました。

### 3. 作業ディレクトリ消失によるモジュール見失い
セル内で `%cd my-tool` を実行した状態で、前述のエラー復旧や再実行のために外側から `shutil.rmtree("my-tool")` が走ると、Python プロセスのカレントディレクトリが存在しない無効なパスを指す状態になります。その結果、以降の相対パス操作やファイル読み込みで `FileNotFoundError` が発生しました。また、`sys.modules` に過去の古いモジュールがキャッシュされ、再取得したコードがメモリ上に反映されない不整合も起きました。

---

## 第3章：なぜ単純な git clone では失敗するのか

起きていた問題の原因を掘り下げると、Colab という対話的実行環境の特性に対する考慮が不足していました。

### 1. セルの再実行（やり直し）を考慮していない
Colab は利用者が試行錯誤しながらセルを何度も再実行する環境です。クローン先にフォルダが既に存在するかどうかを気にせず、常に同じ名前で `git clone` を実行するコードは、やり直し（再実行）に耐えられません。

### 2. タグの直書きによるエラー停止
特定のタグ（`--branch v1.3.1`）をコード内に直書きしていると、開発者のタグ push 忘れや、利用者のタイポが1箇所あるだけでスクリプト全体が停止してしまいます。通信エラーなのか、タグが存在しないのかを判別できず、代替ブランチへの切り替えもできない構成だったため、すぐに処理が止まってしまっていました。

### 3. カレントディレクトリやモジュールキャッシュがプロセス内に残り続ける問題

Colab ノートブックのカーネルは、単一の持続的な Python プロセス上で対話的に実行されます。シェルコマンドの `%cd` や `import` による状態変更はプロセス全体に残り続けるため、以下のトラブルを引き起こします。

1. **滞在中のフォルダを削除してしまう（カレントディレクトリの喪失）**:
   セル内で `%cd my-tool` を実行した後に、再クローン目的で外側から `shutil.rmtree("my-tool")` を実行すると、Python プロセス自身がいまいる場所（カレントディレクトリ）がディスク上から消えてしまいます。この状態になると、以降の相対パス指定やファイルの読み書きで `FileNotFoundError` が発生します。
2. **`sys.modules` によるモジュールキャッシュの残存**:
   Python のインポート機構（`importlib`）は、一度読み込んだモジュールをプロセス空間の辞書 `sys.modules` にキャッシュします。ファイルシステム上の `.py` ファイルを新バージョンでクローンし直しても、Python はディスクを再走査せずメモリ上の古いモジュールを返し続けます。その結果、セルの再実行を行っても最新のバグ修正が反映されず、新設された関数が見つからない `AttributeError` や古いロジックによる不整合が発生します。

---

## 第4章：どう設計し直したか（再実行に強い環境セットアップ）

これらの問題を踏まえ、利用者がセルの再実行を行っても安定して起動するセットアップ処理を設計し直しました。

### 設計の基本方針

- **作業ディレクトリのルート固定（`/content` に戻す）**: 操作の直前に必ずカレントディレクトリを `/content` に戻す。既存の展開先ディレクトリは事前にクリーンアップし、常にクリーンな状態からクローンを開始する
- **最新コミットだけの取得（`git clone --depth 1`）**: 過去のコミット履歴をダウンロードせず、指定した最新のスナップショットのみを取得して通信量と所要時間を抑える
- **タグの事前確認と main ブランチへの切り替え（代替取得）**: `git ls-remote` でタグの存在を確認し、見つからない場合はフラグ（許可時のみ）に応じて `main` ブランチに切り替えて取得を継続する
- **動的インポートとモジュールキャッシュのリセット**: `sys.modules` 内の自作パッケージを明示的にクリアし、再実行時でも新しくクローンされたコードをメモリに再ロードする

```mermaid
flowchart TD
    Start["Step 1 実行開始"] --> CD["os.chdir('/content')<br/>(カレントを /content に戻す)"]
    CD --> CheckDir{"TARGET_DIR が<br/>既に存在するか？"}
    CheckDir -- "Yes" --> Clean["shutil.rmtree()<br/>(既存ディレクトリを事前消去)"]
    CheckDir -- "No" --> Verify
    Clean --> Verify["check_tag() でリモート確認"]

    Verify -- "通信エラー (error)" --> Retry["指数バックオフで再試行 (最大3回)"]
    Retry --> Verify
    Verify -- "再試行上限超過" --> ErrorExit["RuntimeError で明示的中断<br/>(GitHub接続障害案内)"]

    Verify -- "タグ存在 (found)" --> CloneTag["Attempt: git clone --depth 1<br/>--branch TARGET_TAG (指定タグ)"]
    Verify -- "タグ不在 (not_found)" --> CheckAllow{"ALLOW_MAIN_FALLBACK<br/>が有効か？"}
    CheckAllow -- "No (無効)" --> TagExit["RuntimeError で中断<br/>(タグ名確認案内)"]
    CheckAllow -- "Yes (有効)" --> CloneMain["警告出力 ＆ ブランチを 'main' に切替<br/>git clone --branch main"]

    CloneTag --> Pip["sys.executable -m pip install -q -r requirements.txt"]
    CloneMain --> Pip

    Pip --> Reload["sys.modules から自作パッケージを破棄<br/>(最新モジュールの強制再読込)"]
    Reload --> Ready["環境セットアップ完了 (次Stepへ)"]

    style Start fill:#f9f9f9,stroke:#333,stroke-width:1px,color:#333
    style Pip fill:#f0fff0,stroke:#2a2,stroke-width:1px,color:#333
    style Ready fill:#d0ffd0,stroke:#2a2,stroke-width:2px,color:#333
    style ErrorExit fill:#ffe0e0,stroke:#d33,stroke-width:2px,color:#333
    style TagExit fill:#ffe0e0,stroke:#d33,stroke-width:2px,color:#333
```

---

## 第5章：実装のポイントとコード

ここまでに整理した設計を、Google Colab 上でそのまま動作確認できるようにまとめた動作確認用のセットアップコードです。

ノートブックの1つのセルに貼り付け、上部のパラメータ（`REPO_URL` やタグ名）をご自身のリポジトリに合わせて設定して実行してください。クローン、依存関係のインストール、モジュールキャッシュの破棄、パス解決までが一気通貫で完了します。

```python:step1_setup.py
# @title 【Step 1】環境セットアップ
# @markdown 配布したいリポジトリの情報を設定してください
REPO_URL = "https://github.com/my-org/my-tool.git"  # @param {type:"string"}
TARGET_TAG = "v1.3.1"  # @param {type:"string"}
ALLOW_MAIN_FALLBACK = False  # @param {type:"boolean"}
# ↑ タグ不在時に最新開発版 (main) での実行を許容する場合は True にチェック
TARGET_DIR = "/content/my-tool"
PACKAGE_NAME = "src"  # 読み込むパッケージフォルダ名（例: src または my_tool）

import importlib
import os
import re
import shutil
import subprocess
import sys
import time

# 1. 作業ディレクトリを必ず /content に戻す
os.chdir("/content")

# 2. 既存ディレクトリが存在する場合は削除 (再実行時の衝突防止)
if os.path.exists(TARGET_DIR):
    shutil.rmtree(TARGET_DIR, ignore_errors=True)

# 3. リモートタグの事前確認（3状態判定と指数バックオフ）
def check_tag(repo_url: str, tag: str, retries: int = 3) -> str:
    """'found' / 'not_found' / 'error' を返す。通信エラーは指数バックオフで再試行"""
    for i in range(retries):
        try:
            res = subprocess.run(
                ["git", "ls-remote", "--exit-code", repo_url, f"refs/tags/{tag}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                return "found"
            if res.returncode == 2:  # 通信成功・該当refなし
                return "not_found"
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass
        time.sleep(2 ** i)
    return "error"

status = check_tag(REPO_URL, TARGET_TAG)
if status == "error":
    raise RuntimeError(
        "❌ リポジトリが見つからないか、GitHub に接続できません。\n"
        "上部の 'REPO_URL' を実際のリポジトリURLに書き換えて再実行してください。"
    )
if status == "not_found":
    if not ALLOW_MAIN_FALLBACK:
        raise RuntimeError(f"❌ タグ '{TARGET_TAG}' が見つかりません。タグ名を確認してください。")
    print(f"⚠️ タグ '{TARGET_TAG}' がないため main で続行します（未検証コードの可能性あり）。")
    target_to_clone = "main"
else:
    target_to_clone = TARGET_TAG

# 4. 最新コミットだけの取得 (--depth 1)
clone_success = False
for attempt in range(1, 3):
    try:
        print(f"📥 リポジトリを取得中 (attempt {attempt}/2, target: {target_to_clone})...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", target_to_clone, REPO_URL, TARGET_DIR],
            check=True,
            capture_output=True,
            text=True,
        )
        clone_success = True
        break
    except subprocess.CalledProcessError as e:
        # トークン等を含むコマンド全文ではなく、標準エラーのみを安全に出力（URLトークンは伏字化）
        raw_msg = e.stderr.strip() if e.stderr else "クローン処理に失敗しました"
        err_msg = re.sub(r"https://[^@]+@", "https://***@", raw_msg)
        print(f"⚠️ Git クローン警告 (attempt {attempt}): {err_msg}")
        if os.path.exists(TARGET_DIR):
            shutil.rmtree(TARGET_DIR, ignore_errors=True)
        time.sleep(2)

if not clone_success:
    raise RuntimeError("❌ リポジトリ取得に失敗しました。ネットワーク接続または GitHub の稼働状況を確認してください。")

# 5. 依存ライブラリの導入 (カレントディレクトリを移動せず、sys.executable と絶対パスで指定)
req_path = os.path.join(TARGET_DIR, "requirements.txt")
if os.path.exists(req_path):
    try:
        print("📦 依存ライブラリをインストール中...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req_path],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else "pip インストールに失敗しました"
        raise RuntimeError(f"❌ 依存ライブラリのインストールに失敗しました: {err_msg}") from None

# 6. 自作モジュールのキャッシュを破棄 (再実行時の古いコード混入を防止)
for mod in list(sys.modules.keys()):
    if mod == PACKAGE_NAME or mod.startswith(f"{PACKAGE_NAME}."):
        del sys.modules[mod]

# パスを追加してインポートを有効化
if TARGET_DIR not in sys.path:
    sys.path.insert(0, TARGET_DIR)

# 7. エントリポイントのインポート確認
try:
    main_mod = importlib.import_module(f"{PACKAGE_NAME}.main")
    run_pipeline = getattr(main_mod, "run_pipeline", None)
    print(f"✅ エントリポイント（{PACKAGE_NAME}.main）のインポートに成功しました。")
except ModuleNotFoundError as e:
    # パッケージまたは main モジュール自体が存在しない場合のみスキップ
    if e.name in (PACKAGE_NAME, f"{PACKAGE_NAME}.main"):
        print(f"ℹ️ {PACKAGE_NAME}.main が見つからないため、インポートをスキップしました（ご自身のリポジトリ構造に合わせて書き換えてください）。")
    else:
        # 依存ライブラリ等の読み込み失敗は本物のエラーとして再送出
        raise

print("✅ セットアップが完了しました。")
```

#### 💡 このコードを配布用ノートブックに組み込む手順

1. **新しいノートブックを作成**: 最初のセルに上記コードをそのまま貼り付けます。
2. **パラメータを3箇所変更**:
   - `REPO_URL`: ご自身の GitHub リポジトリ URL（例: `https://github.com/your-org/your-repo.git`）
   - `TARGET_TAG`: 配布したい安定版のタグ名（例: `v1.0.0`）
   - `PACKAGE_NAME`: リポジトリ内の自作モジュール名（例: `src` や `my_tool`）
3. **フォームを整えてコードを隠す（任意）**:
   - セルの右上メニュー（縦三点リーダー）から「フォーム」➔「コードを非表示」を選択すると、一般利用者の画面には生コードが隠れ、タイトルと入力フォーム（実行ボタン）だけが表示された綺麗な UI として配布できます。
4. **後続セル（Step 2）から実行**:
   - 次のセル（Step 2）を作成し、`from {PACKAGE_NAME}.main import run_pipeline; run_pipeline()`（例: `from src.main import run_pipeline; run_pipeline()`）のように呼び出せば、クローンされたコードがそのまま動作します。

:::note info
パッケージ名（ここでは `src`）は、サードパーティ製ライブラリと重複しない固有のパッケージ名（例: `my_tool`）にしておくと、名前空間の衝突をより確実に防ぐことができます。
:::

### 2. コラム：プライベートリポジトリを安全に扱うには？

もしコードを一般公開せず、チームメンバーや特定利用者向けに配布する場合、トークンをハードコードしてはいけません。Colab 標準の **「Secrets（🔑）」機能** を使って環境変数経由でトークンを注入し、クローン完了後にローカルの Git 設定からトークンを抹消します。

```python:secrets_private_clone.py
# プライベートリポジトリの場合の安全なトークン注入と痕跡消去
import re
import subprocess
from google.colab import userdata

try:
    gh_token = userdata.get("GITHUB_TOKEN")
    auth_repo_url = f"https://x-access-token:{gh_token}@github.com/my-org/private-tool.git"
except Exception:
    raise RuntimeError("🔑 左メニューの鍵アイコン (Secrets) から 'GITHUB_TOKEN' を設定してください。")

# クローン実行（エラー出力時にトークンが露出しないよう URL 部分を伏字化）
try:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", TARGET_TAG, auth_repo_url, TARGET_DIR],
        check=True,
        capture_output=True,
        text=True,
    )
    # クローン成功後、ローカル .git/config 内の URL からトークンを即時除去
    clean_repo_url = "https://github.com/my-org/private-tool.git"
    subprocess.run(
        ["git", "-C", TARGET_DIR, "remote", "set-url", "origin", clean_repo_url],
        check=True,
    )
except subprocess.CalledProcessError as e:
    raw_msg = e.stderr.strip() if e.stderr else "認証またはリポジトリの取得に失敗しました"
    err_msg = re.sub(r"https://[^@]+@", "https://***@", raw_msg)
    raise RuntimeError(f"❌ プライベートリポジトリの取得エラー: {err_msg}") from None
```

---

## 第6章：動かしてみた結果とベンチマーク

このセットアップ処理の導入により、運用の安定性が向上しました。

### 1. 起動時間とエラー耐性の比較

数百コミット規模のリポジトリを Colab 無料枠でクローンした筆者環境での実測値です。

| 項目 / 状態 | 通常の `!git clone` | 改善したセットアップ | 改善効果 |
| :--- | :---: | :---: | :--- |
| **初動の転送時間** | 数十秒（全履歴取得時） | **約 2〜4 秒（`--depth 1`）** | 初動の短縮 |
| **同一セルの再実行** | `already exists` でエラー停止 | **自動消去・再取得で衝突なし** | 冪等性の担保 |
| **タグ push 漏れ・タイポ時** | エラーによる処理中断 | **検証を経て許可時のみ `main` 退避** | タグ不在時に main 代替取得 |
| **自作モジュール更新** | 再起動しないと旧コード参照 | **`sys.modules` 破棄で即反映** | コード不整合の防止 |

### 2. 同一ランタイムでのセル再実行に対する冪等性と復旧

セッションが完全に切断された場合は Colab の VM ごと初期化されるため前回の残骸は残りませんが、実際の運用で頻繁に発生するのは**「エラー発生後やパラメータ変更時に、同一ランタイム上でセルをもう一度実行する」**ケースです。

セットアップコードの冒頭でカレントディレクトリを `/content` に戻し、既存フォルダを消去することで、同じセッション内でのやり直し時にディレクトリの衝突や、削除済みフォルダ内に留まって迷子になる問題を防ぎます。

※なお、`pip install` の実行途中で処理を強制中断した場合、site-packages 配下に不完全なパッケージが残存する可能性があります。その場合はノートブックのメニューからランタイムの再起動（Restart session）を行うか、`pip install --force-reinstall` を実行して修復します。

### 3. 今回の設計が持つトレードオフと運用上の制約

本設計は「一般利用者の手元で止まりにくく、安全に再実行できること」を優先しています。そのため、以下の**トレードオフ（何を得て、何を妥協したか）**が存在します。

- **【起動速度 vs Git履歴の閲覧性（`--depth 1` の代償）】最新コミットに絞ることで履歴追跡は切り捨てる**:
  `--depth 1` によって数秒での高速クローンを実現した代償として、過去のコミット履歴（`git log` や `git diff`、過去リビジョンへのチェックアウト）は利用できません。コードを配布して実行させる用途に特化し、開発者向けの履歴閲覧性は意図して捨てています
- **【可用性（止まらないこと） vs コードの再現性】main への代替切り替えは未検証コードのリスクを伴う**:
  タグ不在時に `main` ブランチへ自動退避して処理を継続させる選択肢を用意した代償として、開発途中の未検証コードが動いてしまうリスクを負います。そのため本設計では `ALLOW_MAIN_FALLBACK` をデフォルトで `False` とし、利用者がリスクを承知の上で明示的に有効化した場合のみ main へ切り替える安全弁を設けています
- **【高速な再読込 vs C拡張モジュールの更新限界】インメモリ破棄で扱えるのは純粋な Python コードに限定される**:
  ランタイム再起動を挟まずに `sys.modules` の破棄で最新コードを即時反映させる軽快さを取った代償として、`numpy` や `torch` など一度メモリに共有ライブラリ（`.so`）がロードされたバイナリ拡張パッケージの更新は反映できません。これらをアップグレードした場合は、OS プロセス自体の再起動（ランタイム再起動）が必要です
- **【安全なトークン注入 vs ログ出力・プロセス一覧からの漏洩リスク】例外メッセージのサニタイズが必要になる**:
  Secrets を用いてプライベートリポジトリを安全にクローンできる利便性を取った代償として、例外発生時にトークンが露出しないよう `stderr` のサニタイズ（伏字化）が必要です。また、クローン実行中は OS のプロセス一覧（`ps`）から一時的に引数のトークンが見える点にも留意が必要です

---

## 結び

Google Colab 上でリポジトリのコードを動かす作業は、ローカル開発環境での `git clone` とは前提が異なります。

「作業ディレクトリを /content に戻す」「既存フォルダの事前クリーンアップ」「`--depth 1` による最新取得」「タグ不在時の main 代替取得」「モジュールキャッシュの破棄」という一連の対策をセットアップセルに組み込むことで、ボタン1つで安定して起動できる配布基盤を構築できます。

本稿で確立したコード展開フローと、前編（データ永続化編）で設計した 2 層ストレージフローを組み合わせることで、ノートブック全体のライフサイクルは以下のように整理されます。

> **【Step 1】環境セットアップ（本稿：コード展開・依存解決）**  
> 　↓  
> **【Step 2】Pull Phase（前編：永続層 Drive から作業層ローカルディスクへデータステージング展開）**  
> 　↓  
> **【Step 3】処理実行（ローカルディスク上での高速バッチ・クエリ処理）**  
> 　↓  
> **【Step 4】Push Phase（前編：成果物先行・DB末尾置換でデータステージング書き戻し ＆ アンマウント）**

2 つの設計を組み合わせることで、対話的なノートブックであっても運用に耐えうる安定したバッチ実行基盤として活用できるようになります。

---

> **免責事項（Disclaimer）**  
> 本記事に掲載されているコードや設定例は筆者の検証環境に基づくものであり、外部サービス（Google Colab、GitHub 等）の仕様変更や実行環境の違い等によって生じたいかなる損害についても責任を負いかねます。実際の運用や設定はご自身の責任において確認の上で行ってください。
