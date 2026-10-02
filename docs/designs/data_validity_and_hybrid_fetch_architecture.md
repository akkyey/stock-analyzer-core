# データ妥当性検証・ハイブリッド取得・自己修復 (Self-Healing) 設計仕様書

## 1. 概要 (Overview)

本ドキュメントは、`stock-analyzer-core` における定量財務データの品質保証・データ妥当性検証フレームワーク、yfinance と EDINET のハイブリッドデータ取得アーキテクチャ、およびデータ欠損に対する自己修復（Self-Healing）メカニズムの仕様を規定するものである。

---

## 2. データ妥当性検証フレームワーク (4-Tier Validity Assurance Framework)

自前計算 (`_Src = calc`) や補完データの数学的・会計学的・実市場データ的妥当性を客観的に証明するため、以下の4段階の検証アプローチを導入する。

```
+-----------------------------------------------------------------------+
|                4-Tier Validity Assurance Framework                    |
+-----------------------------------------------------------------------+
|  [Tier 1] 交差検証 (Cross-Validation)                                  |
|           - yf/edinet 確定値との相対誤差 (MAPE < 3%) / 相関係数 (R > 0.95)|
+-----------------------------------------------------------------------+
|  [Tier 2] 会計的・数学的恒等式検証 (Accounting Identity Sanity)       |
|           - 時価総額 = 株価 × 発行済株式数                            |
|           - PBR ≒ PER × (ROE / 100)                                   |
+-----------------------------------------------------------------------+
|  [Tier 3] ドメイン範囲・外れ値検知 (Range & Outlier Sanity)           |
|           - PER, PBR, 配当利回り, ROE の実効範囲チェック & クリッピング   |
+-----------------------------------------------------------------------+
|  [Tier 4] 自動ユニットテスト & 回帰防止 (Automated Test Suite)        |
|           - pytest による継続的統合・データ不整合の自動検知           |
+-----------------------------------------------------------------------+
```

### 2.1 [Tier 1] 交差検証 (Cross-Validation)
- **対象**: `yf` (yfinance直取得) および `edinet` (EDINET確定値) が揃っているベンチマーク銘柄群。
- **検証方法**: ベンチマーク銘柄に対しあえて自前計算式 (`calc`) を適用し、確定値と自前計算値の平均絶対相対誤差 (MAPE) および決定係数 ($R^2$) を測定。
- **合格基準**: $R^2 \ge 0.95$ かつ $\text{MAPE} \le 3.0\%$。

### 2.2 [Tier 2] 会計的・数学的恒等式検証 (Accounting Identity Sanity)
毎回のデータ生成時、以下の財務公理・恒等式を満たしているかを検証する：
1. **時価総額**: $\text{Market Cap} = \text{Price} \times \text{Shares}$
2. **PBR と PER・ROE の関係**: $\text{PBR} \approx \text{PER} \times \frac{\text{ROE}}{100}$
3. **配当利回り**: $\text{Div Yield (\%)} = \frac{\text{DPS}}{\text{Price}} \times 100$

### 2.3 [Tier 3] ドメイン範囲・外れ値検知 (Range & Outlier Sanity)
- **範囲規定**:
  - `PER`: $0.5 \le \text{PER} \le 1000$ (極端な異常値のクリッピング)
  - `PBR`: $0.01 \le \text{PBR} \le 100$
  - `Div_Yield`: $0\% \le \text{配当利回り} \le 30\%$
  - `ROE`: $-100\% \le \text{ROE} \le 200\%$

### 2.4 [Tier 4] 自動ユニットテスト (Automated Test Suite)
- [`tests/test_reporter_and_validation.py`](file:///home/irom/dev/project-stock2/stock-analyzer-core/tests/test_reporter_and_validation.py) および [`scratch/verify_calc_validity.py`](file:///home/irom/dev/project-stock2/stock-analyzer-core/scratch/verify_calc_validity.py) にて、レポート生成処理および数値の正確性を回帰テストとして自動検証する。

---

## 3. yfinance & EDINET ハイブリッドアーキテクチャ

### 3.1 データソースと役割分担
- **yfinance (`yf`)**: 株価履歴、出来高、即時市況データ、時価総額・主要指標の即時値。
- **EDINET (`edinet`)**: 有価証券報告書・半期報告書の公式確定財務諸表（売上高、営業利益、純利益、ROE、自己資本比率、EPS・BPS・DPS 等）。取り込み仕様は 3.3 節。
- **自前計算・補完 (`calc`)**: 欠損指標の数学的推定、株価・発行済株式数・純利益からの逆算修復。

### 3.3 EDINET 取り込み仕様（v1.3.0）
v1.2.0 までの取り込みには、2026-10-03 の監査で次の不具合が見つかった（いずれも EDINET の原本と実 DB で確認）。v1.3.0 で作り直した。
- 半期報告書のコンテキスト（`InterimDuration` 等）を読めず、取れなかった項目を NULL で上書きしていた（11 月は全銘柄の約 6 割が該当）。
- コンテキストの部分一致で単体・セグメントの値を拾い、IFRS のタグにも未対応だった（例: ニデックの売上を単体の 2,735 億円と読んでいた。連結は 2.7 兆円）。
- ROE・自己資本比率を一度も更新せず、同梱シードの値のままだった。
- 過年度の訂正報告書が、新しい期の値を上書きし得た。

**読み取り（`src/fetcher/xbrl_parser.py`）**
- 値は「主要な経営指標等の推移」（`*SummaryOfBusinessResults`）から読む。会計基準（日本基準 / IFRS / 米国基準）ごとの要素名を優先順に持つ。営業利益だけは財務諸表本体（`OperatingIncome` / `OperatingProfitLossIFRS`）から読む。
- コンテキストは**完全一致**。要約表に連結（メンバー無し）の値があれば連結、無ければ単体（`_NonConsolidatedMember`）を使う。配当と発行済株式数は提出会社の値として報告されるため常に単体を許容する。
- IFRS の `EquityToAssetRatioIFRSSummaryOfBusinessResults` は名前に反して BPS（1 株当たり親会社所有者帰属持分）。自己資本比率は `RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults`。
- ROE・自己資本比率は XBRL の小数（0.138）を % に換算する。保存値は常に % 表記のため、比率の 100 倍補正（`financial_repair.ratio_scaling_threshold`）は既定で無効にした（自己資本比率 0.7% の実在値を 70% にしていた）。
- 半期報告書からは貸借対照表の項目（総資産・純資産・自己資本比率・発行済株式数）だけを返す。年間の利益と 1 株当たり指標は有価証券報告書の値を使い続ける。
- 抽出ロジックの版 `PARSER_VERSION` を結果と `edinet_documents.parser_version` に記録し、古い版で処理した書類は自動的に取り込み直す。

**書類の選択（`src/fetcher/turbo_acquisition.py`）**
- 対象は 120（有価証券報告書）・130（訂正）・160（半期報告書）・170（訂正）。四半期報告書（140/150）は 2024 年 4 月に廃止され、現在は過去分の訂正しか出ないため対象外。
- 銘柄ごとに、有価証券報告書は**対象期間が最も新しいもの**（同じ期は提出が新しいもの）、半期報告書はそれより新しい期のものを選ぶ。期末が 480 日より古い有価証券報告書（過年度の訂正）は選ばない。
- 走査期間は `fetcher.edinet_scan_days`（既定 30 日）と「前回の走査日から今日まで」の長い方（上限 365 日）。前回の走査日は `app_meta.edinet_last_scan_date` に記録し、一覧を取得できなかった日があれば次回その日から走査し直す。

**DB 反映（`src/services/edinet_bridge.py`）**
- 取れた項目だけを更新する（NULL で上書きしない）。
- `fundamentals` に出所（`period_end` / `submitted_at` = 損益・1 株当たり指標、`bs_period_end` / `bs_submitted_at` = 貸借対照表）を記録し、それより古い期の書類は反映しない（`edinet_documents.status = 'superseded'`）。
- ROE・自己資本比率は書類の報告値を使い、報告が無い場合だけ純利益 / 純資産、純資産 / 総資産で補う。
- 旧版の取り込みで NULL になった株価に依存しない項目（純利益・売上・営業利益・EPS・BPS・DPS・発行済株式数等）は、初回実行時に 1 回だけ同梱シードから補う（DB に値がある項目は変更しない。`app_meta.fundamentals_restored_parser_v2`）。

**株式分割（`stock_splits`）**
- 株価取得で受け取った分割イベント（比率 0.02〜100）を記録し、株価履歴の調整（段差が残っている場合のみ）と 1 株当たり指標の補正に使う（`colab_execution_architecture_and_guide_design.md` 3.3.2）。
- 評価時、分割日が財務値の基準日（書類の提出日時。同梱シード由来の値はシードの作成日時 `updated_at`）より後なら、EPS・BPS・DPS を比率で割り、発行済株式数に比率を掛ける（分割前の 1 株当たり指標を分割後の株価で割ると、PER・PBR が分割比率の分だけ割安に見えるため）。基準日が不明な値は、二重の補正を避けるため補正しない。比率が範囲外の記録は使わない。

### 3.2 データ識別マーカー列 (`_Src`)
出力 CSV (`daily_report.csv`) の数値列の直前には、データソース識別列を配置しデータの透過性を保証する：
- `PER_Src`, `PER`
- `PBR_Src`, `PBR`
- `Div_Yield_Src`, `Div_Yield`
- `ROE_Src`, `ROE`
- `Market_Cap_Src`, `Market_Cap`

---

## 4. データ取得モードとオプション切替仕様

429 Too Many Requests (レート制限) 回避と取得速度のトレードオフに対応するため、オプション切り替え機能を備える。

| モード名 | フラグ / 環境変数 | 特徴・仕様 | 推奨利用シーン |
| :--- | :--- | :--- | :--- |
| **FAST モード** (高速優先) | `--mode fast`<br>`YF_FETCH_MODE=fast` | 高並列処理 (Workers: 15)。即座に取得できる分を取得し、429発生時は即座に EDINET/calc に自動切り替え | 毎日のスクリーニング、1分未満での即時レポート生成 |
| **THOROUGH モード** (慎重高精度) | `--mode thorough`<br>`YF_FETCH_MODE=thorough` | 低並列処理 (Workers: 3)。リクエスト間にウェイト (`--delay 0.4` 秒 + ジッター) を挿入し yf 直取得率を最大化 | yfinance 生データの完全性を最重視する定期更新 |

### 4.1 コマンド実行例
```bash
# 高速優先モード (デフォルト)
python scratch/fetch_yfinance_fast_batch.py --mode fast

# 慎重・高精度モード (ウェイト0.5秒 + ジッター)
python scratch/fetch_yfinance_fast_batch.py --mode thorough --delay 0.5
```

---

## 5. 自己修復（Self-Healing）アルゴリズム

[`src/services/financial_repair.py`](file:///home/irom/dev/project-stock2/stock-analyzer-core/src/services/financial_repair.py) において、以下の自己修復ロジックを Polars ベクトル演算でミリ秒単位で適用する：

1. **D/E比からの自己資本比率逆算**:
   $$\text{Equity Ratio (\%)} = \frac{100.0}{1.0 + \frac{\text{Debt Equity Ratio}}{100.0}}$$
2. **精密 PER の推計**:
   $$\text{PER} = \frac{\text{Price}}{\frac{\text{Net Profit}}{\text{Shares Outstanding}}}$$
3. **黒字転換ステータスの自動付与** (`turnaround_black` 等)
4. **比率表記の自動スケーリング (100倍補正)**: v1.3.0 で既定を無効化（3.3 節）。
   - 保存値は常に % 表記。設定 `financial_repair.ratio_scaling_threshold` を正の値にした場合のみ、その値未満を 100 倍する。

---

## 6. 改訂履歴 (Revision History)

- **v1.1 (2026-10-03)**: 3.3 節（EDINET 取り込み仕様）を追加。半期報告書による NULL 上書き、IFRS・単体値の誤読、ROE・自己資本比率の未更新、過年度訂正による上書きの修正と、株式分割の補正を記載。比率の 100 倍補正を既定で無効化。
- **v1.0 (2026-09-20)**: 初版作成。妥当性検証4段階フレームワーク、EDINETハイブリッド、FAST/THOROUGH モード選択オプションの仕様追加。
