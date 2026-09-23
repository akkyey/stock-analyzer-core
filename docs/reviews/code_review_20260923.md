# コードレビュー指摘事項 - 2026-09-23

## 概要
`main`（`f262215`）時点の `src/` 全体を対象に、不具合、性能、品質、構成の観点で見つかった問題点をまとめる。

処理の見直しで AI（LLM）分析を廃止したが、コード・設定・依存パッケージ・テストに残骸が多く残っている。あわせて、見直し後の新しい処理（`PreFilter`、`QuantAgentEvaluator`）が本番のパイプラインに組み込まれていない。そのため本書では、**残骸を削除し、パイプラインを一本化する整理（D章）を最優先**とし、その上で不具合・性能・品質の各指摘に対応する方針とする。

旧来の複数戦略による採点（`ScoringEngine` / `PolarsEngine` / `src/calc/strategies/`）は、**アーカイブに移すことが決まっている**（指摘18）。本書の指摘のうち旧採点だけに関わるものは、アーカイブによって解消する扱いとする。

**確認方法**
- コードを読んだうえで、主要な指摘は小さな入力で実際に動かして挙動を確かめた（「再現済み」と記載したもの）。
- 呼び出し元の有無は、`src/`、`tests/`、`scratch/` を対象に grep で確認した。
- テストは133件すべて成功。ruff、mypy もエラーなし。カバレッジは 61%。
- 実データを使った全銘柄の性能計測は行っていない。性能の項目は計算量の見積もりである。

**優先度の目安**
- 🔴 結果が誤る
- 🟠 性能・信頼性・保守性への影響が大きい
- 🟡 改善推奨

---

# A. 不具合・未実装

## 1. 🟡 [Bug] README のデモ手順が clone 直後の環境で実行できない
- **現象**: README「4. パイプライン実行」の `python3 scratch/run_full_universe_pipeline.py` が、clone 直後の環境ではファイルが存在せず失敗する。
- **根本原因**: 直前のコミット `f262215`（chore: remove scratch/ directory from git tracking）で `scratch/` を git の管理から外したが、README の案内が更新されていない。
- **対応案**: 指摘16でパイプラインを `src/` に一本化し、README の手順をその入口に差し替える。

## 2. 🟡 [Bug] 「連続グラデーション配点」なのにスコアが途中で飛ぶ
README と `QuantAgentEvaluator` の docstring では「連続グラデーション（リニア傾斜配点）」をうたっているが、次の区間境界で点数が不連続に変化する。

| 指標 | 箇所 | 境界の手前 | 境界の先 | 差 |
|---|---|---|---|---|
| 25日移動平均乖離率 | `src/calc/quant_evaluator.py` `_score_ma_div` | -20.0% ちょうどで 5.0点 | -20.0% を少しでも下回ると 約0点 | 約 -5.0点 |
| 25日移動平均乖離率 | 同上 | +20.0% ちょうどで 2.0点 | +20.0% を超えると -4.0点 | -6.0点 |
| RSI | `src/calc/quant_evaluator.py` `_score_rsi` | 75 の手前で 約0点 | 75 ちょうどで -3.0点 | -3.0点 |

- **影響**: 指標がわずかに変わるだけで総合スコアが最大6点動き、判定（BUY / WATCH など）の境界をまたぐおそれがある。特に乖離率 -20% 付近は、減点の手前が満点（5.0点）の区間になっている。
- **対応案**: 意図したペナルティならその旨をコメントで明記する。意図していないなら、境界をまたいで線形につながるように区間の式を直す。

## 3. 🟡 [Cleanup] `QuantAgentEvaluator.evaluate()` の thesis / risks は AI 時代の名残
- **現象**: 戻り値 `(score, verdict, thesis, risks)` のうち、`thesis` は常に `""`、`risks` は常に `[]`。モジュールの docstring には「Thesis、およびリスク要因を算出する」とある。
- **背景**: 投資判断の根拠（thesis）とリスク要因は、LLM が生成していた項目（`AIAgent.analyze_dossier` の `investment_thesis`、`risk_factors`）である。AI を廃止した以上、埋める予定のない戻り値になっている。
- **対応案**: 戻り値を `(score, verdict)` に縮め、docstring とテスト（`tests/test_quant_evaluator.py` の4要素アンパック）を合わせて修正する。クラス名から「Agent」を外す件は指摘17で扱う。

## 4. 🟡 [Refactor] パイプラインの実行クラスが2つあり、テストされているのは使われていない方
- **現象**: 同じ「取得 → 評価 → 統合」の3段階を実行するクラスが2つある。

| クラス | 本番コードからの利用 | テスト | カバレッジ |
|---|---|---|---|
| `src/orchestration/pipeline.py` の `OrchestrationPipeline` | `ScanHandler` から呼ばれている（本番の入口） | `test_scan_handler.py` でモック化されているだけ | 35% |
| `src/orchestration/pipeline_orchestrator.py` の `PipelineOrchestrator` | どこからも呼ばれていない | `tests/orchestration/test_pipeline_orchestrator.py`、`tests/test_orchestration_phases.py` | 90% |

- **`PipelineOrchestrator` 側の問題**:
  - 例外をログに出すだけで止めてしまい、呼び出し側に失敗が伝わらない。
  - `_run_integration()` の中身が `pass` だけで、呼ばれてもいない。
- **影響**: 本番の入口である `OrchestrationPipeline` のフェーズ連結（各フェーズの戻り値を次へ渡す部分）が、実質的にテストされていない。
- **対応案**: `PipelineOrchestrator` とそのテストを削除し、テストの対象を `OrchestrationPipeline` に移す（指摘16の一本化と同時に行う）。

## 5. 🔴 [Bug] 評価フェーズが全期間の行を採点し、上位50件に過去日付の行と重複銘柄が混ざる（再現済み）
- **現象**: 評価フェーズの出力（上位50件、そのままレポートに渡る）が「最新日の上位50銘柄」になっていない。何か月も前の日付でスコアが高かった行や、同じ銘柄の複数日付の行が選ばれる。
- **根本原因**:
  - `src/orchestration/phases/evaluation.py:96` で `calc_batch_technicals_vectorized(..., latest_only=False)` を呼ぶため、全銘柄の全履歴（最大約1年分）がそのまま採点に進む。
  - `_aggregate_winners` は `(code, entry_date)` ごとに勝者の戦略を選ぶだけで、日付を最新に絞らない。
  - 直後の `sort("quant_score").head(50)`（`evaluation.py:63`）も、全日付をまたいで上位を取る。
- **再現**: 銘柄 A（1月: 90点、9月: 50点）と B（1月: 80点、9月: 70点）を `_aggregate_winners` → `sort → head` に通すと、`A 1月 / B 1月 / B 9月 / A 9月` の順になった。最新日の順位（B > A）とは逆で、B が2行入る。
- **付随する問題**: `PolarsEngine.calculate_scores` の業種内順位（`rank().over("sector")`）も全日付をまたいで計算されるため、順位の意味が崩れる（旧採点はアーカイブ予定のため、この部分は修正不要）。
- **影響**: レポート（CSV / Google Sheets / Discord 通知）の内容が古いデータに基づくおそれがある。
- **対応案**:
  - 旧採点（`_run_multi_strategy_scoring`、`_aggregate_winners`）はアーカイブで評価フェーズから外れる（指摘18）。
  - ただし、テクニカル指標を全履歴で計算する構造は残るため、一本化後のパイプライン（指摘16）でも、テクニカル指標の計算の後、`PreFilter` の前に `code` ごとの最新 `entry_date` の行だけに絞る処理が必要である。
  - 出力で `code` が重複していないことを検証する処理を入れる。

## 6. 🔴 [Bug] 財務データ修復の比率スケーリングで、自己資本比率5%が500%になる（再現済み）
- **箇所**: `src/services/financial_repair.py` の Step 5（クリッピング）と Step 6（比率の自動スケーリング）。
- **根本原因**:
  - Step 6 は「絶対値が10未満なら小数表記とみなして100倍する」というヒューリスティックで、正しくパーセント表記された 10% 未満の値も100倍してしまう。
  - さらに `equity_ratio` の 0〜100 へのクリップが Step 5（スケーリングの**前**）にあるため、変換後の値が上限で止まらない。
- **再現**: `equity_ratio = [5.0, 45.0, 0.45, 8.0]` を入力すると `[500.0, 45.0, 45.0, 800.0]` になる。
- **影響**:
  - 自己資本比率が低い銘柄（銀行など、5% 前後が普通の業種）が、自己資本比率の配点で満点（`QuantAgentEvaluator` では 15点）を得る。つまり財務が弱い銘柄ほど高く評価される方向に誤る。
  - `PreFilter` の債務超過判定（自己資本比率 <= 0%）には影響しないが、`current_ratio`、`quick_ratio`、`debt_equity_ratio` も同じ判定の対象になっている。
- **冪等性の問題**: 同じデータに2回適用すると 0.05 → 5.0 → 500.0 になる（再現済み）。修復済みの値を DB に保存し、次の実行で再び修復する経路があると、値が増え続けるおそれがある。
- **対応案**:
  - 単位はヒューリスティックで推測せず、データ取得元（EDINET パーサー、yfinance）ごとに確定させる。
  - 暫定対応としては、クリップをスケーリングの後に移し、10% 未満の値を正常値として扱える判定（例: 1.0 未満のみ100倍）に改める。

## 7. 🟠 [Bug] 取得フェーズで DB の履歴と新しく取得したデータを結合する処理が不正確
- **箇所**: `src/orchestration/phases/acquisition.py`、`src/repositories/market_data_repository.py:88`
- **問題点**:
  1. **単位の混在**: `get_all_history_pl` は `trading_value as Volume`（売買代金、円）を `Volume` として返す。一方、yfinance から取得した行の `Volume` は出来高（株数）である。同じ列に単位の異なる値が混ざる（下流の影響範囲は要確認。`PreFilter` の流動性判定が売買代金を使うため、組み込み後は影響が出る可能性が高い）。
  2. **どちらの行を残すか不定**: 結合後の `.unique("Date")`（`acquisition.py:123`）は `keep` を指定していないため、同じ日付で DB の行と新しく取得した行のどちらが残るか保証されない。当日の値を更新したい場合は `keep="last"` と順序の保証が必要。
  3. **タイムアウトで異常終了**: 取得結果を受け取る側の `result_queue.get(timeout=120)`（`acquisition.py:140`）は、取得側の1バッチが120秒を超えると `queue.Empty` を送出する。これを捕捉していないため、パイプライン全体が異常終了する。yfinance の遅延やレート制限で起こり得る。

## 8. 🟠 [Quality] 例外を握りつぶして「正常に見える誤った結果」を返す
- `src/fetcher/polars_processor.py:275`（`calc_from_polars`）、`:129`（`pad_calendar`）: 例外が起きると、空の DataFrame や加工前のデータを返す。評価フェーズには「Input data is empty」とだけ記録され、本当の原因が見えなくなる。
- （参考）`src/calc/engine.py:165` の `ScoringEngine.calculate_score` も、例外が起きると全銘柄0点のデータを返して失敗を隠していた。これは旧採点のアーカイブ（指摘18）で解消する。
- **対応案**:
  - テクニカル指標の計算が失敗した場合や、入力が空になった場合は、例外として呼び出し側に伝える。
  - 一本化後のパイプラインの `PreFilter` / `QuantAgentEvaluator` では、同じ書き方（例外を握りつぶして既定値を返す）をしない。

---

# B. 性能改善

## 9. 🟠 [Perf] 毎回の実行で全履歴を採点・保存している
- 指摘5の根本原因と同じ。全約4,000銘柄 × 最大約245営業日（約100万行）を対象に、次の処理を毎回行っている。
  - 財務データの修復（`FinancialRepairService`）
  - DuckDB への一括 UPSERT（`save_metrics`、`evaluation.py:42`）
  - 戦略の数だけの採点（`PolarsEngine`）と勝者の集約 → **旧採点のアーカイブ（指摘18）で解消する**
- 本当に全履歴が必要なのは、テクニカル指標（移動平均、RSI、MACD）の計算だけである。
- **対応案**:
  - 財務データの修復、`PreFilter`、`QuantAgentEvaluator` は、最新日の約4,000行だけに行う（処理量はおよそ 1/245 になる見込み）。
  - `save_metrics` も、新規・更新された日付の行だけに限定する。

## 10. 🟠 [Perf] 取得フェーズで、銘柄ごとに全履歴を検索している
- `acquisition.py:101` の `df_db_hist_all.filter(pl.col("code") == code)` を、銘柄ごと（約4,000回）に実行している。そのたびに全銘柄の3か月分の履歴（約25万行）をすべて調べるため、合計でおよそ10億行の比較になる。
- **対応案**: ループの前に `df_db_hist_all.partition_by("code", as_dict=True)` で一度だけ銘柄ごとに分割し、辞書から取り出す。

## 11. 🟠 [Perf] 並列化のつもりの `ThreadPoolExecutor` が使われておらず、取得が直列で動いている
- `acquisition.py:84` で `ThreadPoolExecutor(max_workers=2)` を作っているが、`submit` / `map` を一度も呼んでいない。約200バッチ（20銘柄ずつ）が1バッチずつ直列に取得されている。
- **対応案**:
  - 実際にバッチを executor に投入する。
  - もしくは、`yf.download(threads=True)` の内部並列に任せる方針なら、executor を削除して意図をコメントに残す。
  - 並列度はレート制限を踏まえて設定ファイルから指定できるようにする。

## 12. 🟡 [Perf] Polars と pandas の間で変換を繰り返している
- データの流れは次のとおりで、途中で形式が何度も変わる。
  1. 取得側: yfinance（pandas）→ Polars に変換して DB の履歴と結合 → `to_pandas()` で pandas に戻す（`acquisition.py:126`）
  2. 評価フェーズ: `calc_batch_technicals_vectorized` で、銘柄ごとに `pl.from_pandas()` で Polars に戻す → 日付の型変換を銘柄ごとに実行 → 最後に `concat`
- 約4,000銘柄それぞれで変換と、Python ループの中での `with_columns` が発生している。
- **対応案**:
  - 取得フェーズの出力を Polars に統一し、銘柄ごとの DataFrame ではなく、`code` 列を持つ1つの長い DataFrame で受け渡す。
  - 日付の型変換は `concat` の後に一度だけ行う。

---

# C. 品質強化

## 13. 🟡 [Test] 本番の主要経路のテストカバレッジが低い
- 全体は 61% だが、本番で使われる経路ほど低い。

| ファイル | カバレッジ | 備考 |
|---|---|---|
| `src/orchestration/pipeline.py` | 35% | 本番の入口（指摘4） |
| `src/fetcher/facade.py` | 14% | データ取得の窓口 |
| `src/fetcher/jpx.py` | 9% | 銘柄マスタ |
| `src/fetcher/market_fetcher.py` | 18% | 株価取得 |
| `src/fetcher/xbrl_parser.py` | 18% | EDINET 財務の正本 |
| `src/validation_engine.py` | 38% | |

- `src/orchestration/report_helper.py`（9%）の大半は指摘17で削除対象になるため、表から除外した。
- 全体のカバレッジは、指摘17の削除（`src/ai/` 約1,000行など）によって上がる見込みだが、それは見かけ上の改善であり、上表の経路のテストは別途必要である。
- 指摘2、5、6は、いずれも既存テストをすり抜けている。
- **対応案**（優先順）:
  1. `QuantAgentEvaluator` の各 `_score_*` 関数に、区間の境界値（±ε）の表形式テストを追加する。
  2. `FinancialRepairService` に、単位の境界（5%、10%、0.05）と冪等性（2回適用しても同じ値になること）のテストを追加する。
  3. 一本化したパイプライン（指摘16）に、「複数日付の入力で、出力が最新日だけかつ `code` が重複しないこと」のテストを追加する。
  4. `xbrl_parser` に、実際の XBRL の断片を使ったテストデータ（fixture）によるテストを追加する。

## 14. 🟡 [Quality] 静的解析の設定が緩い
- mypy は「エラーなし」だが、型注釈のない関数の中身は検査されていない（`config_singleton.py` などで `annotation-unchecked` の note が出ている）。
- 実在しないモジュールの import が見逃されている（指摘17の `sentinel_repository`、`rank_history_repository`）。関数の中での遅延 import のため、ruff も mypy も検知していない。
- **対応案**: `pyproject.toml` の `[tool.mypy]` に `check_untyped_defs = true` を加え、段階的に型を付けていく。ruff には `UP`（pyupgrade）、`SIM`、`BLE`（`except Exception` の乱用検知、指摘8の対策）の追加を検討する。未使用コードの検出には `vulture` の導入も有効である。

## 15. 🟡 [Quality] Polars 2.0 で動かなくなる非推奨 API を使っている
- `src/orchestration/phases/evaluation.py:56` の `pl.col("entry_date").cast(pl.Date)`（String から Date への cast）は、テスト実行時に DeprecationWarning が出ている。
- **対応案**: `str.to_date()` に置き換える。CI で `-W error::DeprecationWarning` を有効にして、同種の API が残っていないか検知する。

---

# D. 構成の整理（AI 残骸の削除、旧採点のアーカイブ、パイプラインの一本化）

## 16. 🟠 [Architecture] 新しい処理が本番のパイプラインに組み込まれていない
- **現状**: README が説明する3層構成（事前足切り → クオンツ評価 → 2ファイルの CSV 出力）のうち、次の2つは `src/` の本番パイプライン（`ScanHandler` → `OrchestrationPipeline`）から一度も呼ばれていない。
  - `PreFilter`（`src/calc/pre_filter.py`）
  - `QuantAgentEvaluator`（`src/calc/quant_evaluator.py`）
- **呼び出し元**: `scratch/` のスクリプトだけ（`run_full_universe_pipeline.py`、`benchmark_*.py`、`rebuild_and_benchmark_full_universe.py`、`verify_live_e2e.py`）。`scratch/` は git の管理外なので、リポジトリだけでは新しい処理を実行できない。
- **本番パイプラインの現状の流れ**: 取得 → テクニカル指標の計算（全履歴）→ 財務データの修復 → 戦略ごとの採点（`PolarsEngine`）→ 上位50件 → AI 用カルテの作成（使われない）→ レポート出力。旧来の構成のままである。
- **対応案**: `scratch/run_full_universe_pipeline.py` の処理を `src/` のフェーズとして取り込み、入口を一本化する。想定する流れは次のとおり。
  1. **取得フェーズ**: 変更なし（指摘7、10、11、12を反映）。
  2. **評価フェーズ**:
     1. テクニカル指標の計算（全履歴）
     2. 最新日への絞り込み（指摘5）
     3. 財務データの修復（指摘6を修正したうえで）
     4. `PreFilter`（除外した銘柄は理由付きで保持）
     5. `QuantAgentEvaluator` による採点と判定
  3. **統合フェーズ**: `daily_report.csv` と `uncalculable_stocks.csv` を出力し、Google Drive / Sheets / Discord に連携する。
- **決定事項**: 旧来の複数戦略採点はアーカイブに移し、最終スコアは `QuantAgentEvaluator` に一本化する（指摘18）。

## 17. 🟠 [Cleanup] AI（LLM）分析の残骸を削除する
AI 分析は処理の見直しで廃止したが、次のものが残っている。いずれも本番パイプラインからは使われていない。

**削除するファイル**

| 対象 | 内容 |
|---|---|
| `src/ai/`（`agent.py`、`prompt_builder.py`、`key_manager.py`、`response_parser.py`、`__init__.py`） | AI 分析の本体（約1,000行）。`src/ai/` 以外の `src/` から import されていない |
| `src/orchestration/dossier_builder.py` | AI に渡すカルテ（StockDossier）の作成。作ったカルテを読む処理がない |
| `src/repositories/analysis_repository.py` | AI の分析結果（`analysis_results` テーブル）の保存と、キャッシュの読み込み |
| `src/validation_engine.py` | 業種ポリシーによる検証（`score_value` などのカテゴリ別スコアと AI プロンプトの除外設定を扱う）。本番コードからの呼び出し元は `src/ai/agent.py` だけ |
| `src/orchestration/pipeline_orchestrator.py` | 指摘4 |
| `config/ai_prompts.yaml` | プロンプトのテンプレート |
| `tests/test_ai_components.py`、`tests/ai/`、`tests/test_e2e_agentic_pipeline.py`、`tests/test_dossier_builder.py`、`tests/test_analysis_repository.py`、`tests/orchestration/test_pipeline_orchestrator.py` | 上記のテスト。`tests/test_repositories.py`、`tests/test_orchestration_phases.py`、`tests/orchestration/test_evaluation_phase.py`、`tests/test_reporter_and_validation.py`（`ValidationEngine` の部分）は該当部分だけを削る |

**修正する箇所（コード）**

| 対象 | 削除・修正する内容 |
|---|---|
| `src/orchestration/phases/evaluation.py:66-69` | カルテの作成と `context.stock_dossiers` への格納 |
| `src/orchestration/context.py` | `no_ai` 引数と属性、`stock_dossiers`、`analysis_repo`、`execute_equity_auditor`、`get_auditor_path`（存在しない `equity_auditor.py` を指す）、`execute_ingest_repair`（同じく auditor を呼ぶ。呼び出し元なし）、`get_analysis_stats` / `get_session_stats` |
| `src/orchestration/context.py` の `sentinel_repo`、`rank_repo` | 存在しないモジュール（`src.repositories.sentinel_repository`、`rank_history_repository`）を import しており、呼ばれると ImportError になる。呼び出し元もない |
| `src/orchestration/context.py` の `print_session_summary` | 「Analyzed: N records」「Unique Stocks」は `analysis_results`（AI 時代の分析結果）の件数で、現在の処理と無関係な値を表示している。評価件数・除外件数などの、実際の処理の統計に差し替える |
| `src/orchestration/report_helper.py` | 使われているのは `_upload_summary_to_gspread` だけ。`export_reports` をはじめとする、`analysis_results` を読んでレポートを組み立てる関数群は呼び出し元がない。`_upload_summary_to_gspread` だけを `integration.py` か `colab_tools.py` へ移し、ファイルは削除する |
| `src/repositories/duck_repository.py` | `analysis_results` テーブルの作成と `save_analysis_results`。既存の DB ファイルからテーブルを削除するかどうかは、運用側で判断する |
| `src/repositories/__init__.py` | `AnalysisRepository` の export |
| `src/config_schema.py` | `AIConfig`、`ConfigModel.ai`、`APISettingsConfig.gemini_tier`、`SectorPolicy.ai_prompt_excludes` |
| `src/constants.py:12` | `AI_PROMPTS_PATH` |
| `src/env_loader.py:74` | `GEMINI_API_KEY` |

**修正する箇所（設定・依存・文書）**

| 対象 | 削除・修正する内容 |
|---|---|
| `config/config.yaml`、`config/defaults.yaml`、`config/test_config.yaml` | `ai:` セクション、`gemini_tier`、業種ごとの `ai_prompt_excludes`（`max_ai_threads` は YAML にはなく、`context.py` の既定値だけ） |
| `requirements.txt` | `google-genai`、`tenacity`（AI のリトライでしか使っていない）。`google-api-python-client` は Google Drive 連携で使うため残す |
| `README.md` | 「クオンツ × AI エージェント協調型」「Stage 2: エージェント層 (QuantAgentEvaluator / LLM)」などの記述。3層構成の図を実際の処理に合わせる |
| `QuantAgentEvaluator` という名前 | 「Agent」を外し、`QuantEvaluator` などに改名する（モジュールの docstring の「AIエージェント評価」も） |
| `docs/articles/`、`docs/designs/` | AI エージェントに触れている記述（`qiita_part2_quant_gatekeeper.md`、`pipeline_performance_benchmark_report.md` など）。公開済みの記事は、履歴として残すか追記で補足するかを判断する |
| `scratch/demo_live_pipeline.py` | `PromptBuilder` を import しているため、削除後は動かなくなる（git の管理外） |

**作業時の注意**
- `ConfigModel.ai` は**必須項目**である。`src/config_schema.py` と設定の YAML は、同じコミットで同時に修正すること。片方だけ消すと、設定の検証で起動に失敗する。
- 削除後に `pytest`、`ruff`、`mypy` に加えて、`grep -rniE "src\.ai|gemini|dossier|analysis_results|ai_prompt|no_ai"` で参照が残っていないことを確認する。

## 18. 🟠 [Cleanup] 旧来の複数戦略による採点をアーカイブに移す
最終スコアを `QuantAgentEvaluator` に一本化するため、旧来の複数戦略採点をアーカイブに移す（決定事項）。

**アーカイブに移すファイル**

| 対象 | 内容 |
|---|---|
| `src/calc/engine.py` | `ScoringEngine`（戦略の登録、採点の振り分け、`filter_and_rank`） |
| `src/calc/engines/polars_engine.py` | `PolarsEngine`（設定駆動の5層採点） |
| `src/calc/strategies/`（`base.py`、`generic.py`、`__init__.py`） | 戦略クラス |
| `src/calc/base.py`、`src/calc/__init__.py` の `Calculator` | 旧採点の基底クラスと窓口。`src/calc/__init__.py` は空にするか、`PreFilter` / `QuantAgentEvaluator` の export だけにする |
| `config/thresholds.yaml` | `PolarsEngine` の閾値（ほかの利用元は削除予定の `src/ai/prompt_builder.py` だけ） |
| `tests/test_calc_engines.py`、`tests/calc/test_polars_engine.py`、`tests/test_schema_cleansing.py` | 旧採点のテスト |

**修正する箇所（コード）**

| 対象 | 削除・修正する内容 |
|---|---|
| `src/orchestration/phases/evaluation.py` | `_run_multi_strategy_scoring`、`_aggregate_winners`、上位50件の選定。指摘16の流れ（最新日への絞り込み → 修復 → `PreFilter` → `QuantAgentEvaluator`）に置き換える |
| `src/constants.py` | `METRIC_CATEGORY`、`LOWER_IS_BETTER_DEFAULTS`、`THRESHOLDS_PATH`（旧採点だけが使う） |
| `src/config_schema.py` | `StrategyConfig`、`MetricMetadata`、`ScoringConfig`、`ScoringV2Config`、`ConfigModel` の `current_strategy`、`use_polars`、`scoring`、`scoring_v2`、`strategies`、`FilterConfig.min_quant_score` |
| `src/config_loader.py` の `_sync_macro_context` と `config/market_context.txt` | 市況（強気/弱気、金利、注目業種）を `scoring_v2.macro` に書き込んでいるが、**現状でもこの値を読むコードがない**。旧採点と一緒にアーカイブする |
| `src/reporter.py`（222〜224、273〜274 行目付近） | `strategy_name`、`score_value`、`score_growth` など、旧採点が出力する列を前提にしている。`QuantAgentEvaluator` の出力（`score`、`verdict`）に合わせて直す |
| `src/fetcher/polars_processor.py` の `SSOT_SCHEMA`、`src/repositories/duck_repository.py` の `daily_metrics.quant_score` 列 | 旧採点のスコアを保存する列。新しいスコアを保存するなら列の意味を改め、保存しないなら削除する |
| `tests/helpers/stubs.py`、`tests/orchestration/test_evaluation_phase.py`、`tests/test_scan_handler.py` | 旧採点を前提にしたスタブとテストを、新しい流れに合わせて書き直す |

**修正する箇所（設定）**

| 対象 | 削除・修正する内容 |
|---|---|
| `config/config.yaml`、`config/defaults.yaml`、`config/test_config.yaml` | `current_strategy`、`use_polars`、`strategies:`、`scoring:`、`scoring_v2:`、`metric_metadata_defaults:`、`filter.min_quant_score` |
| `hard_filters:` | **残す**。`PreFilter`（`src/calc/pre_filter.py:187`）が使っている |

**アーカイブで同時に解消する既存の問題**
- `config/config.yaml` にトップレベルの `scoring:` が2回書かれている（75行目と510行目）。YAML では後の方が優先されるため、75行目の `lower_is_better`（PER・PBR などを「低いほど良い」とする指定）は読み込まれていない（読み込んで確認済み。`scoring` は `{'min_coverage_pct': 50}` だけになる）。コードの既定値 `LOWER_IS_BETTER_DEFAULTS` で動いていたため、表面化していなかった。
- 指摘8の `ScoringEngine` の例外の握りつぶし、指摘9の戦略の数だけの採点。

**アーカイブの置き場所**
- リポジトリ内に置く場合は、ルートに `archive/legacy_scoring/` などを作り、次の設定から除外する。
  - pytest: `norecursedirs` には `archive` が登録済み
  - ruff / mypy: `exclude` に追加が必要（現在は `stock-analyzer4` だけ）
  - カバレッジの計測対象（`--cov=src`）: `src/` の外に置けば対象外になる
- 現在の `src/` の import パス（`src.calc.engine` など）のままでは動かなくなる。アーカイブ後に動かす必要がないなら、それで問題ない。動かす必要があるなら、アーカイブ直前のコミットに git のタグ（例: `legacy-scoring-final`）を付けておく方が、コードをそのまま復元できて確実である。

**作業時の注意**
- `ConfigModel` の `current_strategy`、`scoring`、`strategies` は**必須項目**である。指摘17の `ai` と同じく、`src/config_schema.py` と設定の YAML は同じコミットで修正すること。
- 移した後に `grep -rnE "ScoringEngine|PolarsEngine|GenericStrategy|BaseCalculator|METRIC_CATEGORY|scoring_v2|current_strategy|strategy_name|score_value|score_growth" src tests` で、参照が残っていないことを確認する。

---

## 今後の対応案 (Next Steps)
優先度順。整理を先に行い、その上で不具合を直す。整理の前に不具合を直すと、削除するコードまで修正することになるためである。

1. 🟠 **整理**: 次の3つを行う。設定のスキーマと YAML は、1と2をまとめて1回で直すと手戻りが少ない。
   - AI 残骸の削除（指摘17）
   - 旧採点のアーカイブ（指摘18）
   - `PipelineOrchestrator` と thesis / risks の削除（指摘4、3）
2. 🟠 **一本化**: 新しい処理を `src/` のパイプラインに組み込み（指摘16）、README の手順を更新する（指摘1）。
3. 🔴 **結果を誤らせる不具合の修正**: 一本化の中で、最新日への絞り込み（指摘5）と比率スケーリング（指摘6）を修正する。同時にテスト（指摘13の2、3）を追加する。
4. 🟠 **性能と信頼性の改善**: 指摘9〜12を適用し、取得処理の結合（指摘7）と例外の扱い（指摘8）を直す。指摘9は、手順1〜3でほぼ解消する。
5. 🟡 **仕上げ**: 配点の不連続を直し（指摘2）、テストと静的解析を強化する（指摘13〜15）。
