# v1.3.1 オーナー変更のレビューと対応記録

- **作成日**: 2026-10-09
- **対象**: `e3f635c30bb9`（安全化シード）〜 `e4db65d` の変更、および対応コミット `b3a3588`
- **関連資料**:
  - [v1.3.1 リリース完了報告書](v1_3_1_release_report_20261006.md)
  - [公開前レビュー票](v1_3_1_pre_release_review_ticket_20261006.md)
  - [データの出所・再配布方針](../designs/data_sources_and_redistribution_policy.md)

---

## 1. 確認して問題がなかった点

- シード `e3f635c30bb9`: 出所未確認の 16 列だけが空になり、EDINET 由来の列・行数（3,926）・型・列の並びは変わっていない。
- `scripts/build_fundamentals_seed.py`: 出所未確認の列を既定で空にする（`--keep-unverified` で無効化）。
- 品質ゲート（ruff・mypy・テスト）: 通過。
- タグ `v1.3.1`: 注釈付きで `4d43b10` を指し、タグ時点の `TARGET_BRANCH = "v1.3.1"`。
- README・利用ガイドの免責: 出典、加工、非公式性、株価を同梱しないこと、が記載されている。

## 2. 指摘と対応

| # | 重要度 | 指摘 | 対応 |
| :--- | :--- | :--- | :--- |
| 1 | 高 | ノートの「財務データ自前再構築」セルと利用ガイド 7-5 の出力（`fundamentals_custom.parquet`）を、パイプラインが読まない（読むのは同梱の `fundamentals_seed.parquet` のみ）。Drive にも同期されず、「自給自足」の記述が実装されていなかった | セルを削除。Step 0 に `refetch_edinet_year`（既定オフ）を追加した（§3）。ガイド 7-5 を書き換え |
| 2 | 高 | 再構築セルの作りの問題: API キーがフォームに入りノートに残る／失敗しても「完了」と表示／2 回目の実行が拒否される | セル削除により解消。キーは Step 2 の設定を使う |
| 3 | 中 | `Profit_Growth` は同梱データが空のため、新規環境で全銘柄が `-` になる | `profit_growth_raw`（評価時に EDINET の純利益から計算）を優先して表示。無ければ保存値、それも無ければ `-`。テスト追加 |
| 4 | 中 | 既存 DB は旧値が残り、新規 DB は空のため、営業 CF の足切りと `Profit_Growth` が異なり得る | `Profit_Growth` は #3 で差が縮まった。営業 CF の足切りの差は残る。方針の設計書に明記（意図した差として扱う） |
| 5 | 中 | 「既存 DB には 16 列の旧値が維持される」の記述が不正確。`sales_growth` は、シードの売上が更新された銘柄で、財務データの更新時に空になる | 方針の設計書・利用ガイドを訂正（維持されるのは 15 列） |
| 6 | 低 | README・利用ガイドの「公共データ利用規約（第 1.0 版）」のリンクが EDINET 利用規約のページだった | PDL1.0 の原文ページ（`https://www.digital.go.jp/resources/open_data/public_data_license_v1.0`）へのリンクを追加 |
| 7 | 低 | レビュー票: T-06 が「作業待ち」のまま。T-03 の検索コマンドの文字列が自分自身に一致していた | T-06 を「合格」に更新。検索パターンを文字クラスで書き直し（検索結果 0 件） |
| 8 | 低 | 同梱の JPX 銘柄一覧（`src/resources/jp_stock_list.csv`）の利用条件が未確認 | **未対応**（§5） |

## 3. 設計: 財務データの取り直し（`refetch_edinet_year`）

### 3.1 判断の根拠
- 毎回の実行は、前回の EDINET 走査日から今日までを自動で取り直す（`acquisition.py` の `_edinet_scan_days`。上限 365 日）。普段使う人は、何もしなくても財務データが最新になる。
- 取り直しで追いつけないのは、**同梱データが古くなってから初めて使う**場合だけ。前回の走査日の記録がなく、既定の `edinet_scan_days`（30）分しか取らない。
- この場合も、`edinet_scan_days` を 365 にするだけで、通常のパイプラインが、取得 → パーサー → 異常値のガード → 決算期の新旧判定つきの反映を行う。別の中間ファイルや、それを読み込む仕組みは要らない。

### 3.2 仕様
- Step 0 のチェック `refetch_edinet_year`（既定オフ）。
- オンのとき、Step 3 が `fetcher.edinet_scan_days = 365` を設定する。EDINET の API キーは Step 2 の設定を使い、未設定なら止める。
- 所要時間は、通常の実行に約 100 分が加わる。完了後は、チェックを外す。
- 途中で切れた場合は、走査日の記録が残らないため、次回また 1 年分を取り直す。
- EDINET の規約は、短時間の大量のアクセスを禁じているため、取り直しは必要なときに 1 回だけにする。
- `scripts/build_fundamentals_seed.py` は、開発者が同梱シードを作る道具として残す（利用者向けの手順ではない）。

## 4. 変更したファイル（コミット `b3a3588`）

- `notebooks/stock_analyzer_colab.ipynb`（再構築セルと説明の削除、Step 0・Step 3 の変更）
- `src/reporter.py`、`tests/test_reporter.py`（`Profit_Growth`）
- `docs/guides/colab_user_execution_guide.md`（7-5、8 章）
- `docs/designs/data_sources_and_redistribution_policy.md`
- `docs/reviews/v1_3_1_pre_release_review_ticket_20261006.md`
- `README.md`

検証: ruff・mypy・テスト 289 件が通過。

## 5. 公開前に残っている項目

- Colab 実機での `refetch_edinet_year` の動作確認（約 100 分）。
- `Profit_Growth` の実データでの確認（テストは単体のみ）。
- リリース報告書（[v1_3_1_release_report_20261006.md](v1_3_1_release_report_20261006.md)）の §2 課題 2・§3 が、削除した再構築セルについて書かれている（履歴の記録として未修正）。
- リリース報告書に内部のサーバー名が書かれている（精査予定）。
- JPX の銘柄一覧の利用条件の確認（指摘 8）。
- yfinance の利用条件の補足は、付属のドキュメントで行う予定（ノートには載せない）。
- `scripts/verify_parity.py` の `run_verification` の複雑度 26（D）は、以前からのもの。検証用のスクリプトであり、`scripts` を品質ゲートに含めると決めるまで対応しない。
