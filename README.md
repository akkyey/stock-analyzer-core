# stock-analyzer-core

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/akkyey/stock-analyzer-core/blob/main/notebooks/stock_analyzer_colab.ipynb)

**stock-analyzer-core** は、全上場日本株（約 4,000 銘柄）を対象に、高速なデータ処理と堅牢な防衛ロジックを両立する **超高速クオンツ分析・多層評価エンジン** です。Google Colab の無料枠で手軽にブラウザ上から実行できます。

---

## 🌟 主な特徴

1. **超高速スクリーニング & 3層パイプライン (Polars + DuckDB)**:
   - Polars を全面採用し、数千銘柄の財務データ・株価時系列をミリ秒単位で処理。
   - **第1層 (PreFilter)**: ボロ株・極小流動性トラップ・債務超過を瞬時に機械的足切り。
2. **連続グラデーション（リニア傾斜配点）クオンツ評価**:
   - **第2層 (QuantEvaluator)**: 指標の実数値に応じたきめ細かい 100 点満点リニア配点モデル。
   - Wilder 法準拠の RSI、MACD、25日移動平均乖離率のモメンタム判定。
3. **多層ゲートキーパー & 客観的グレード判定 (防衛アーキテクチャ)**:
   - **客観的格付けグレード (Grade S/A/B/C)**: 金融商品取引法（投資助言規制）に配慮し、売買推奨ではなく客観的スクリーニング基準に基づく格付け（`Grade S` / `Grade A` / `Grade B` / `Grade C`）を標準採用（設定により旧来の `STRONG_BUY` 等へも切替可能）。
   - **一過性特別利益トラップ抑止**: 本業赤字で資産売却益により低PER・高ROEに見えるバリュートラップ銘柄を自動検知し、評価を厳格に制限。
   - **実績赤字制限**: 実績赤字（ROE < 0）銘柄の判定を最大 `Grade B`（旧 `WATCH`）に制限。
   - **テクニカル足切り**: 下降トレンド（MACD Bearish / 乖離率 < -10%）時の最上位グレード付与を制限。
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

### 1. Google Colab で実行（推奨・環境構築不要）

ブラウザだけで全 4,000 銘柄のスクリーニングを実行できます。以下のバッジからノートブックを開いて、Step 0 から順に再生ボタン（▶）を押すだけで完走します。

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/akkyey/stock-analyzer-core/blob/main/notebooks/stock_analyzer_colab.ipynb)

- **詳細マニュアル**: [docs/guides/colab_user_execution_guide.md](docs/guides/colab_user_execution_guide.md)

#### 📁 Google Drive 上の保存先と構成
Colab 実行時、結果やキャッシュは Google Drive 内に安全に自動永続化されます（デフォルト: `マイドライブ/StockAnalyzer/`）：
```text
Google Drive/
└── StockAnalyzer/                     # 保存先フォルダ（カスタマイズ可能）
    ├── output/daily_report.csv        # 本日のスクリーニング合格銘柄一覧
    ├── output/uncalculable_stocks.csv # 足切り除外銘柄一覧（理由付き）
    ├── cache/stock_analyzer.duckdb    # 時系列キャッシュDB（過去履歴の蓄積・差分更新用）
    └── config/custom_config.json      # 独自カスタマイズ設定（任意）
```
- **所要時間の目安**: 
  - **ローカル / 専用サーバー**: 平常差分時は **約 1分40秒**（通信1回あたりの応答が速いため）
  - **Google Colab（環境制約）**: 初回・2回目以降ともに **約 8〜10分**（Colab からの通信は1回あたりの応答に時間がかかるため）
- **保存先の変更方法**: Step 0 のセル上部にある入力フォーム **`drive_folder_name`** にフォルダ名（例: `MyStock`）や相対パス（例: `Portfolio/Japan`）、共有ドライブ（例: `/content/drive/Shareddrives/...`）を入力するだけで自由に変更可能です。


### 2. ローカル環境でのインストール・実行

```bash
git clone https://github.com/akkyey/stock-analyzer-core.git
cd stock-analyzer-core

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 環境設定

```bash
cp .env.example .env
# 必要に応じて .env 内の認証情報を設定
```

### 4. 単体テストの実行

```bash
pytest
```

### 5. パイプライン実行（全銘柄スキャン）

```bash
# 推奨: パッケージエントリポイントからの実行
python3 -m src scan

# または mode_handler からの直接実行
python3 -m src.orchestration.mode_handler --mode scan
```

実行後、`data/output/` ディレクトリに `daily_report.csv` および `uncalculable_stocks.csv` が出力されます。

---

## 📊 データの出所と免責事項

- **財務データ**: 金融庁 EDINET 閲覧（提出）サイト（https://disclosure2dl.edinet-fsa.go.jp/）の開示書類（有価証券報告書・半期報告書等の XBRL）をもとに、本プロジェクトが加工・作成したものです（[公共データ利用規約（第 1.0 版）](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html) に準拠）。国が作成した未加工の公式データではありません。
- **株価・時系列データ**: 利用者の実行環境において、Yahoo! Finance（yfinance）から都度取得されるものであり、本リポジトリには同梱・再配布していません。
- **銘柄マスタ**: 日本取引所グループ（JPX）の公表情報をもとにしています。
- **非公式ツールの明記**: 本ツールは金融庁、株式会社日本取引所グループ、米 Yahoo! Inc. のいずれとも提携・公認関係にはなく、非公式のオープンソース研究プロジェクトです。投資助言業務や売買推奨を目的としたものではありません。投資判断は必ず自己責任で行ってください。

---

## 📄 ライセンス

本プロジェクトおよび配布ノートブックは、[MIT License](LICENSE)（Copyright (c) 2026 akkyey）の下で公開されています。商用・非商用を問わず自由にご利用・改変いただけます。

