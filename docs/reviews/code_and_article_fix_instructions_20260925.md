# プログラムおよび記事改修指示書 (2026-09-25)

本書は、Qiita記事（前編・後編）および基盤プログラムに対する外部レビュー指摘事項に基づき、Agy IDEで改修作業を実施するための具体的な手順・コード修正仕様・検証コマンドを定義した指示書である。

---

## 📌 目次
1. [改修の背景と目的](#1-改修の背景と目的)
2. [改修仕様【プログラム編】](#2-改修仕様プログラム編)
   - [Task 1-1: `PreFilter` における市場データ欠損 (None) のスルー防止](#task-1-1-prefilter-における市場データ欠損-none-のスルー防止)
   - [Task 1-2: `PreFilter` における債務超過と未開示の判定順序是正](#task-1-2-prefilter-における債務超過と未開示の判定順序是正)
   - [Task 1-3: `DataContractGuard` における必須列存在チェックの強化](#task-1-3-datacontractguard-における必須列存在チェックの強化)
   - [Task 1-4: `FinancialRepairService` の自己資本比率直接計算対応（推奨）](#task-1-4-financialrepairservice-の自己資本比率直接計算対応推奨)
   - [Task 1-5: テストケースの追加と更新](#task-1-5-テストケースの追加と更新)
3. [改修仕様【記事編】](#3-改修仕様記事編)
   - [前編: ベンチマーク比較条件の適正化と前作記述の整合](#前編-qiita_part1_data_acquisitionmd)
   - [後編: 表現の適正化・除外内訳表の追加・免責事項の明記](#後編-qiita_part2_quant_gatekeepermd)
4. [検証・動作確認手順](#4-検証動作確認手順)

---

## 1. 改修の背景と目的

Qiita記事公開前のレビューにおいて、以下のクリティカルな課題が指摘された。
1. **プログラムの境界値ロジックの穴**:
   - `PreFilter` で売買代金（`avg_tv`）や出来高ゼロ日数（`zero_days`）が `None` の場合、条件をスルーして適格側（`daily_report.csv`）へ漏出する
   - 債務超過チェックの手前で自己資本比率未開示チェックが走るため、純資産マイナス企業が「未開示」に誤分類される
   - データ契約保証で、DataFrameに必須列自体が存在しない場合にスルーされる
2. **ベンチマークと記事表現の乖離**:
   - 前回（351社）と今回（188社）でDL社数が異なるのに「44%短縮・1.8倍高速化」と過大に表現されている（公平な指標は1社あたり1.31秒 ➔ 1.01秒 / 23%短縮）
   - 「0.15秒で25倍高速化」と謳いながら中身は行ループ（`to_dicts`）である点、および見出し「指標間の恒等式違反」と内容（流動性）の不一致
   - 金融商品取引法上の配慮（投資助言ではない免責文）の欠落、未公開リンク（`#`）の放置

これらを迅速・確実に是正し、技術的信頼性の高いコードベースと記事を完成させる。

---

## 2. 改修仕様【プログラム編】

### Task 1-1: `PreFilter` における市場データ欠損 (None) のスルー防止
- **対象ファイル**: `src/calc/pre_filter.py`
- **問題箇所**: 256〜289行目付近
- **修正内容**:
  `avg_tv`（20日平均売買代金）、`zero_days`（5日出来高ゼロ日数）、`latest_trade_date`（最終取引日）が `None` の場合、現在は `if avg_tv is not None and avg_tv < min_tv_20d:` 等となっており足切りをすり抜ける。これらを安全に除外隔離する。

#### 修正仕様
```python
# ==============================================================================
# 修正前 (src/calc/pre_filter.py:256-289 付近)
# ==============================================================================
            # 1. データ鮮度不足判定（最新市場日より大幅に古い取引停止銘柄）
            if is_recent_trade is False and latest_trade_date is not None:
                rejected_rows.append(...)
                continue

            # 2. 売買不能判定（直近5営業日出来高ゼロ）
            if zero_days is not None and zero_days > 0:
                rejected_rows.append(...)
                continue

            # 3. 極小流動性トラップ判定（20日平均売買代金 < 3,000万円）
            if avg_tv is not None and avg_tv < min_tv_20d:
                rejected_rows.append(...)
                continue

# ==============================================================================
# 修正後
# ==============================================================================
            # 1. 取引日・市場データ欠損判定（最終取引日が存在しない、または取引停止）
            if latest_trade_date is None or is_recent_trade is False:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "データ鮮度不足 (取引停止)",
                        "filter_detail": (
                            "最終取引日データ欠損"
                            if latest_trade_date is None
                            else f"最終取引日 ({latest_trade_date}) が直近5営業日範囲外"
                        ),
                    }
                )
                continue

            # 2. 売買不能判定（直近5営業日出来高データ欠損、または出来高ゼロ日あり）
            if zero_days is None or zero_days > 0:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "商い不成立",
                        "filter_detail": (
                            "出来高時系列データ欠損"
                            if zero_days is None
                            else f"直近5営業日以内に出来高ゼロ日あり ({zero_days}日)"
                        ),
                    }
                )
                continue

            # 3. 極小流動性トラップ判定（売買代金欠損、または20日平均売買代金 < 3,000万円）
            if avg_tv is None or avg_tv < min_tv_20d:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "極小流動性トラップ",
                        "filter_detail": (
                            "売買代金データ欠損/算出不能"
                            if avg_tv is None
                            else f"20日平均売買代金不足 ({avg_tv / 10_000.0:,.0f}万円 < {min_tv_20d / 10_000.0:,.0f}万円)"
                        ),
                    }
                )
                continue
```

---

### Task 1-2: `PreFilter` における債務超過と未開示の判定順序是正
- **対象ファイル**: `src/calc/pre_filter.py`
- **問題箇所**: 313〜333行目付近
- **修正内容**:
  純資産マイナス（`net_assets <= 0`）または `equity_ratio <= min_eq_ratio` を判定する前に `equity_ratio is None` を判定しているため、修復されず `equity_ratio` が `None` のまま残った債務超過銘柄が「未開示」に誤分類される。順序を是正し、純資産情報（`net_assets`）も利用する。

#### 修正仕様
```python
# ==============================================================================
# 修正前
# ==============================================================================
            # 6. 重要財務指標未開示 / 算出不能判定（自己資本比率等の必須財務データ欠損）
            if equity_ratio is None:
                ...
                continue

            # 7. 構造的破綻（債務超過判定）
            if equity_ratio <= min_eq_ratio:
                ...
                continue

# ==============================================================================
# 修正後
# ==============================================================================
            # 6. 構造的破綻（債務超過判定）: 純資産マイナス または 自己資本比率 <= 0
            net_assets = row.get("net_assets")
            is_insolvent = False
            insolvency_detail = ""

            if net_assets is not None and net_assets <= 0:
                is_insolvent = True
                insolvency_detail = f"純資産マイナス ({net_assets:,.0f}円)"
            elif equity_ratio is not None and equity_ratio <= min_eq_ratio:
                is_insolvent = True
                insolvency_detail = f"自己資本比率マイナス/ゼロ ({equity_ratio:.1f}% <= {min_eq_ratio:.1f}%)"

            if is_insolvent:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "構造的破綻 (債務超過)",
                        "filter_detail": insolvency_detail,
                    }
                )
                continue

            # 7. 重要財務指標未開示 / 算出不能判定（自己資本比率等の必須財務データ欠損）
            if equity_ratio is None:
                rejected_rows.append(
                    {
                        **row,
                        "filter_reason": "重要指標未開示/算出不能",
                        "filter_detail": "自己資本比率等の財務諸表データ未開示または欠損",
                    }
                )
                continue
```

---

### Task 1-3: `DataContractGuard` における必須列存在チェックの強化
- **対象ファイル**: `src/orchestration/phases/integration.py`
- **問題箇所**: 162〜175行目
- **修正内容**:
  `check_cols = [c for c in essential_cols if c in cols]` だと、必須列自体が存在しない場合にスルーしてしまう。`missing_cols` を明示的にチェックし、例外を投げる。

#### 修正仕様
```python
# ==============================================================================
# 修正前 (src/orchestration/phases/integration.py:162-175)
# ==============================================================================
        essential_cols = ["code", "price", "verdict"]
        cols = df.columns
        check_cols = [c for c in essential_cols if c in cols]

        if check_cols:
            null_exprs = [pl.col(c).is_null().sum().alias(c) for c in check_cols]
            null_counts = df.select(null_exprs).to_dicts()[0]
            invalid_cols = {col: count for col, count in null_counts.items() if count > 0}
            if invalid_cols:
                err_msg = f"Data Contract Violation: Null values detected in essential columns: {invalid_cols}"
                self.log_error(err_msg)
                raise AssertionError(err_msg)
            self.log_info(f"✅ Data contract verified: 0.00% missing values across essential columns ({check_cols}).")

# ==============================================================================
# 修正後
# ==============================================================================
        essential_cols = ["code", "price", "verdict"]
        cols = set(df.columns)
        missing_cols = set(essential_cols) - cols

        if missing_cols:
            err_msg = f"Data Contract Violation: Missing required essential columns in DataFrame: {sorted(missing_cols)}"
            self.log_error(err_msg)
            raise AssertionError(err_msg)

        null_exprs = [pl.col(c).is_null().sum().alias(c) for c in essential_cols]
        null_counts = df.select(null_exprs).to_dicts()[0]
        invalid_cols = {col: count for col, count in null_counts.items() if count > 0}
        if invalid_cols:
            err_msg = f"Data Contract Violation: Null values detected in essential columns: {invalid_cols}"
            self.log_error(err_msg)
            raise AssertionError(err_msg)
        self.log_info(f"✅ Data contract verified: 0.00% missing values across essential columns ({essential_cols}).")
```

---

### Task 1-4: `FinancialRepairService` の自己資本比率直接計算対応（推奨）
- **対象ファイル**: `src/services/financial_repair.py`
- **問題箇所**: 24〜37行目
- **修正内容**:
  `net_assets`（純資産）と `total_assets`（総資産）が存在する場合、D/Eレシオ逆算に先立って `(net_assets / total_assets) * 100` で直接算出する。

#### 修正仕様
```python
        # [Step 1] 自己資本比率 (Equity Ratio) の精緻化
        # 1-1. 総資産と純資産による直接計算 (最優先)
        if "total_assets" in cols and "net_assets" in cols and "equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        pl.col("equity_ratio").is_null()
                        & pl.col("total_assets").is_not_null()
                        & (pl.col("total_assets") > 0)
                        & pl.col("net_assets").is_not_null()
                    )
                    .then((pl.col("net_assets") / pl.col("total_assets")) * 100.0)
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )

        # 1-2. D/Eレシオ (総負債ベース/有利子負債ベース) からの逆算補完
        if "equity_ratio" in cols and "debt_equity_ratio" in cols:
            df = df.with_columns(
                [
                    pl.when(
                        pl.col("equity_ratio").is_null()
                        & pl.col("debt_equity_ratio").is_not_null()
                        & (pl.col("debt_equity_ratio") > 0)
                    )
                    .then(100.0 / (1.0 + (pl.col("debt_equity_ratio") / 100.0)))
                    .otherwise(pl.col("equity_ratio"))
                    .alias("equity_ratio")
                ]
            )
```

---

### Task 1-5: テストケースの追加と更新
- **対象ファイル**: `tests/test_pre_filter.py`, `tests/orchestration/test_integration_phase.py`
- **追加テスト項目**:
  1. `test_pre_filter_rejects_missing_liquidity_data()`: `avg_trading_value_20d` や `zero_volume_days_5d` が `None` の銘柄が正しく除外されることを検証。
  2. `test_pre_filter_insolvency_priority_over_unopened()`: 純資産がマイナスの銘柄が「重要指標未開示」ではなく「構造的破綻 (債務超過)」に分類されることを検証。
  3. `test_data_contract_guard_raises_on_missing_column()`: 必須列（`price` 列など）が存在しない DataFrame を渡した際に `AssertionError` がスローされることを検証。

---

## 3. 改修仕様【記事編】

### 前編: `qiita_part1_data_acquisition.md`

#### 1. ベンチマーク比較の主役を「1社あたり処理速度」に是正
- **問題**: 前回（351社）と今回（188社）で社数が異なるのに「44%短縮・1.8倍高速化」と過大に比較している。
- **修正方針**:
  - 主役を「1社あたり **1.31秒 ➔ 1.01秒（約23%高速化）**」に切り替える。
  - 総合所要時間（5分08秒）は「当日の開示188社における実稼働一例（参考値）」と位置づけ、注記を明記する。
  - 「同条件対比」という表現を改め、「処理社数および1社あたりの実効速度の推移」とする。

#### 2. 前作の記述の整合（30日スキャン vs 単日スキャン）
- **問題**: 第1章で前回を「直近30日間の新着書類(351社)」と書き、対比表で「単日スキャン」と書いている。
- **修正方針**:
  - 「前作では日次で前日分のみを同期する運用だったが、取りこぼし対策として30日分を愚直にダウンロードしようとすると351社・7分42秒を要していた。今回は毎朝30日分のメタデータをスキャンしながら、取得済み有報のダウンロードをスキップするキャッシュ判定を導入した」という文脈に統一する。

#### 3. D/Eレシオ逆算の理由と定義の注記
- **修正方針**:
  - EDINETから直接自己資本比率タグが取れなかった特殊フォーマット時のフォールバックとしてD/Eレシオから逆算している背景を明記する。また、数式 `100 / (1 + DE/100)` は「総負債 / 純資産」をベースとした恒等式に基づくことを明記する。

---

### 後編: `qiita_part2_quant_gatekeeper.md`

#### 1. レトリックの抑制とPolars実装への誠実な説明
- **問題**: 「0.15秒で25倍高速化」「Rustエンジンでミリ秒処理」と謳いながら、中身は `to_dicts()` の行ループである点。
- **修正方針**:
  - 「25倍高速化」という煽り表現を削除。
  - 「Polarsで時系列と財務データを結合した上で、Python側で多段ガードを適用し、3,920社を0.15秒（147ミリ秒）で判定。実用上十分なレスポンスでバッチ処理を完了させている」という客観的な説明に改める。

#### 2. 見出しの是正
- **問題**: 第2章3節「指標間の恒等式違反」の中身が流動性（超小型株の商い不成立等）の話になっている。
- **修正方針**:
  - 見出しを **「3. 時価総額と実効流動性の乖離（超小型株の流動性トラップ）」** に改める。

#### 3. 除外1,800社（46%）の内訳表の掲載
- **修正方針**:
  読者が最も関心を持つ実データとして、第6章に以下の内訳表を明記する：

| 除外判定理由（filter_reason） | 除外社数 | 主な除外詳細（filter_detail） |
| :--- | :---: | :--- |
| **商い不成立** | 739 社 | 直近5営業日以内に出来高ゼロ日が存在 |
| **データ鮮度不足 (取引停止)** | 440 社 | 最終取引日が直近5営業日範囲外（長期取引停止等） |
| **重要指標未開示/算出不能** | 251 社 | 自己資本比率・株価等の必須データ未開示 |
| **極小流動性トラップ** | 139 社 | 20日平均売買代金が3,000万円未満 |
| **市場データ取得不能** | 104 社 | yfinance等の市場価格（OHLCV）データ未取得 |
| **致命的キャッシュ枯渇** | 86 社 | 営業CFマージンが-10%未満の赤字流出（金融除く） |
| **超低位ボロ株** | 33 社 | 株価が50円未満 |
| **構造的破綻 (債務超過)** | 8 社 | 純資産マイナスまたは自己資本比率 <= 0% |
| **除外銘柄 合計** | **1,800 社** | **全3,920社中 45.9% をスコアリング前に隔離** |

#### 4. 免責文（投資助言ではない旨）の追加と `#` リンクの整理
- **免責文**: 記事末尾（または冒頭）に以下を明記する。
  > ⚠️ **免責事項（Disclaimer）**: 本記事はデータエンジニアリングおよびパイプライン設計の技術的知見を共有するものであり、特定の有価証券の売買推奨や投資助言を目的としたものではありません。掲載されている判定結果やスクリーニングルールは学習・検証用途であり、投資判断はご自身の責任において行ってください。
- **有料リンク整理**: `[準備中](#)` となっているリンクは、公開されるまで一時的に削除するか、「※本分析基盤と連動するAIエージェントのプロンプト構成は、後日別記事にて公開予定です」等の注記に差し替える。

---

## 4. 検証・動作確認手順

改修完了後、以下の手順で動作確認を実施する。

### 1. ローカルでのテスト実行
```bash
# テストの実行 (105件以上の全パス確認)
pytest tests/ -v

# コード規約チェック
ruff check src/ tests/
```

### 2. 実際のCSV出力検証
```bash
# 検証ツールの実行
python -m src.tools.verify_csv_outputs
```
- **確認事項**:
  - `output/daily_report.csv` と `output/uncalculable_stocks.csv` の合計社数が **3,920社** と完全に一致すること。
  - 重複銘柄がゼロであること。
  - `daily_report.csv` の必須列（`code`, `price`, `verdict`）に欠損値がゼロであること。

### 3. リモート実機 (`masaaki-sv`) への同期と確認
```bash
# 修正したファイルを masaaki-sv へ同期
rsync -avz src/ tests/ docs/ masaaki-sv:/home/irom/dev/stock-analyzer-core/

# masaaki-sv 上でテストとリントを実行
ssh masaaki-sv "export PATH=\$HOME/.cargo/bin:\$HOME/.local/bin:\$PATH && cd /home/irom/dev/stock-analyzer-core && uv run ruff check src/ tests/ && uv run pytest tests/ -q"
```
