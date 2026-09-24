# コードレビュー指摘事項（再レビュー） - 2026-09-23

## 概要
[code_review_20260923.md](code_review_20260923.md)（以下「前回レビュー」）の指摘を反映した修正を確認した結果をまとめる。

**対象**
- ブランチ `docs/code-review-20260923`（`e93adeb`）に対する、未コミットの作業ツリーの変更（59ファイル、+402 / -4,569行）。

**確認方法**
- テストは92件すべて成功。ruff、mypy もエラーなし。カバレッジは 63%。
- 前回レビューの grep（AI 残骸と旧採点の参照が残っていないか）を再実行した。
- 主要な指摘は、小さな入力で実際に動かして挙動を確かめた（「再現済み」と記載したもの）。

**結論**
- AI 残骸の削除と旧採点のアーカイブは、ほぼ前回レビューどおりに完了している。
- しかし、`scratch/` から `src/` のパイプラインへ移植する際に、**テクニカル指標の列名の読み替えと、PreFilter への流動性データの受け渡しが抜け落ちた**。そのため、スコアと足切りが正しく計算されていない（指摘1、2）。
- README に載せた実行手順は、実際には何も起動しない（指摘3）。

**優先度の目安**（前回レビューと同じ）
- 🔴 結果が誤る
- 🟠 性能・信頼性・保守性への影響が大きい
- 🟡 改善推奨

---

# A. 新たに見つかった問題

## 1. 🔴 [Bug] テクニカル指標の列名が合っておらず、採点に使われていない（再現済み）
- **箇所**: `src/orchestration/phases/evaluation.py:105-107`（`_apply_quant_evaluator`）
- **現象**: 評価フェーズは `row.get("ma25_divergence")` と `row.get("macd_status", "")` を読んでいる。しかし、テクニカル指標の計算（`PolarsProcessor.SSOT_SCHEMA`）が出している列は `ma_divergence` で、`macd_status` を作る処理は `src/` のどこにもない。どちらの値も常に `None` / `""` になる。
- **影響**:
  - 乖離率の配点（最大5点）が、全銘柄で0点になる。
  - 死に株の判定（`_is_dead_stock`）は乖離率が `None` だと成立しないため、働かない。
  - MACD の配点は、`macd_status` が空だと `macd_hist` の値に関係なく常に3.0点（中立）になる。
  - ゲートキーパー3（MACD Bearish での STRONG_BUY 禁止、乖離率 -10% 未満での最大 WATCH）が一切働かない。
- **再現**: 120営業日にわたって 2,000円 → 1,200円 に下落する銘柄（ROE 10%）で試した。

| 経路 | スコア | 判定 |
|---|---|---|
| 現在のパイプライン（`_apply_quant_evaluator`） | 25.0 | PASS |
| 列名を合わせ、`macd_status="Bearish"` を渡した場合 | 27.2 | PASS |

- **経緯**:
  - 旧 `scratch/run_full_universe_pipeline.py` では `"ma25_divergence": row.get("ma_divergence")` と読み替えていた。移植の際にこれが抜けた。
  - `macd_status` は、旧 scratch でも `""` を渡していた。つまり MACD の配点とゲートキーパー3は、**旧パイプラインの時点から一度も働いていなかった**。
- **対応案**:
  - `ma25_divergence` は `row.get("ma_divergence")` から読む。
  - `macd_status` は `macd_hist` の正負から `"Bullish"` / `"Bearish"` を決める（`PolarsProcessor` で列として出力するのが望ましい）。
  - 評価フェーズのテストに、**実際の `PolarsProcessor` の出力を通して**、乖離率の配点とゲートキーパー3が効くことを確かめるケースを追加する。現在のテストは入力の辞書を直接組み立てているため、列名のずれを検知できない。

## 2. 🔴 [Bug] PreFilter に流動性データが渡っておらず、2つの除外ルールが働いていない
- **箇所**: `src/orchestration/phases/evaluation.py:58`
- **現象**: `PreFilter.apply_filter(repaired_df)` は、流動性データ（`df_liquidity`）を渡していない。`repaired_df` は最新日の1行だけに絞り込んだ後のデータなので、時系列の集計もできない。その結果、`PreFilter.evaluate` の既定値の処理（`pre_filter.py:205-216`）で次のようになる。

| 除外ルール | 本来の判定 | 現在の判定 |
|---|---|---|
| 1. 売買不能（直近5営業日に出来高ゼロの日がある） | 5日分の出来高 | `zero_volume_days_5d` に既定値の0が入り、**常に通過する** |
| 2. 極小流動性（20日平均の売買代金が3,000万円未満） | 20日間の平均 | **最新1日分**の `trading_value`（null の場合は `volume × price`）で判定される |

- **影響**:
  - 直近に取引が成立しなかった日のある銘柄が、評価対象に残る。
  - 1日だけ出来高が膨らんだ銘柄が通過し、逆に1日だけ薄商いだった銘柄が除外される。
- **経緯**: 旧 scratch では、`PreFilter.aggregate_timeseries_metrics(df_ts)` で集計した結果を `df_liquidity` として渡していた（`run_full_universe_pipeline.py:84-87`）。
- **対応案**:
  - 最新日に絞り込む**前**の `processed_df`（全履歴）から `aggregate_timeseries_metrics` で集計し、`apply_filter` に渡す。
  - `aggregate_timeseries_metrics` は `trading_value` 列を前提にしているが、`PolarsProcessor` の出力では `trading_value` が null（計算していない）である。`volume × price` から売買代金を計算する処理を、どちらかに入れる必要がある。前回指摘7の1（DB から読む履歴の `Volume` に売買代金が入っている問題）もあわせて直すこと。

## 3. 🟠 [Bug] README の実行手順が何もしない（再現済み）
- **箇所**: `README.md`「4. パイプライン実行」
- **現象**: `python3 -m src.orchestration.mode_handler --mode scan` を実行すると、何も起動せず、終了コード0で終わる。
- **根本原因**: `src/orchestration/mode_handler.py` は抽象基底クラス `ModeHandler` だけのファイルで、`if __name__ == "__main__":` も引数の処理もない。`src/` にはパイプラインを起動する入口（CLI）が存在しない（`__main__` があるのは `src/tools/generate_report.py` だけ）。
- **影響**: 前回指摘1（clone 直後にデモ手順が動かない）が、形を変えて残っている。終了コードが0なので、失敗にも気づきにくい。
- **対応案**:
  - `src/__main__.py` などに、`OrchestratorContext` を作成して `ScanHandler().execute(context)` を呼ぶ入口を作る（例: `python -m src scan`）。
  - README をその手順に合わせる。
  - 入口のテスト（引数の処理と `ScanHandler` の呼び出し）を追加する。

## 4. 🟠 [Bug] 比率のスケーリングで、自己資本比率以外の比率の単位がばらばらになる（再現済み）
- **箇所**: `src/services/financial_repair.py` の Step 5（比率の自動スケーリング）
- **現象**: 判定が「絶対値が1.0以下なら100倍」になった。自己資本比率（0〜1 または 0〜100%）にはこれで正しく働く。しかし、同じ判定を当てている流動比率・当座比率・D/Eレシオは、比率表記でも1を超えるのが普通なので、同じ列に「倍」と「%」が混ざる。

| 列 | 入力 | 出力 |
|---|---|---|
| `current_ratio` | 0.9 / 1.0 / 1.5 / 150.0 | **90.0** / **100.0** / **1.5** / 150.0 |
| `debt_equity_ratio` | 0.8 / 1.2 / 80.0 / 120.0 | **80.0** / **1.2** / 80.0 / 120.0 |

- **影響**: 現在の採点（`QuantEvaluator`）と `PreFilter` が使う比率は `equity_ratio` だけなので、スコアには影響しない。ただし、DB（`daily_metrics`）にはこの状態で保存されるため、後でこれらの列を使うと誤る。
- **補足（境界値の扱い）**: 新しいテスト `test_financial_repair_boundary_values` では `equity_ratio = 1.0 → 100.0` を正としている。しかし自己資本比率100%（負債ゼロ）は現実にはほぼない。一方、1% 前後の自己資本比率はあり得る。`1.0` は「1%」として扱う方が自然で、判定は `< 1.0` にするのが安全である。
- **対応案**:
  - スケーリングの対象を `equity_ratio` だけに絞る。
  - 境界の判定を `< 1.0` にし、テストの期待値を合わせる。
  - 恒久的には、前回レビューのとおり、単位をデータ取得元ごとに確定させる。

## 5. 🟡 [Bug] daily_report.csv に判定（verdict）が出力されず、旧採点の列が残っている
- **箇所**: `src/reporter.py:212-266`（`_format_single_item`）、`:268-274`（`_calculate_fundamental_score`）
- **現象**:
  - 評価フェーズで付与した `verdict` 列が、CSV の出力列に含まれていない。README には「評価通過銘柄（スコア・判定付き）」とある。
  - 旧採点の列が残っている。`Strategy` は `strategy_name` を読むため常に `-`、`Fundamental` は `score_value` などを読むため常に `0.0`、`Trend` は `score_trend` がないため `trend_score` にフォールバックする。
- **対応案**:
  - `Verdict` 列を追加する。
  - `Strategy`、`Fundamental_Raw`、`Fundamental` の列と `_calculate_fundamental_score` を削除する。
  - Google Sheets 側（`StockAnalysis_Latest_Report`）が列の並びに依存している場合は、あわせて確認する。

---

# B. 前回レビューの指摘ごとの反映状況

| 前回の指摘 | 状況 | 確認結果 |
|---|---|---|
| 1. README のデモ手順 | △ | 手順は差し替えられたが、新しい手順が何も起動しない（本書の指摘3） |
| 2. 配点の不連続 | ✅ | `_score_ma_div`（-25、-15、-5、5、15、25%）と `_score_rsi`（75）のすべての境界で、両側の値が一致することを確認した。なお、区間の形も変わっている（例: 乖離率 -20% は 5.0点 → 2.5点）。意図した変更であれば問題ない。**境界値のテストは未追加** |
| 3. thesis / risks | ✅ | 戻り値が `(score, verdict)` になった。互換用の別名 `QuantAgentEvaluator` が残っており、テストはその名前で呼んでいる |
| 4. `PipelineOrchestrator` | ✅ | 削除された。ただし `pipeline.py` のカバレッジは 45% のまま |
| 5. 最新日への絞り込み | △ | `group_by("code").last()` で、銘柄ごとに1行になった。ただし下記の懸念がある。`code` が重複しないことを確かめるテストは未追加 |
| 6. 比率スケーリング | △ | 自己資本比率は修正され、冪等性のテストも追加された。自己資本比率以外の比率に新しい問題がある（本書の指摘4） |
| 7. 取得処理の結合 | ❌ | `Volume` の単位混在、`unique("Date")` の `keep` 未指定、`result_queue.get(timeout=120)` は変わっていない |
| 8. 例外の握りつぶし | ❌ | `polars_processor.py` は変わっていない。さらに、評価フェーズの `save_metrics` の失敗を警告ログだけで握りつぶす処理が新たに入った（`evaluation.py` の手順5） |
| 9. 全履歴の採点・保存 | ✅ | 財務データの修復、保存、採点が最新日の行だけになった |
| 10. 銘柄ごとの全件検索 | ✅ | `partition_by("code", as_dict=True)` と、タプルのキー `(code,)` での取り出しに置き換わった（Polars 1.44.2 の仕様どおり） |
| 11. 使われていない `ThreadPoolExecutor` | ❌ | 変わっていない |
| 12. pandas との往復変換 | ❌ | 変わっていない |
| 13. テストカバレッジ | △ | `FinancialRepairService` のテストは追加された。配点の境界値テスト、評価フェーズの重複チェックのテスト、`xbrl_parser` のテストは未追加 |
| 14. 静的解析の設定 | ❌ | `check_untyped_defs` は未設定 |
| 15. 非推奨 API | ✅ | 該当箇所が削除され、DeprecationWarning が出なくなった |
| 16. パイプラインの一本化 | △ | `PreFilter` → `QuantEvaluator` の流れは組み込まれた。移植漏れがある（本書の指摘1、2） |
| 17. AI 残骸の削除 | ✅ | 下記の細かい残りを除いて完了 |
| 18. 旧採点のアーカイブ | ✅ | `archive/legacy_scoring/` に移され、ruff と mypy の除外設定にも追加された。下記の細かい残りと、本書の指摘5がある |

**前回指摘5の懸念**: `group_by("code").last()` は、銘柄ごとの最終行を取るだけである。売買停止中の銘柄や上場廃止になった銘柄は、何日も前の行がそのまま評価に入る。全体の最新日から N 営業日以内の行だけに絞る処理を足すと安全である。

---

# C. 細かい残り

| 対象 | 内容 |
|---|---|
| `src/constants.py` の `METRIC_CATEGORY`、`LOWER_IS_BETTER_DEFAULTS` | 旧採点でしか使っていなかった定数 |
| `src/repositories/duck_repository.py` の `rank_history`、`sentinel_alerts` テーブル | 旧機能（順位の履歴、監視アラート）のテーブル。docstring の `save_analysis_results` の記述も残っている（`polars_processor.py:211` も同様） |
| `tests/conftest.py:49` | `analysis_results` テーブルへの参照 |
| `tests/test_quant_evaluator.py` | 互換用の別名 `QuantAgentEvaluator` で呼んでいる。`QuantEvaluator` に置き換えれば、`quant_evaluator.py` 末尾の別名を削除できる |
| `config/defaults.yaml` の `filter.min_trading_value: 10000000` | `hard_filters.min_trading_value: 30000000` と値が異なる。`PreFilter` が読むのは `hard_filters` の方なので、`filter` 側が使われていなければ削除する |
| git の状態 | リネームと削除はステージ済み、変更は未ステージ、`tests/test_financial_repair.py` は git の管理対象外。コミットの際は `git add -A` などで取りこぼさないこと |

---

## 今後の対応案 (Next Steps)
優先度順。

1. 🔴 **移植漏れを直す**（指摘1、2）
   - テクニカル指標の列名を合わせ、`macd_status` を作る。
   - PreFilter に流動性データを渡す。
   - `PolarsProcessor` の実際の出力を通すテストを追加する。
2. 🟠 **パイプラインの入口を作る**（指摘3）。README の手順を実際に動かして確かめる。
3. 🟠 **比率スケーリングの対象を `equity_ratio` に絞る**（指摘4）。
4. 🟡 **レポートの列を整理する**（指摘5）。
5. 前回レビューの未着手の指摘（7、8、11、12、14）と、テストの追加（13）を進める。
