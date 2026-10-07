---
title: "!git clone で配布したColabが本番で動かなくなる理由：壊れないブートストラップ設計と依存解決"
tags:
  - Python
  - GoogleColaboratory
  - Git
  - GitHub
  - アーキテクチャ
private: false
---

## TL;DR
- Google Colab で GitHub リポジトリをクローンして実行させる配布モデルでは、再実行時の既存パス衝突、タグ未反映によるエラー中断、作業ディレクトリ消失によるインポートエラーが頻発する
- カレントディレクトリを `/content` に固定した冪等なクリーンアップ、`--depth 1` による浅いクローン、リモート検証に基づくセルフヒーリング型ブートストラップ設計にする
- これにより、ユーザーにコマンド操作を要求せず、ボタン1つで安定版コードと依存関係を展開できる起動基盤を確保できる

:::note info
**【Colab実運用基盤シリーズ】**
- **第1弾（データ永続化編）**: [Google Drive上のDuckDBを直接読み書きして遅延とロック破損が起きた話：ColabとDrive間のデータステージング（2層ストレージ）設計](qiita_part6_colab_ephemeral_storage_architecture.md)
<!-- ※ 第1弾公開後に実際のQiita URLへ差し替えてください -->
- **第2弾（コード配布編・本作）**: !git clone で配布したColabが本番で動かなくなる理由：壊れないブートストラップ設計と依存解決
:::

---

## はじめに

Google Colaboratory（以下、Colab）は、ブラウザだけで Python コードを実行できるため、オープンソースツールや分析パイプライン（前編で触れた株価スクリーナーなど）をチームメンバーや一般ユーザーに配布する手段として広く使われています。

その際、最も一般的な配布方法は「ノートブック内に `!git clone https://github.com/...` を記述し、リポジトリのコードをそのまま実行させる」という構成です。しかし、この単純な `git clone` をそのまま配ると、セルの再実行時やリリースのタイミングでエラーが発生することがあり、パイプラインが停止します。

本稿では、Colab 上で GitHub リポジトリのコードと依存ライブラリを確実に起動させるためのブートストラップ設計[^bootstrap-note]をまとめます。

---

## 第1章：初期の設計と、暗黙の前提

GitHub で管理されているコードを Colab 上で利用者に実行させる際、多くのチュートリアルでは以下のようなシンプルなセルが提示されます。

```python:naive_bootstrap.py
# 初期の典型的なセットアップセル
!git clone -b v1.0.0 https://github.com/my-org/my-tool.git
%cd my-tool
!pip install -r requirements.txt

from src.main import run_pipeline
run_pipeline()
```

この設計の背景には、以下のような暗黙の前提が存在していました。

- **前提 1（単一実行の前提）**: 利用者は上から下へセルを1度だけ順番に実行する。セルの再実行やノートブックの途中でやり直す操作は行われない
- **前提 2（リモート同期完全性の前提）**: 指定したリリースブランチやタグは、利用者がセルを実行した瞬間に必ず GitHub 上に存在し、即時に取得できる
- **前提 3（環境一貫性の前提）**: `%cd` によるカレントディレクトリの移動や `pip install` の実行結果は、同一ランタイムセッション内であれば常に予測通りに維持される

[^bootstrap-note]: **ブートストラップ（Bootstrap）**: 本来のプログラムを実行する前に、必要なコードのクローン、依存ライブラリのインストール、実行パスの初期化などを自律的に行い、システムを実行可能な状態に立ち上げる初期起動処理のこと。

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

### 1. 「やり直し」を考慮しない非冪等性
Colab は利用者が試行錯誤しながらセルを何度も再実行する環境です。クローン先にファイルが存在するかどうかを検証せず、常に同じ名前で `git clone` を叩くコードは、再実行に対する冪等性（何度実行しても同じ結果になること）[^idempotency-note] を満たしていません。

### 2. リモート依存の単一障害点（SPOF）[^spof-note]
特定のリリースタグ（`--branch v1.3.1`）をハードコードして指定することは、タグの push 漏れやタイポがあった瞬間にパイプライン全体を中断させる要因になります。通信エラーとタグ不在を区別せず、フォールバックの余地がない構成は耐障害性が不十分でした。

### 3. グローバル状態（カレントディレクトリとモジュールキャッシュ）の汚染

Colab ノートブックのカーネルは、単一の持続的な Python プロセス上で対話的に実行されます。シェルコマンドの `%cd` や `import` による状態変更はプロセス全体に永続するため、以下の不整合を引き起こします。

1. **カレントディレクトリの宙づり（Dangling CWD）**:
   Linux プロセスにおいて、カレントワーキングディレクトリ（CWD）は OS カーネルの inode（ディレクトリ実体）を参照しています。セル内で `%cd my-tool` を実行した後に、再クローン目的で外側から `shutil.rmtree("my-tool")` を実行すると、ディレクトリ実体がディスクから解放され、プロセスの CWD は「削除済みで存在しない無効な inode」を掴んだままになります。この状態に陥ると、以降の相対パス解決で `FileNotFoundError`（ENOENT）が発生します。
2. **`sys.modules` によるインメモリキャッシュの固着**:
   Python のインポート機構（`importlib`）は、一度読み込んだモジュールをプロセス空間のグローバル辞書 `sys.modules` にキャッシュします。ファイルシステム上の `.py` ファイルを新バージョンでクローンし直しても、Python はディスクを再走査せずメモリ上の旧オブジェクトを返し続けます。その結果、セルの再実行を行っても最新のバグ修正が反映されず、新設された関数が見つからない `AttributeError` や古いロジックによる不整合が発生します。

[^idempotency-note]: **冪等性（Idempotency / べきとうせい）**: ある操作を1回実行しても、複数回繰り返して実行しても、得られる結果やシステムの状態が全く同一になる性質のこと。
[^spof-note]: **SPOF（Single Point of Failure / 単一障害点）**: システムの中で、そこが1箇所でも停止・故障するとシステム全体の機能停止を引き起こすボトルネックのこと。

---

## 第4章：どう設計し直したか（自己修復型ブートストラップ）

これらの前提の限界を踏まえ、利用者がセルの再実行を行っても安定して起動するブートストラップを設計し直しました。

### 設計の基本方針

- **作業ディレクトリのルート固定（`/content` 原点回帰）**: 操作の直前に必ずカレントディレクトリを `/content` に戻す。既存の展開先ディレクトリは事前にクリーンアップし、常にゼロクリアされた状態からクローンを開始する
- **Shallow Clone（浅いクローン：`--depth 1`）[^shallow-note] による帯域・起動時間の節約**: 過去のコミット履歴をすべて切り捨て、最新スナップショットのみを短時間で展開する
- **事前検証付きリトライ ＆ 自動フォールバック（セルフヒーリング）**: `git ls-remote` で通信状態とタグの存在を区別し、タグが存在しない場合は設定フラグ（許可時のみ）に基づき `main` ブランチへ退避して取得を継続する
- **動的インポートとモジュールキャッシュのリセット**: `sys.modules` 内の自作パッケージを明示的にクリアし、再実行時でも新しくクローンされたコードをメモリに再ロードする

[^shallow-note]: **Shallow Clone（浅いクローン）**: 過去の全コミット履歴をダウンロードせず、指定した最新のスナップショット（深度1）のみを取得する Git の機能。ネットワーク転送量とクローン所要時間を最小化できます。

```mermaid
flowchart TD
    Start["Step 1 実行開始"] --> CD["os.chdir('/content')<br/>(原点復帰)"]
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

### 1. 堅牢なクローンと事前検証付きフォールバック

以下のコードを、セットアップセルの先頭に配置します。

```python:step1_git_bootstrap.py
# @title 【Step 1】環境セットアップ
import os
import re
import shutil
import subprocess
import sys
import time

# 1. 作業ディレクトリを必ず /content に戻す (作業ディレクトリ消失エラーの防止)
os.chdir("/content")

REPO_URL = "https://github.com/my-org/my-tool.git"
TARGET_DIR = "/content/my-tool"
TARGET_TAG = "v1.3.1"  # 検証済み安定版タグ
ALLOW_MAIN_FALLBACK = False  # @param {type:"boolean"}
# ↑ タグ不在時に未検証の最新開発版 (main) での実行を許容する場合は True

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
    raise RuntimeError("❌ GitHub に接続できません。ネットワーク状況を確認して再実行してください。")
if status == "not_found":
    if not ALLOW_MAIN_FALLBACK:
        raise RuntimeError(f"❌ タグ '{TARGET_TAG}' が見つかりません。タグ名を確認してください。")
    print(f"⚠️ タグ '{TARGET_TAG}' がないため main で続行します（未検証コードの可能性あり）。")
    target_to_clone = "main"
else:
    target_to_clone = TARGET_TAG

# 4. Shallow Clone の実行 (通信エラー時はリトライ)
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
    raise RuntimeError("❌ GitHub からのリポジトリ取得に失敗しました。ネットワーク接続または GitHub の稼働状況を確認してください。")
```

### 2. 依存ライブラリの導入とモジュールキャッシュのリセット

画面が長いログで埋め尽くされるのを防ぎつつ、過去セッションのモジュールキャッシュを無効化します。

```python:step1_dependencies.py
# 5. 依存ライブラリの導入 (sys.executable を使用し、作業ディレクトリを汚染せず絶対パスで指定)
req_path = os.path.join(TARGET_DIR, "requirements.txt")
if os.path.exists(req_path):
    try:
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
# ※ 外部PyPIパッケージのC拡張モジュール等は破棄対象外（ランタイム再起動が必要）
for mod in list(sys.modules.keys()):
    if mod == "src" or mod.startswith("src."):
        del sys.modules[mod]

# パスを追加してインポートを有効化
if TARGET_DIR not in sys.path:
    sys.path.insert(0, TARGET_DIR)

from src.main import run_pipeline
print("✅ セットアップが完了しました。")
```

:::note info
パッケージ名（ここでは `src`）は、サードパーティ製ライブラリと重複しない固有のパッケージ名（例: `my_tool`）にしておくと、名前空間の衝突をより確実に防ぐことができます。
:::

### 3. コラム：プライベートリポジトリを安全に扱うには？

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

このブートストラップの導入により、運用の安定性が向上しました。

### 1. 起動時間とエラー耐性の比較

数百コミット規模のリポジトリを Colab 無料枠でクローンした筆者環境での実測値です。

| 項目 / 状態 | 通常の `!git clone` | 改善したブートストラップ | 改善効果 |
| :--- | :---: | :---: | :--- |
| **初動の転送時間** | 数十秒（全履歴取得時） | **約 2〜4 秒（`--depth 1`）** | 初動の短縮 |
| **同一セルの再実行** | `already exists` でエラー停止 | **自動消去・再取得で衝突なし** | 冪等性の担保 |
| **タグ push 漏れ・タイポ時** | エラーによる処理中断 | **検証を経て許可時のみ `main` 退避** | 安全なフォールバック |
| **自作モジュール更新** | 再起動しないと旧コード参照 | **`sys.modules` 破棄で即反映** | コード不整合の防止 |

### 2. 同一ランタイムでのセル再実行に対する冪等性と復旧

セッションが完全に切断された場合は Colab の VM ごと初期化されるため前回の残骸は残りませんが、実際の運用で頻繁に発生するのは**「エラー発生後やパラメータ変更時に、同一ランタイム上でセルをもう一度実行する」**ケースです。

セットアップコードの冒頭でカレントディレクトリを `/content` に戻し、既存フォルダを消去することで、同じセッション内でのやり直し時にディレクトリ衝突や無効な inode 参照（Dangling CWD）が起きるのを防ぎます。

※なお、`pip install` の実行途中で処理を強制中断した場合、site-packages 配下に不完全なパッケージが残存する可能性があります。その場合はノートブックのメニューからランタイムの再起動（Restart session）を行うか、`pip install --force-reinstall` を実行して修復します。

### 3. 今回の設計が持つトレードオフと運用上の制約

本設計は「一般利用者の手元で止まりにくく、安全に再実行できること」を優先しています。そのため、以下の**トレードオフ（何を得て、何を妥協したか）**が存在します。

- **【起動速度 vs Git履歴の閲覧性（Shallow Cloneの代償）】最新スナップショットに絞ることで履歴追跡は切り捨てる**:
  `--depth 1` によって数秒での高速クローンを実現した代償として、過去のコミット履歴（`git log` や `git diff`、過去リビジョンへのチェックアウト）は利用できません。コードを配布して実行させる用途に特化し、開発者向けの履歴閲覧性は意図して捨てています
- **【可用性（止まらないこと） vs コードの再現性】自動フォールバックは未検証コードのリスクを伴う**:
  タグ不在時に `main` ブランチへ自動退避して処理を継続させる選択肢を用意した代償として、開発途中の未検証コードが動いてしまうリスクを負います。そのため本設計では `ALLOW_MAIN_FALLBACK` をデフォルトで `False` とし、利用者がリスクを承知の上で明示的に有効化した場合のみフォールバックする安全弁を設けています
- **【高速な再読込 vs C拡張モジュールの更新限界】インメモリ破棄で扱えるのは純粋な Python コードに限定される**:
  ランタイム再起動を挟まずに `sys.modules` の破棄で最新コードを即時反映させる軽快さを取った代償として、`numpy` や `torch` など一度メモリに共有ライブラリ（`.so`）がロードされたバイナリ拡張パッケージの更新は反映できません。これらをアップグレードした場合は、OS プロセス自体の再起動（ランタイム再起動）が必要です
- **【安全なトークン注入 vs ログ出力・プロセス一覧からの漏洩リスク】例外メッセージのサニタイズが必要になる**:
  Secrets を用いてプライベートリポジトリを安全にクローンできる利便性を取った代償として、例外発生時にトークンが露出しないよう `stderr` のサニタイズ（伏字化）が必要です。また、クローン実行中は OS のプロセス一覧（`ps`）から一時的に引数のトークンが見える点にも留意が必要です

---

## 結び

Google Colab 上でリポジトリのコードを動かす作業は、ローカル開発環境での `git clone` とは前提が異なります。

「作業ディレクトリの原点復帰」「既存フォルダの事前クリーンアップ」「Shallow Clone」「事前検証付きフォールバック」「モジュールキャッシュの破棄」という一連の対策をブートストラップに組み込むことで、ボタン1つで安定して起動できる配布基盤を構築できます。

本稿で確立したコード展開フローと、前編（データ永続化編）で設計した 2 層ストレージフローを組み合わせることで、ノートブック全体のライフサイクルは以下のように整理されます。

> **【Step 1】ブートストラップ実行（本稿：コード展開・依存解決）**  
> 　↓  
> **【Step 2】Pull Phase（前編：永続層 Drive から作業層ローカルディスクへデータステージング展開）**  
> 　↓  
> **【Step 3】処理実行（ローカルディスク上での高速バッチ・クエリ処理）**  
> 　↓  
> **【Step 4】Push Phase（前編：成果物先行・DB末尾置換でデータステージング書き戻し ＆ アンマウント）**

2 つの設計を組み合わせることで、対話的なノートブックであっても運用に耐えうる安定したバッチ実行基盤として活用できるようになります。
