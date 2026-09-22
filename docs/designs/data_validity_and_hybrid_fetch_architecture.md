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
- **EDINET (`edinet`)**: 有価証券報告書・四半期報告書の公式確定財務諸表（売上高、営業利益、自己資本比率等）。
- **自前計算・補完 (`calc`)**: 欠損指標の数学的推定、株価・発行済株式数・純利益からの逆算修復。

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
4. **比率表記の自動スケーリング (100倍補正)**:
   - 小数表記 (`0.45`) とパーセント表記 (`45.0%`) の自動識別と規格化。

---

## 6. 改訂履歴 (Revision History)

- **v1.0 (2026-09-20)**: 初版作成。妥当性検証4段階フレームワーク、EDINETハイブリッド、FAST/THOROUGH モード選択オプションの仕様追加。
