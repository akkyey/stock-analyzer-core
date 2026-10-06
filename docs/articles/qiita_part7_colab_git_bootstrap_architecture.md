---
title: !git clone で配布したColabが本番で動かなくなる理由：壊れないブートストラップ設計と依存解決
tags:
  - Python
  - GoogleColaboratory
  - Git
  - GitHub
  - アーキテクチャ
private: false
---

## 3行要約
- Google Colab で GitHub リポジトリをクローンして実行させる配布モデルでは、再実行時の既存パス衝突、タグ未反映による即死、作業ディレクトリ消失によるインポートエラーが頻発する
- カレントディレクトリを `/content` に固定した冪等なクリーンアップ、`--depth 1` による浅いクローン、指定タグ失敗時の `main` 自動フォールバックを導入した
- ユーザーにコマンド操作を要求せず、ボタン1つで安定版コードと依存関係を展開できるセルフヒーリング型ブートストラップを確立した

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
エラーやパラメータ変更で利用者がセットアップセルをもう一度実行した際、Git が衝突エラーを起こして停止しました。
```text
fatal: destination path 'my-tool' already exists and is not an empty directory.
```
利用者は「どうしていいかわからない」状態に陥り、ランタイムの全初期化を強いられます。

### 2. リリース直後のタグ未反映による即死
新バージョン（例: `v1.3.1`）のリリースアナウンス直後に、利用者がクローンを実行すると以下のエラーで停止しました。
```text
fatal: Remote branch v1.3.1 not found in upstream origin
```
開発者のタグ push タイミングとの微細なタイムラグや、タイポ、ブランチの切り替え途中でパイプライン全体が行き止まりになり、手動介入が必要になりました。

### 3. 作業ディレクトリ消失によるモジュール見失い
セル内で `%cd my-tool` を実行した状態で、前述のエラー復旧や再実行のために外側から `shutil.rmtree("my-tool")` が走ると、Python プロセスのカレントディレクトリが存在しない幽霊パスを指す状態になります。その結果、以降のあらゆる相対パス操作やファイル読み込みが `FileNotFoundError` で即死する現象が発生しました。また、`sys.modules` に過去セッションの古いモジュールがキャッシュされ、最新コードが反映されない不整合も起きました。

---

## 第3章：なぜ単純な git clone では失敗するのか

起きていた問題の原因を掘り下げると、Colab という対話的実行環境の特性に対する考慮が不足していました。

### 1. 「やり直し」を考慮しない非冪等性
Colab は利用者が試行錯誤しながらセルを何度も再実行する環境です。クローン先にファイルが存在するかどうかを検証せず、常に同じ名前で `git clone` を叩くコードは、再実行に対する冪等性（何度実行しても同じ結果になること）を満たしていません。

### 2. リモート依存の単一障害点（SPOF）
特定のリリースタグ（`--branch v1.3.1`）をハードコードして指定することは、そのタグが存在しない瞬間にパイプライン全体を落とす単一障害点になります。配布用ノートブックにおいては、「指定タグが万が一取得できなくても、最新の開発版（`main`）に退避して処理を継続する」フォールバック機構が欠落していました。

### 3. グローバル状態（カレントディレクトリとモジュールキャッシュ）の汚染
シェルコマンドの `%cd` はプロセス全体のカレントディレクトリを変更します。ディレクトリを削除・再作成する処理とカレントディレクトリの移動を無秩序に混在させると、実行パスの整合性が容易に崩壊します。

---

## 第4章：どう設計し直したか（自己修復型ブートストラップ）

破綻した前提を改め、利用者が何も考えずに何度実行しても確実に立ち上がるブートストラップを設計しました。

### 設計の基本方針

- **作業ディレクトリのルート固定（`/content` 原点回帰）**: 操作の直前に必ずカレントディレクトリを `/content` に戻す。既存の展開先ディレクトリは事前にクリーンアップし、常にゼロクリアされた状態からクローンを開始する
- **Shallow Clone（`--depth 1`）による帯域・起動時間の節約**: 過去のコミット履歴をすべて切り捨て、最新スナップショットのみを数秒で展開する
- **リトライ ＆ `main` 自動フォールバック（セルフヒーリング）**: 指定タグのクローンに失敗した場合、警告を表示した上で自動的に `main` ブランチを取得し直す
- **動的インポートとモジュールキャッシュのリセット**: `sys.modules` 内の該当パッケージを明示的にクリアし、再実行時でも確実に新しくクローンされたコードをメモリに再ロードする

```mermaid
flowchart TD
    Start["Step 1 実行開始"] --> CD["os.chdir('/content')<br/>(原点復帰)"]
    CD --> CheckDir{"TARGET_DIR が<br/>既に存在するか？"}
    CheckDir -- Yes --> Clean["shutil.rmtree()<br/>(既存ディレクトリを完全消去)"]
    CheckDir -- No --> Clone1
    Clean --> Clone1["Attempt 1: git clone --depth 1<br/>--branch TARGET_BRANCH (指定タグ)"]

    Clone1 --> Result1{"クローン成功？"}
    Result1 -- Yes --> Pip["pip install -q -r requirements.txt"]
    Result1 -- No --> Fallback["警告出力 ＆ TARGET_DIR クリーンアップ<br/>ブランチを 'main' に切り替え"]
    Fallback --> Clone2["Attempt 2: git clone --depth 1<br/>--branch main (自動フォールバック)"]
    Clone2 --> Result2{"クローン成功？"}
    Result2 -- Yes --> Pip
    Result2 -- No --> Fatal["RuntimeError で明示的中断<br/>(GitHub障害案内)"]

    Pip --> Reload["sys.modules から自作パッケージを破棄<br/>(最新モジュールの強制再読込)"]
    Reload --> Ready["環境セットアップ完了 (次Stepへ)"]

    style Start fill:#f9f9f9,stroke:#333,stroke-width:1px,color:#333
    style Pip fill:#f0fff0,stroke:#2a2,stroke-width:1px,color:#333
    style Ready fill:#d0ffd0,stroke:#2a2,stroke-width:2px,color:#333
    style Fatal fill:#ffe0e0,stroke:#d33,stroke-width:2px,color:#333
```

---

## 第5章：実装のポイントとコード

### 1. 堅牢なクローンと自動フォールバック

以下のコードを、セットアップセルの先頭に配置します。

```python
import os
import shutil
import subprocess
import sys

# 1. 作業ディレクトリを必ず /content に戻す (作業ディレクトリ消失エラーの防止)
os.chdir("/content")

REPO_URL = "https://github.com/my-org/my-tool.git"
TARGET_DIR = "/content/my-tool"
TARGET_BRANCH = "v1.3.1"  # 検証済み安定版タグ

# 2. 既存ディレクトリが存在する場合は完全に削除 (再実行時の衝突防止)
if os.path.exists(TARGET_DIR):
    shutil.rmtree(TARGET_DIR, ignore_errors=True)

# 3. 耐障害性クローン (指定タグ失敗時は main へ自動フォールバック)
clone_success = False
current_branch = TARGET_BRANCH

for attempt in range(1, 3):
    try:
        print(f"📥 リポジトリを取得中 (attempt {attempt}/2, target: {current_branch})...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", current_branch, REPO_URL, TARGET_DIR],
            check=True,
            capture_output=True,
            text=True,
        )
        clone_success = True
        break
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else str(e)
        print(f"⚠️ Git クローン警告 (attempt {attempt}): {err_msg}")
        if attempt == 1:
            print("   安定版の取得に失敗したため、main ブランチへフォールバックして再試行します...")
            current_branch = "main"
        if os.path.exists(TARGET_DIR):
            shutil.rmtree(TARGET_DIR, ignore_errors=True)

if clone_success and current_branch != TARGET_BRANCH:
    print(f"⚠️ 安定版 ({TARGET_BRANCH}) を取得できなかったため、最新の main ブランチで続行します。")
if not clone_success:
    raise RuntimeError("❌ GitHub からのリポジトリ取得に失敗しました。インターネット接続または GitHub の稼働状況を確認してください。")
```

### 2. 依存ライブラリのサイレント導入とモジュール再ロード

画面が長いログで埋め尽くされるのを防ぎつつ、過去セッションのモジュールキャッシュを無効化します。

```python
# 4. 依存ライブラリの導入 (ログを汚さない -q オプション)
try:
    os.chdir(TARGET_DIR)
    subprocess.run(["pip", "install", "-q", "-r", "requirements-colab.txt"], check=True)
except subprocess.CalledProcessError as e:
    raise RuntimeError(f"❌ 依存ライブラリのインストールに失敗しました: {e}") from None

# 5. 既存のキャッシュモジュールを安全に破棄して最新版を確実にロード
for mod in list(sys.modules.keys()):
    if mod.startswith("src"):
        del sys.modules[mod]

# パスを追加してインポートを有効化
if TARGET_DIR not in sys.path:
    sys.path.insert(0, TARGET_DIR)

from src.main import run_pipeline
print("✅ セットアップが完了しました。")
```

### 3. コラム：プライベートリポジトリを安全に扱うには？

もしコードを一般公開せず、購入者や社内メンバー限定で Colab から実行させたい場合、ハードコードでトークンを埋め込んではいけません。Colab 標準の **「Secrets（🔑）」機能** を使って環境変数経由でトークンを注入します。

```python
# プライベートリポジトリの場合のトークン注入
from google.colab import userdata

try:
    gh_token = userdata.get("GITHUB_TOKEN")
    auth_repo_url = f"https://x-access-token:{gh_token}@github.com/my-org/private-tool.git"
except Exception:
    raise RuntimeError("🔑 左メニューの鍵アイコン (Secrets) から 'GITHUB_TOKEN' を設定してください。")
```

---

## 第6章：動かしてみた結果とベンチマーク

この自己修復型ブートストラップの導入により、運用の安定性が向上しました。

### 1. 起動時間とエラー耐性の比較（傾向）

| 項目 / 状態 | 通常の `!git clone` | 自己修復型ブートストラップ | 改善効果 |
| :--- | :--- | :--- | :--- |
| **起動・転送時間** | 数十秒（履歴全取得） | **約 2〜4 秒（`--depth 1`）** | 初動の高速化 |
| **同一セルの再実行** | `already exists` で即死 | **自動消去・再取得で 100% 成功** | 冪等性の担保 |
| **タグ未反映・タイポ時** | エラーで完全停止 | **`main` へ自動フォールバック** | セルフヒーリング |
| **依存モジュール更新** | 再起動しないと旧コード参照 | **`sys.modules` 破棄で即反映** | コード不整合の防止 |

### 2. 今回の設計の限界とトレードオフ

- **GitHub 側の全面障害**: GitHub 自身がダウンしている場合はフォールバックも成立しないため、外部ホスティングの冗長化まではカバーできません
- **`main` フォールバック時の動作差異**: タグ取得失敗時に `main` を取得した場合、開発中の破壊的変更が含まれている可能性があります。本番環境では警告メッセージを明示し、ユーザーに状況を知らせることが重要です
- **トークン漏洩リスク（プライベート時）**: Secrets を用いる場合でも、ノートブック内でトークンを出力（print）するコードを含めないよう注意が必要です

---

## 結び

Google Colab 上でリポジトリのコードを動かす作業は、ローカル環境での `git clone` とは前提が異なります。

「作業ディレクトリの原点復帰」「既存フォルダの事前クリーンアップ」「Shallow Clone」「フォールバック」「モジュールキャッシュの破棄」という一連の防壁をブートストラップに組み込むことで、ユーザーがつまずくことなくボタン1つで安定稼働する配布基盤が完成します。
