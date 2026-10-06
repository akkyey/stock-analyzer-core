# レビュー票: v1.3.1（財務データの更新）と、初版公開前の確認・作業

- **作成日**: 2026-10-06
- **起票**: Claude（Sonnet 5.5 / Opus 5.5）の作業セッション
- **担当**: リポジトリの所有者（以下の検証・作業は、所有者が実施する）
- **対象**: `release/v1.3.1`（リモートの先頭は `4fb1d62`。ローカルに未 push のコミットがある。§3）
- **ステータス**: **作業待ち**（コード・テスト・CLI 検証は完了。**出所未確認の列の扱いの決定**、Colab での実機確認、公開前の整備が残っている）
- **関連資料**:
  - 設計書: [`data_validity_and_hybrid_fetch_architecture.md`](../designs/data_validity_and_hybrid_fetch_architecture.md)（3.3 節）、[`data_sources_and_redistribution_policy.md`](../designs/data_sources_and_redistribution_policy.md)（データの出所と再配布方針）、[`message_display_improvement_design.md`](../designs/message_display_improvement_design.md)（2.4 節）、[`colab_execution_architecture_and_guide_design.md`](../designs/colab_execution_architecture_and_guide_design.md)（3.4.1 節）
  - 報告書: [`v1_3_0_release_and_v1_3_1_seed_report_20261006.md`](v1_3_0_release_and_v1_3_1_seed_report_20261006.md)（経緯、§8 追補、誤りと是正）

---

## 1. この票の目的

1. v1.3.1 で行った変更（財務データの作り直しと、既存 DB への反映ほか）の**現状と、確認の済んだこと・済んでいないこと**を、一箇所にまとめる。
2. 所有者が、残りの**検証と作業を、この票だけで再現・実施できる**ようにする（手順、期待する結果、合否の基準を記載する）。
3. 初版公開（履歴をリセットして出す予定）の前に、**確認・整備すべきこと**を洗い出す。

## 2. 現状の要約

| 区分 | 状態 |
| :--- | :--- |
| v1.3.0 | 公開済み（`main` = `d664d8c`、タグ `v1.3.0`）。Colab の本番の保存先でも、タグの取得と差分更新を確認済み |
| v1.3.1 のコード | 実装・テスト済み（ruff・mypy・テスト 288 件・radon の複雑度 21 以上は 0 件） |
| 同梱の財務データ（シード） | EDINET から作り直した**候補**を、リポジトリに置いた（識別子 `33d17aaaecea`、3,926 銘柄）。**出所未確認の列を含む**（T-01） |
| CLI での検証 | 5 回の実行すべて完了・合格（§4 T-04） |
| Colab での実機確認 | **未実施**（最終版のシードでは、まだ） |
| 公開前の整備 | **未実施**（T-02・T-03・T-07） |

## 3. リポジトリの状態（引き継ぎ時点）

- ブランチ: `release/v1.3.1`。リモート（`origin/release/v1.3.1`）は `4fb1d62`。
- **未 push のコミット**（ローカル）: `7b5f864`（ガードとスクリプトの修正）、`38e0b7c`（文言の統一）、`c99b1d7`（個人のテスト用ファイルを追跡から外す）、および、本票・設計書・報告書・シードのコミット（作成後）。push は、所有者の判断で行う。
- 追跡から外したファイル: `docs/articles/aaaa`（所有者のテスト用ファイル。誤って追跡に入れ、push 済みの `7c146ba` に含まれている。ファイルは作業ディレクトリに残してある）。
- 未追跡のまま残してあるもの: `.tours/`、`docs/articles/aaaa`（所有者の私物）。
- ノートブックの `TARGET_BRANCH` は、確認用に `"release/v1.3.1"` になっている（公開時にタグへ戻す。T-07）。

## 4. チケット一覧

優先度: **A**＝公開前に必須、**B**＝公開前に望ましい、**C**＝公開後でもよい。

### T-00（A）リポジトリの公開状態の確認

- **事実**: Colab の Step 1 は、認証なしで `git clone https://github.com/akkyey/stock-analyzer-core.git` に成功している（実機で確認）。したがって、**このリポジトリは、現在すでに公開状態である可能性が高い**。
- **確認すること**: GitHub の設定で、リポジトリが Public かどうか。Public であれば、「履歴をリセットして初版として出す」前に、現在の履歴（旧シード、個人のパス、`docs/articles/aaaa`、過去のコミットメッセージ）が、すでに公開されている。強制 push で履歴を置き換えても、**フォーク・キャッシュ・既存の clone には残る**。
- **合否の基準**: 公開状態を確認し、公開済みであれば、①履歴の秘密情報（API キー等）の有無を確認する（`git log -p` に対する gitleaks 等の検査。現在の追跡ファイルの検査では、見つからなかった。T-07）、②履歴リセットの方針（強制 push、または新規リポジトリでの公開）を決める。

### T-01（A）シードの出所未確認の列の確認と処置

- **背景**: シードの一部の列（旧シードから引き継いだ列）は、作り方がリポジトリに無く、出所を確認できない。列名が yfinance の財務項目に対応しており、yfinance の API は個人利用向け（再配布は避けるべき）。詳細: 設計書 `data_sources_and_redistribution_policy.md` §3.2。
- **対象の列**: `operating_cf`・`free_cf`・`payout_ratio`・`payout_status`・`current_ratio`・`quick_ratio`・`debt_equity_ratio`・`sales_status`・`profit_status`・`is_turnaround`・`turnaround_status`・`profit_growth`・`profit_growth_raw`・`sales_growth`（旧値）・`repair_metadata`・`fetch_status`
- **足切り・採点で使われるのは `operating_cf` だけ**（`src/calc/pre_filter.py:409`。営業 CF マージンの足切り。空のときは判定を省く）。それ以外は、レポートの出力列にも含まれない（`Profit_Growth`・`Sales_Growth` を除く）。
- **確認手順**:
  1. 旧シードを作った方（コミット `f4d83b4` は `Irom`、`e087f8d` は `akkyey`）に、列の作り方（出所のデータ源）を確認する。
  2. 同梱している JPX の銘柄一覧（`src/resources/jp_stock_list.csv`）の利用条件も確認する。
  3. `profit_growth_raw` が評価時に再計算されるか確認する（`src/services/financial_repair.py` の 214〜224 行付近。再計算されるなら、シードの値は、使われない）。
- **処置（選択）**:
  - (a) 出所が EDINET と確認できた → そのまま残す。
  - (b) 確認できない（または yfinance 由来） → シードから外す（空にする）。作成済みのシードに対して、次のように、列を空にして書き出し直す（スクリプトにこのオプションは無い）。

    ```python
    import polars as pl
    seed = pl.read_parquet("src/resources/fundamentals_seed.parquet")
    drop = ["operating_cf", "free_cf", "payout_ratio", "payout_status", "current_ratio", "quick_ratio",
            "debt_equity_ratio", "sales_status", "profit_status", "is_turnaround", "turnaround_status",
            "profit_growth", "profit_growth_raw", "repair_metadata"]
    seed.with_columns([pl.lit(None, dtype=seed.schema[c]).alias(c) for c in drop]).write_parquet(
        "src/resources/fundamentals_seed.parquet"
    )
    ```
    列を空にすると、シードの内容が変わり、識別子が変わる（既存 DB への反映が、もう一度、一度だけ行われる）。実行後に、T-04 の検証を、再度行う。
- **合否の基準**: 同梱する列の出所が、設計書 §3 の表で、すべて「確認済み」になっている。

### T-02（A）出典・加工・非公式の明記

- **背景**: EDINET 利用規約は、出典の記載と、加工した旨の記載を求め、「国が作成した未加工のデータであるかのような態様」での公表を禁じている。README・利用ガイドには、記載がない（README は、データ源を「EDINET / yfinance / JPX」と書くのみ）。
- **作業**: 設計書 `data_sources_and_redistribution_policy.md` §4 の 3 の文面を、README と利用ガイド（8 章 免責事項）に追記する。規約原文の URL は、[EDINET 利用規約](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html)のページで確認する。
- **合否の基準**: README と利用ガイドに、出典・加工・「公式のものではない」旨・株価は利用者が Yahoo! Finance から自分の環境で取得する旨、が記載されている。

### T-03（B）個人の PC のパスの除去

- **背景**: 追跡中のファイルに、`/home/irom/...`・`file:///home/irom/...`（個人の PC のパスとユーザー名）がある。
- **対象**（`git grep -cIE "/home/irom|/Users/irom|file:///home" -- . ':!*.parquet'` の結果。件数）:
  - `docs/archive/designs/2026-03-27_repository_decoupling_security.md`（1）
  - `docs/designs/csv_agent_reading_guide.md`（3）
  - `docs/designs/data_validity_and_hybrid_fetch_architecture.md`（2）
  - `docs/designs/pipeline_performance_benchmark_report.md`（7）
  - `docs/reviews/code_and_article_fix_instructions_20260925.md`（2）
  - `scripts/sync_articles.sh`（3）
- **作業**: リポジトリ内のファイルへのリンクは、相対リンクにする。実在しない環境の例のパスは、一般的な例（`/path/to/...`）にする。`scripts/sync_articles.sh` は、公開する必要があるかも判断する。
- **合否の基準**: 上の `git grep` が、0 件になる。

### T-04（A）CLI での検証（実施済み。再現手順と結果）

最終版のシード（識別子 `33d17aaaecea`）での結果（2026-10-06）。再度実施する場合（例: T-01 で列を外した後）の手順も示す。

- **準備**: ①旧 DB（v1.3.1 より前に作った DB。例: Colab の Drive にある `stock_analyzer.duckdb` のコピー）、②新しいシードを置いた作業用のコピー（`git worktree add --detach <dir> HEAD`）、③EDINET の API キー（環境変数 `EDINET_API_KEY`。表示しないこと）。
- **実行**（各シナリオ。作業ディレクトリは、`STOCK_ANALYZER_BASE_DIR` で指定。旧 DB は `<dir>/cache/stock_analyzer.duckdb` に置く）:

  ```bash
  STOCK_ANALYZER_BASE_DIR=<dir> EDINET_API_KEY=*** python -m src scan
  ```

  各回の所要時間は約 7〜8 分（Yahoo の応答による）。レポートは `<dir>/output/daily_report.csv`、DB は `<dir>/cache/stock_analyzer.duckdb`。
- **シナリオと、今回の結果**:

| # | シナリオ | 期待 | 結果 |
| :--- | :--- | :--- | :--- |
| 1 | 旧 DB・反映を切る（`config/config.yaml` に `fetcher:` → `refresh_fundamentals_from_seed: false`） | 更新なし。印も残らない | 合格（レポート 2,089 行） |
| 2 | 旧 DB・反映あり（既定） | 「財務データを、新しい版の同梱データで N 銘柄更新しました」。印に識別子が記録される | 合格（3,744 銘柄更新、2,095 行） |
| 3 | 2 の DB で、もう一度 | 更新されない。レポートが 2 と全項目一致 | 合格（全項目一致） |
| 4 | 空の DB・初回 | 「同梱の財務データから N 銘柄を初期登録」。「更新しました」は出ない。印だけが記録される | 合格（レポート 2,099 行） |
| 5 | 4 の DB で、もう一度 | レポートが 4 と全項目一致 | 合格（全項目一致） |
| 6 | 2 と 4 の比較 | 財務・評価が一致 | 合格（差は市場区分 2 行のみ。空の DB にだけある 4 銘柄は新規上場） |

- **反映（シナリオ 1 → 2）によるレポートの変化**（共通 2,089 行）: 判定が **337 銘柄（16.1%）** で変わる（C→B 150、B→C 93、B→A 56、A→B 28、A→C 7、C→A 2、ほか 1）。スコアの変化は 75.1% が 5 点以内、8.9% が 10 点超。**この変化の大きさを、公開前に了承する**（利用者には、利用ガイドとノートブックで案内済み）。
- **株数の修正の確認**: 同じ決算期の値を持つ旧 DB にも、新しい株数が届く（3443：177,566,022 → 52,422,630、2120：108,915,004 → 134,586,032）。

### T-05（A）シードの確定・再作成（必要な場合のみ）

- **現状**: リポジトリのシードは、EDINET の書類 5,032 件（97 分）から作った最終候補。**再作成は、通常は不要**（T-01 の処置は、列を空にするだけで済む）。
- **再作成が必要な場合**（パーサーの変更後など）:

  ```bash
  git show 9f4ccb1^:src/resources/fundamentals_seed.parquet > /path/to/seed_original.parquet   # 作り直す前の元のシード
  python scripts/build_fundamentals_seed.py --work-dir /path/to/empty_dir --out /path/to/seed_new.parquet \
      --days 400 --base-seed /path/to/seed_original.parquet
  ```

  - **作業ディレクトリは、空にする**（使い回すと、実行ごとに、提出日時が変わる。空でないと、`--reuse` が無い限り実行しない）。EDINET の API キーが必要（`.env` の `EDINET_API_KEY`）。
  - **履歴をリセットすると、`9f4ccb1^` が参照できなくなる**。元のシード（`seed_original.parquet`）は、リセットの前に、リポジトリの外に保存しておく（T-07）。
- **期待する結果**（再作成した場合の目安）: 銘柄数 3,926。空の項目（`eps` 211、`net_profit` 197、`shares_outstanding` 176）。元のシードで値があって空になる項目は、`sales_growth` の 194 件のみ。

### T-06（A）Colab での実機確認

- **前提**: 最終版のシードを含むコミットを、`release/v1.3.1` に push する（T-01 の処置の後）。
- **手順**:
  1. 次の URL のノートブックを開く: `https://colab.research.google.com/github/akkyey/stock-analyzer-core/blob/release/v1.3.1/notebooks/stock_analyzer_colab.ipynb`
  2. 本番の保存先（`StockAnalyzer`）のまま、`reset_database` は OFF にして、Step 0 → Step 1 → Step 3 を実行する。
  3. **期待**: Step 0 に `refresh_fundamentals`（初期値 ON）が出る。Step 1 の最初の行が `branch/tag: release/v1.3.1`。Step 3 で「🔄 財務データを、新しい版の同梱データで N 銘柄更新しました」が出る（本番の DB は、前回の版のシードを反映済みなので、新しいシードで、もう一度反映される）。
  4. 確認用のセル（**`with` で開いて、必ず閉じる形**）で、印を確認する:

     ```python
     import duckdb
     with duckdb.connect('/content/working/cache/stock_analyzer.duckdb', read_only=True) as con:
         print(con.execute("select key, value from app_meta where key like 'fundamentals%' order by key").fetchall())
         print(con.execute("select count(*), count(period_end), count(eps) from fundamentals").fetchone())
     ```

     期待: `fundamentals_seed_refreshed` が、リポジトリのシードの識別子（`33d17aaaecea`、T-01 で列を空にした場合は、その識別子）。出所あり（`period_end`）が約 3,716、`eps` ありが約 3,718。
  5. Step 3 をもう一度実行し、「更新しました」が**出ない**こと（約 20 分かかることがある）。
- **合否の基準**: 3・4 が期待どおり。
- **補足**: 前回（前の版のシードでの）の実機確認は、合格している（更新 3,561 銘柄、印の記録を確認）。

### T-07（A）公開前の整備と、履歴リセットの注意

履歴をリセットして初版として公開する際の確認項目。

1. **非公開の私物を含めない**: `docs/articles/aaaa`（所有者のテスト用）、`.tours/`、`.env`、`.claude/` 等。`git status --ignored` で、追跡・追加の対象に入っていないか確認する。
2. **履歴のリセットで失われるもの**: ①元のシード（`9f4ccb1^`。T-05）、②過去のタグ（`v1.0.0`〜`v1.3.0`。リモートにも公開済み）、③コミットメッセージ内の記録（参照している資料内のコミットのハッシュは、無効になる）。**設計書・報告書に書かれているコミットのハッシュ**（例: `52085d6`）は、リセット後は参照できないため、そのままでよいか判断する（削除するか、「リセット前の記録」と注記する）。
3. **ノートブックと README の参照先**: ノートブックの `TARGET_BRANCH`（現在は検証用の `release/v1.3.1`。初版のタグに変更する。**タグが存在しないと、Step 1 が `main` にフォールバックし、警告を表示する**）、README と利用ガイドの「Open In Colab」バッジのパス（`main` のノートブックを開く）、`REPO_URL`。
4. **バージョン名**: 初版の名前（例: `v1.0.0`）を決める。資料内の「v1.3.0」「v1.3.1」の記述（更新内容・既知の限界・改訂履歴）を、初版としての書き方に直すか決める。
5. **資料の公開範囲**: `docs/reviews/`・`docs/articles/`（Qiita 記事の草稿）・`docs/archive/` は、内部の記録・草稿を含む。公開するか判断する。
6. **機密情報の検査**（履歴全体）: 現在の追跡ファイルは、検査済み（API キー・Webhook・認証ファイルは見つからず。`.env.example` のみ）。**履歴全体**は、未検査（T-00）。
7. **ライセンス**: `LICENSE`（MIT、`Copyright (c) 2026 akkyey`）。同梱データ（財務データ、JPX の一覧）の利用条件は、T-01・T-02 で確認する。

### T-08（C）残課題（公開後でもよい）

| # | 内容 |
| :--- | :--- |
| 1 | 売上高成長率を、EDINET の前期の売上から計算し直す（`sales_growth` が空の銘柄を減らす。パーサーの変更が必要で、シードの再作成を伴う） |
| 2 | 営業 CF など、EDINET の要約表から作れる列を、パーサーで作る（T-01 で列を外した場合の代替） |
| 3 | BPS のずれの残り（半期報告書による純資産・株数の更新で、BPS が年次のまま）。設計書 3.3 の「型 ④」 |
| 4 | 旧シードの値が残る 384 行の、株式分割の二重補正の可能性（配当のみ、4 銘柄） |
| 5 | Qiita 記事 2 本（EDINET 編・株式分割編）のフィードバック反映。公開は、フィードバックを重ねてから |
| 6 | 作成済みのシードから出所未確認の列を外すオプション（`--drop-columns`）を、スクリプトに追加する |

## 5. 今回の対応のうち、これまで資料に書いていなかったもの（本票で記録）

| 内容 | 記録先 |
| :--- | :--- |
| 発行済株式数を提出日現在の値で読む（パーサー版 3）、BPS・株数のずれの原因分類 | 設計書 `data_validity_and_hybrid_fetch_architecture.md` 3.3 |
| 1 株当たりの値のガード（株数 1,000 株未満、純資産 / (株数 × BPS) が 3 桁以上ずれる） | 同上 |
| 既存 DB への反映の規則（同じ決算期でも、シードの提出日時が DB 以降なら反映） | 同上 |
| シード作成スクリプトの列の組み立て（`assemble_seed`、`--base-seed`、列の重複の修正） | 同上 |
| 取得できない銘柄の失敗の数え方（1 日に 1 回、`stocks.last_fail_date`） | 同上 |
| DuckDB の接続残りの案内、「反映 0 件」の非表示、「シード」の文言の統一 | 設計書 `message_display_improvement_design.md` 2.4 |
| データの出所・利用条件、列ごとの出所、再配布方針 | 設計書 `data_sources_and_redistribution_policy.md` |
| 利用者への周知（判定が約 16% で変わる）と、更新を止める方法 | 利用ガイド（Step 3 の後）、ノートブック Step 0 |
| 個人のテスト用ファイルの誤コミットと是正、公開状態・個人パスの確認 | 本票 §3、T-00、T-03、T-07 |
| 最終版のシードでの CLI 検証 5 回の結果 | 本票 T-04、設計書 3.3 |

## 6. 作業ログ（この票の起票時点までの誤りと是正）

詳細は、報告書 §6・§8 を参照。本票の起票にかかわるもの:

| # | 誤り | 是正 |
| :--- | :--- | :--- |
| 1 | `git add src tests docs` で、所有者の私物 `docs/articles/aaaa` を、コミット（`7c146ba`、push 済み）に含めた | 追跡から外すコミット `c99b1d7`（ローカル）。以後は、ファイルを明示して追加 |
| 2 | シード作成スクリプトが、作り直し済みのシードを土台にすると、出所の列が重複して落ちた（97 分の取得の直後） | `assemble_seed` に分けて修正し、テストを追加。取得結果は残っていたため、書き出しのやり直しは 1 分 |
| 3 | 提出日現在の株数に変えた後、543A の 1 株当たりの値が、ガードをすり抜けた | 株数との整合を見るガードを追加 |
| 4 | 出所の確認が、旧シードの引き継ぎ列に及んでいなかった（所有者の指摘で判明） | 本票 T-01、設計書 `data_sources_and_redistribution_policy.md` |

## 7. 判定欄（所有者が記入）

| チケット | 判定（合格／不合格／保留） | 日付 | メモ |
| :--- | :--- | :--- | :--- |
| T-00 | 保留（方針決定） | 2026-10-06 | 履歴リセットは有料版公開時に再考。v1.3.1 は通常リリースとして進める |
| T-01 | 合格（処置 b 完了） | 2026-10-06 | 出所未確認の 15 列を None に上書きして再出力。新識別子 `ef0293e94ba3` を生成・確認 |
| T-02 | 合格 | 2026-10-06 | README および利用ガイド（8章）に EDINET 出典（PDL1.0）、加工、非公式性、株価データの利用者取得を明記 |
| T-03 | 合格 | 2026-10-06 | 該当 6 ファイル（+報告書・スクリプト）の個人パスを除去・相対リンク化。grep 0 件達成 |
| T-04 | 合格 | 2026-10-06 | 新シード（`ef0293e94ba3`）にて初期登録・反映・冪等性の正常動作を確認 |
| T-05 | 合格 | 2026-10-06 | シード原本を退避済み。再作成不要で処置 (b) の適用完了 |
| T-06 | 作業待ち | 2026-10-06 | 新シードコミット push 後に Colab 実機で確認予定 |
| T-07 | 保留（方針決定） | 2026-10-06 | 履歴リセットは有料版公開時に再考。私物除外・TARGET_BRANCH 確認を順次実施 |
| T-08 | 保留 | 2026-10-06 | 公開後タスクとして維持 |

