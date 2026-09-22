# stock-analyzer-core

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**stock-analyzer-core** は、全上場日本株（約 4,000 銘柄）を対象に、高速なデータ処理とインテリジェントな投資判断を両立する **「クオンツ × AI エージェント協調型」株式評価エンジン** です。

---

## 🌟 主な特徴

1. **超高速スクリーニング (Polars Engine)**:
   - Polars を全面採用し、数千銘柄の財務データ・株価時系列をミリ秒単位で処理。
   - ボロ株・整理銘柄・債務超過を瞬時に機械的除外（一次足切り）。
2. **連続グラデーション（リニア傾斜配点）クオンツ評価**:
   - 大味な二値判定を廃止し、指標の実数値に応じたきめ細かい 100 点満点採点モデル。
   - Wilder 法に準拠した RSI、MACD、25日移動平均乖離率のモメンタム判定。
3. **多層投資判断ゲートキーパー (防衛アーキテクチャ)**:
   - **一過性特別利益トラップ抑止**: 本業赤字で資産売却益により低PER・高ROEに見えるバリュートラップ銘柄を自動検知し、評価を厳格に制限。
   - **実績赤字制限**: 実績赤字（ROE < 0）銘柄の投資判断を最大 `WATCH` に制限。
   - **テクニカル足切り**: 下降トレンド（MACD Bearish / 乖離率 < -10%）時の買い判定を制限。
4. **2ファイル完全分離クリーン CSV 出力**:
   - `daily_report.csv`: 評価可能銘柄（全19カラム、欠損率 0.00% の完全データ）。
   - `uncalculable_stocks.csv`: 算出不能銘柄（当期純損失や債務超過などの理由・内訳付きで明示分離）。

---

## 🏗️ システムアーキテクチャ

```text
[全 4,000 銘柄データ (EDINET / yfinance / JPX)]
       │
       ▼
【Stage 1: スクリプト層 (DuckDB + Polars)】
   ・高速データ補完・整合性チェック
   ・機械的一次足切り (PolarsEngine.screening)
       │
       ▼ 高密度銘柄カルテ (StockDossier)
【Stage 2: エージェント層 (QuantAgentEvaluator / LLM)】
   ・連続グラデーション採点 (Agent_Score)
   ・多層ゲートキーパー適用 (STRONG_BUY / BUY / WATCH / PASS)
       │
       ├─► daily_report.csv (完全評価可能銘柄 / 欠損ゼロ)
       └─► uncalculable_stocks.csv (算出不能銘柄 / 理由付き)
```

---

## 🚀 クイックスタート

### 1. インストール

```bash
git clone https://github.com/akkyey-stock-org/stock-analyzer-core.git
cd stock-analyzer-core

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境設定

```bash
cp .env.example .env
# 必要に応じて .env 内の API キー等を設定
```

### 3. 単体テストの実行

```bash
pytest tests/test_quant_evaluator.py
```

### 4. パイプライン実行（デモ・全銘柄評価）

```bash
python3 scratch/run_full_universe_pipeline.py
```

実行後、`data/output/daily_report.csv` に全件ランク付け結果が出力されます。

---

## 📄 ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。
