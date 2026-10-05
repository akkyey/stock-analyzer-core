# 設計書: パイプライン実行ログおよび Colab ノートブック表示メッセージの改善方針

- **作成日**: 2026-10-05
- **対象バージョン**: v1.3.1（予定）
- **ステータス**: レビュー待ち（実装保留）
- **対象ファイル**:
  - `notebooks/stock_analyzer_colab.ipynb`
  - `src/orchestration/phases/acquisition.py`
  - `src/fetcher/jpx.py`

---

## 1. 背景と課題

Google Colab 上での検証（初回構築および継続実行）において、以下の「表示と実態の乖離」および「ユーザーの違和感」が確認された。

1. **所要時間表示の乖離**:
   - 初回構築モードにおいて、ノートブック上で「所要時間は約 8〜10 分」と案内されていたが、実測は **約 20.0 分**（市場データ取得 12.2 分＋前処理・EDINET同期・DB構築等）であった。
   - 以前の並列ダウンロード（マルチスレッド）時代の古い固定文字列が残っており、Yahoo Finance 429 規制対策でシングルスレッド化した現状と合致していなかった。
2. **銘柄数推移の不透明さ（3920 → 3912 → 3890）**:
   - ログ上で理由の説明なく数字が遷移するため、データの欠落やバグと誤認されやすい。
     - `3,920`: JPX公式上場銘柄リストの全行数
     - `3,912`: 内部同梱の財務シード（直近新規上場 8 社未収録）
     - `3,890`: 株価配信のない PRO Market / 30日連続失敗等（30 社）を除外した市場データ取得対象
3. **前処理中の沈黙時間**:
   - `🚀 パイプライン実行を開始します...` から `📡 全 3890 銘柄の市場データ取得を開始します...` まで、EDINET API 通信やキャッシュ読み込み等の処理が裏で実行されているが、コンソール表示がないためフリーズしたような体感を与える。
4. **取得完了内訳の表現不足**:
   - 成功件数（3,846 件）と失敗件数（44 件）が別々に表示され、母数（3,890 件）との整合関係が直感的に分かりにくい。

---

## 2. 改善方針と設計

### 2.1 Colab ノートブック側の修正 (`notebooks/stock_analyzer_colab.ipynb`)

#### ① Step 1 セル（実行準備・モード検知）
- **初回実行検知時**:
  ```python
  # 現状
  print("   Step 3 の実行には【約 8〜12 分】かかります。ブラウザを閉じずにお待ちください ☕")
  print("   （※2回目以降も、全3,920銘柄に通信するため同程度の時間がかかります）")

  # 改善後
  print("   Step 3 の実行には【約 18〜20 分】かかります。ブラウザを閉じずにお待ちください ☕")
  print("   （※全3,890銘柄の過去1年分日足＋EDINET開示データを安全に一括初期構築します）")
  ```
- **差分更新検知時（2回目以降）**:
  ```python
  # 現状
  print("   所要時間は【約 8〜10 分】となります。ブラウザを閉じずにお待ちください ☕")

  # 改善後
  print("   所要時間は【約 5〜8 分】となります。ブラウザを閉じずにお待ちください ☕")
  print("   （※既存DBをベースに、直近の市場データ差分のみを高速スキャンします）")
  ```

#### ② Step 3 セル（事前案内・Markdown / Code）
- **Markdown セル (Cell 9)**:
  - 目安時間を「初回実行時は約 18〜20 分、2回目以降は約 5〜8 分」へ更新。
- **実行直前メッセージ (Cell 10)**:
  ```python
  if is_first:
      print("⏳ 【初回実行モード】全3,890銘柄の市場データを初期スキャン中...")
      print("   所要時間は【約 18〜20 分】です（安全のため順次取得）。ブラウザを閉じずに少々お待ちください ☕")
  else:
      print("⚡ 【差分更新モード】既存DBをもとに直近の市場データ差分を取得中...")
      print("   所要時間は【約 5〜8 分】です。ブラウザを閉じずに少々お待ちください ☕")
  ```

---

### 2.2 パイプライン本体のログ修正 (`src/orchestration/phases/acquisition.py`)

#### ① 銘柄マスタ・財務シードの登録ログ
差分が発生している理由をログに併記する。

```python
# _ensure_stock_master
print(
    f"   ✅ 東証(JPX)公式リストから銘柄マスタに {len(target_codes)} 銘柄を初期登録しました。",
    flush=True,
)

# _ensure_fundamentals
diff_seeds = len(target_codes) - len(df_seed) if len(target_codes) >= len(df_seed) else 0
diff_note = f" (直近の新規上場等 {diff_seeds} 銘柄を除く)" if diff_seeds > 0 else ""
print(
    f"   ✅ 財務シードデータから {len(df_seed)} 銘柄を初期登録しました{diff_note}。",
    flush=True,
)
```

#### ② 前処理（EDINET同期・キャッシュ）の可視化
裏で進行している処理について、ユーザー向けに 1 行進捗を出力する。

```python
# _sync_edinet 開始時
print("   🔍 EDINET 最新開示の同期確認中 (過去30日分)...", flush=True)

# キャッシュ利用時 / 同期完了時
print(f"   ✨ EDINET 同期完了 ({sync_count} 件の開示書類を反映 / キャッシュ有効)", flush=True)
```

#### ③ 市場データ取得対象の選定理由の明示
```python
# _select_targets / _fetch_market_data
excluded_special = len(target_codes_before) - len(target_codes)
if excluded_special > 0:
    print(
        f"   ℹ️ 市場データ取得対象: {len(target_codes)} 銘柄 "
        f"(株価配信のない PRO Market 等 {excluded_special} 銘柄を除外)",
        flush=True,
    )
print(
    f"📡 全 {len(target_codes)} 銘柄の市場データ取得を開始します...",
    flush=True,
)
```

#### ④ 完了サマリーの整合性表記
```python
# _fetch_market_data 完了時
print(
    f"✅ 市場データ取得完了: 合計 {total_attempted} 銘柄\n"
    f"   ・正常取得: {success_count} 銘柄\n"
    f"   ・取得対象外/データなし: {fail_count} 銘柄 (PRO Market、直近取引停止等)",
    flush=True,
)
```

---

## 3. 実装・検証手順（実装着手時）

1. **コード修正**:
   - `notebooks/stock_analyzer_colab.ipynb` の Cell 4, 9, 10 を更新
   - `src/orchestration/phases/acquisition.py` のログ出力箇所を更新
2. **ローカルテスト**:
   - `pytest` で既存テスト（特にログ関連やフェーズ実行テスト）にリグレッションがないか確認
   - 品質ゲートキーパー（Ruff / Mypy / Radon CC）の合格確認
3. **Colab での表示確認**:
   - ノートブックを実行し、ログの視認性・整合性が意図通り向上したことを確認
