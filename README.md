# stock-analyzer-core

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**stock-analyzer-core** は、全上場日本株（約 4,000 銘柄）を対象に、高速なデータ処理と堅牢な防衛ロジックを両立する **超高速クオンツ分析・多層評価エンジン** です。

---

## 🌟 主な特徴

1. **超高速スクリーニング & 3層パイプライン (Polars + DuckDB)**:
   - Polars を全面採用し、数千銘柄の財務データ・株価時系列をミリ秒単位で処理。
   - **第1層 (PreFilter)**: ボロ株・極小流動性トラップ・債務超過を瞬時に機械的足切り。
2. **連続グラデーション（リニア傾斜配点）クオンツ評価**:
   - **第2層 (QuantEvaluator)**: 指標の実数値に応じたきめ細かい 100 点満点リニア配点モデル。
   - Wilder 法準拠の RSI、MACD、25日移動平均乖離率のモメンタム判定。
3. **多層投資判断ゲートキーパー (防衛アーキテクチャ)**:
   - **一過性特別利益トラップ抑止**: 本業赤字で資産売却益により低PER・高ROEに見えるバリュートラップ銘柄を自動検知し、評価を厳格に制限。
   - **実績赤字制限**: 実績赤字（ROE < 0）銘柄の投資判断を最大 `WATCH` に制限。
   - **テクニカル足切り**: 下降トレンド（MACD Bearish / 乖離率 < -10%）時の買い判定を制限。
4. **2ファイル完全分離クリーン CSV 出力**:
   - **第3層 (IntegrationPhase)**:
     - `daily_report.csv`: 評価通過銘柄（スコア・判定付き）。
     - `uncalculable_stocks.csv`: 算出不能・除外銘柄（除外理由・内訳付きで完全分離）。

---

## 🏗️ システムアーキテクチャ

```text
[全 4,000 銘柄データ (EDINET / yfinance / JPX)]
       │
       ▼
【Acquisition Phase (市場データ・財務データ高速収集)】
   ・EDINET XBRL 自動取得 & yfinance バッチ同期
   ・時系列テクニカル指標の一括ベクトル算出 (PolarsProcessor)
       │
       ▼
【Evaluation Phase (3層クオンツ評価)】
   ・最新日レコードへの絞り込み
   ・財務修復サービス (FinancialRepairService による比率スケーリング)
   ・第1層: PreFilter (地雷株・極小流動性足切り)
   ・第2層: QuantEvaluator (100点満点リニア採点 & ゲートキーパー判定)
       │
       ▼
【Integration Phase (レポート生成・外部連携)】
   ・daily_report.csv (評価合格銘柄)
   ・uncalculable_stocks.csv (除外銘柄 / 理由付き)
   ・Google Drive / Spreadsheet / Discord 自動連携
```

---

## 🚀 クイックスタート

### 1. インストール

```bash
git clone https://github.com/akkyey-stock-org/stock-analyzer-core.git
cd stock-analyzer-core

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境設定

```bash
cp .env.example .env
# 必要に応じて .env 内の認証情報を設定
```

### 3. 単体テストの実行

```bash
pytest
```

### 4. パイプライン実行（全銘柄スキャン）

```bash
# 推奨: パッケージエントリポイントからの実行
python3 -m src scan

# または mode_handler からの直接実行
python3 -m src.orchestration.mode_handler --mode scan
```

実行後、`data/output/` ディレクトリに `daily_report.csv` および `uncalculable_stocks.csv` が出力されます。

---

## 📄 ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。
