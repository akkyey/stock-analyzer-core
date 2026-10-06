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
- Google Colab で GitHub リポジトリをクローンして実行させる配布モデルでは、再実行時の既存パス衝突、タグ未反映による即死、作業ディレクトリ消失によるインポートエラーが頻発する
- カレントディレクトリを `/content` に固定した冪等なクリーンアップ、`--depth 1` による浅いクローン、リモート検証に基づくセルフヒーリング型ブートストラップを導入した
- ユーザーにコマンド操作を要求せず、ボタン1つで安定版コードと依存関係を展開できる起動基盤を確立した

:::note info
**【Colab実運用基盤シリーズ】**
- **第1弾（データ永続化編）**: [Google Drive上のDuckDB直叩きで遅延とロック破損に直面した話：ColabとDrive間のPull/Push 2層ストレージ設計](qiita_part6_colab_ephemeral_storage_architecture.md)
<!-- ※ 第1弾公開後に実際のQiita URLへ差し替えてください -->
- **第2弾（コード配布編・本作）**: !git clone で配布したColabが本番で動かなくなる理由：壊れないブートストラップ設計と依存解決
:::

---

## はじめに

Google Colaboratory（以下、Colab）は、ブラウザだけで Python コードを実行できるため、オープンソースツールや分析パイプラインをエンドユーザーに配布する手段として広く使われています。

その際、最も一般的な配布方法は「ノートブック内に `!git clone https://github.com/...` を記述し、リポジトリのコードをそのまま実行させる」という構成です。しかし、この単純な `git clone` を本番で一般ユーザーに配ると、セルの再実行時やリリースのタイミングで高確率でエラーが発生し、パイプラインが停止します。

本稿では、特定業務ドメインに依存しない汎用的なノートブック配布モデルを対象とし、Colab 上で GitHub リポジトリのコードと依存ライブラリを確実に起動させるためのブートストラップ設計を記録します。

---

## 第1章：初期の設計と、暗黙の前提

GitHub で管理されているコードを Colab 上で利用者に実行させる際、多くのチュートリアルでは以下のようなシンプルなセルが提示されます。

```python
# 初期の典型的なセットアップセル
!git clone -b v1.0.0 https://github.com/my-org/my-tool.git
%cd my-tool
!pip install -r requirements.txt

from src.main import run_pipeline
run_pipeline()
```

この設計の背景には、以下の暗黙の前提（仮定）が存在します。

- **前提 1（単一実行の前提）**: 利用者は上から下へセルを1度だけ順番に実行する。セルの再実行やノートブックの途中でやり直す操作は行われない
- **前提 2（リモート同期完全性の前提）**: 指定したリリースブランチやタグは、利用者がセルを実行した瞬間に必ず GitHub 上に存在し、一発で取得できる
- **前提 3（環境一貫性の前提）**: `%cd` によるカレントディレクトリの移動や `pip install` の実行結果は、同一ランタイムセッション内であれば常に予測通りに維持される

---

## 第2章：本番運用で起きた3つのトラブル

上記のような単純なセットアップセルを一般配布したところ、利用者の手元で以下のトラブルが頻発しました。

### 1. セル再実行時のディレクトリ衝突エラー
エラー発生やパラメータ変更で利用者がセットアップセルをもう一度実行した際、Git が衝突エラーを起こして停止しました。
```text
fatal: destination path 'my-tool' already exists and is not an empty directory.
```
利用者は手動でファイルを消す方法がわからず、ランタイムの全初期化を強いられます。

### 2. リリース直後のタグ不一致による即死
新バージョン（例: `v1.3.1`）のリリース告知直後に、利用者がクローンを実行すると以下のエラーで停止しました。
```text
fatal: Remote branch v1.3.1 not found in upstream origin
```
開発者のタグ push 直後の同期遅延や、ノートブック側の指定タグのタイポ、ブランチの切り替え途中でパイプライン全体が行き止まりになり、手動介入が必要になりました。

### 3. 作業ディレクトリ消失によるモジュール見失い
セル内で `%cd my-tool` を実行した状態で、前述のエラー復旧や再実行のために外側から `shutil.rmtree("my-tool")` が走ると、Python プロセスのカレントディレクトリが存在しない幽霊パスを指す状態になります。その結果、以降のあらゆる相対パス操作やファイル読み込みが `FileNotFoundError` で即死する現象が発生しました。また、`sys.modules` に過去セッションの古いモジュールがキャッシュされ、再取得したコードがメモリ上に反映されない不整合も起きました。

---

## 第3章：なぜ単純な git clone では失敗するのか

起きていた問題の原因を掘り下げると、Colab という対話的実行環境の特性に対する考慮が不足していました。

### 1. 「やり直し」を考慮しない非冪等性
Colab は利用者が試行錯誤しながらセルを何度も再実行する環境です。クローン先にファイルが存在するかどうかを検証せず、常に同じ名前で `git clone` を叩くコードは、再実行に対する冪等性（何度実行しても同じ結果になること）を満たしていません。

### 2. リモート依存の単一障害点（SPOF）
特定のリリースタグ（`--branch v1.3.1`）をハードコードして指定することは、そのタグが存在しない瞬間にパイプライン全体を落とす単一障害点になります。ネットワーク一時エラーとタグ不在を区別せず、フォールバックの余地がない構成は対障害性が脆弱でした。

### 3. グローバル状態（カレントディレクトリとモジュールキャッシュ）の汚染
シェルコマンドの `%cd` はプロセス全体のカレントディレクトリを変更します。ディレクトリを削除・再作成する処理とカレントディレクトリの移動を無秩序に混在させると、実行パスの整合性が容易に崩壊します。

---

## 第4章：どう設計し直したか（自己修復型ブートストラップ）

破綻した前提を改め、利用者が何も考えずに何度実行しても確実に立ち上がるブートストラップを設計しました。

### 設計の基本方針

- **作業ディレクトリのルート固定（`/content` 原点回帰）**: 操作の直前に必ずカレントディレクトリを `/content` に戻す。既存の展開先ディレクトリは事前にクリーンアップし、常にゼロクリアされた状態からクローンを開始する
- **Shallow Clone（`--depth 1`）による帯域・起動時間の節約**: 過去のコミット履歴をすべて切り捨て、最新スナップショットのみを短時間で展開する
- **事前検証付きリトライ ＆ 自動フォールバック（セルフヒーリング）**: `git ls-remote` で通信状態とタグの存在を区別し、タグが存在しない場合は設定フラグ（許可時のみ）に基づき `main` ブランチへ退避して取得を継続する
- **動的インポートとモジュールキャッシュのリセット**: `sys.modules` 内の自作パッケージを明示的にクリアし、再実行時でも新しくクローンされたコードをメモリに再ロードする

```mermaid
flowchart TD
    Start["Step 1 実行開始"] --> CD["os.chdir('/content')<br/>(原点復帰)"]
    CD --> CheckDir{"TARGET_DIR が<br/>既に存在するか？"}
    CheckDir -- "Yes" --> Clean["shutil.rmtree()<br/>(既存ディレクトリを完全消去)"]
    CheckDir -- "No" --> Verify
    Clean --> Verify["check_tag() でリモート確認"]

    Verify -- "通信エラー (error)" --> Retry["指数バックオフで再試行 (最大3回)"]
    Retry --> Verify
    Verify -- "再試行上限超過" --> ErrorExit["RuntimeError で明示的中断<br/>(GitHub接続障害案内)"]

    Verify -- "タグ存在 (found)" --> CloneTag["Attempt: git clone --depth 1<br/>--branch TARGET_BRANCH (指定タグ)"]
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

```python
import os
import shutil
import subprocess
import sys
import time

# 1. 作業ディレクトリを必ず /content に戻す (作業ディレクトリ消失エラーの防止)
os.chdir("/content")

# @title 【Step 1】環境セットアップ
REPO_URL = "https://github.com/my-org/my-tool.git"
TARGET_DIR = "/content/my-tool"
TARGET_BRANCH = "v1.3.1"  # 検証済み安定版タグ
ALLOW_MAIN_FALLBACK = False  # @param {type:"boolean"}
# ↑ タグ不在時に未検証の最新開発版 (main) での実行を許容する場合は True

# 2. 既存ディレクトリが存在する場合は完全に削除 (再実行時の衝突防止)
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

status = check_tag(REPO_URL, TARGET_BRANCH)
if status == "error":
    raise RuntimeError("❌ GitHub に接続できません。ネットワーク状況を確認して再実行してください。")
if status == "not_found":
    if not ALLOW_MAIN_FALLBACK:
        raise RuntimeError(f"❌ タグ '{TARGET_BRANCH}' が見つかりません。タグ名を確認してください。")
    print(f"⚠️ タグ '{TARGET_BRANCH}' がないため main で続行します（未検証コードの可能性あり）。")
    target_to_clone = "main"
else:
    target_to_clone = TARGET_BRANCH

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
        # トークン等を含むコマンド全文ではなく、標準エラーのみを安全に出力
        err_msg = e.stderr.strip() if e.stderr else "クローン処理に失敗しました"
        print(f"⚠️ Git クローン警告 (attempt {attempt}): {err_msg}")
        if os.path.exists(TARGET_DIR):
            shutil.rmtree(TARGET_DIR, ignore_errors=True)
        time.sleep(2)

if not clone_success:
    raise RuntimeError("❌ GitHub からのリポジトリ取得に失敗しました。ネットワーク接続または GitHub の稼働状況を確認してください。")
```

### 2. 依存ライブラリの導入とモジュールキャッシュのリセット

画面が長いログで埋め尽くされるのを防ぎつつ、過去セッションのモジュールキャッシュを無効化します。

```python
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

もしコードを一般公開せず、社内メンバーや特定利用者向けに配布する場合、トークンをハードコードしてはいけません。Colab 標準の **「Secrets（🔑）」機能** を使って環境変数経由でトークンを注入し、クローン完了後にローカルの Git 設定からトークンを抹消します。

```python
# プライベートリポジトリの場合の安全なトークン注入と痕跡消去
from google.colab import userdata

try:
    gh_token = userdata.get("GITHUB_TOKEN")
    auth_repo_url = f"https://x-access-token:{gh_token}@github.com/my-org/private-tool.git"
except Exception:
    raise RuntimeError("🔑 左メニューの鍵アイコン (Secrets) から 'GITHUB_TOKEN' を設定してください。")

# クローン実行（エラー出力時にトークンが露出しないよう e.stderr のみを使用）
try:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", TARGET_BRANCH, auth_repo_url, TARGET_DIR],
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
    err_msg = e.stderr.strip() if e.stderr else "認証またはリポジトリの取得に失敗しました"
    raise RuntimeError(f"❌ プライベートリポジトリの取得エラー: {err_msg}") from None
```

---

## 第6章：動かしてみた結果とベンチマーク

この自己修復型ブートストラップの導入により、運用の安定性が向上しました。

### 1. 起動時間とエラー耐性の比較（傾向）

| 項目 / 状態 | 通常の `!git clone` | 自己修復型ブートストラップ | 改善効果 |
| :--- | :--- | :--- | :--- |
| **初動の転送時間** | 数十秒（履歴数千コミットの場合） | **約 2〜4 秒（`--depth 1`）** | 初動の短縮 |
| **同一セルの再実行** | `already exists` で即死 | **自動消去・再取得で衝突なし** | 冪等性の担保 |
| **タグ未反映・タイポ時** | エラーで完全停止 | **検証を経て許可時のみ `main` 退避** | セルフヒーリング |
| **自作モジュール更新** | 再起動しないと旧コード参照 | **`sys.modules` 破棄で即反映** | コード不整合の防止 |

### 2. 今回の設計の限界とトレードオフ

- **GitHub 側の全面障害**: GitHub 自身がダウンしている場合はフォールバックも成立しないため、外部ホスティングの冗長化まではカバーできません
- **`main` フォールバック時の動作差異**: タグ取得失敗時に `main` を取得した場合、開発中の未検証コードが動く可能性があります。そのため本設計では `ALLOW_MAIN_FALLBACK` フラグを用意し、明示的に許容された場合のみフォールバックする制御としています
- **C拡張モジュールなどの更新制限**: `sys.modules` からの破棄で即時リロードできるのは純粋な Python スクリプトのみです。`numpy` や `torch` などのバイナリ拡張を含むライブラリを `pip install --upgrade` した場合は、Colab ランタイム自体の再起動（`os.kill(os.getpid(), 9)` など）が必要です
- **トークン漏洩リスク（プライベート時）**: Secrets を用いる場合でも、`CalledProcessError` の例外オブジェクト（`str(e)`）を不用意に出力すると URL 内部のトークンがコンソールに漏洩するリスクがあります。エラーハンドリングでは `e.stderr` のみを利用し、クローン完了後に `git remote set-url` で URL をサニタイズする防御策が不可欠です

---

## 結び

Google Colab 上でリポジトリのコードを動かす作業は、ローカル開発環境での `git clone` とは前提が異なります。

「作業ディレクトリの原点復帰」「既存フォルダの事前クリーンアップ」「Shallow Clone」「事前検証付きフォールバック」「モジュールキャッシュの破棄」という一連の防壁をブートストラップに組み込むことで、ユーザーがつまずくことなくボタン1つで安定稼働する配布基盤が完成します。

本稿で確立したコード展開フローと、前編（データ永続化編）で設計した 2 層ストレージフローを組み合わせることで、ノートブック全体のライフサイクルは以下のように綺麗に整流化されます。

> **【Step 1】ブートストラップ実行（本稿：コード展開・依存解決）**  
> 　↓  
> **【Step 2】Pull Phase（前編：永続層 Drive から作業層 SSD へデータ一括展開）**  
> 　↓  
> **【Step 3】処理実行（ローカル SSD 上での高速バッチ・クエリ処理）**  
> 　↓  
> **【Step 4】Push Phase（前編：成果物先行・DB末尾置換で永続層へ同期 ＆ アンマウント）**

2 つの設計を組み合わせることで、Colab は「壊れやすい対話的ノートブック」から「本番運用に耐えうる堅牢な配布バッチ基盤」へと進化します。


