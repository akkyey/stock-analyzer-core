# 投資戦略設定ガイド (Strategy Configuration Guide)

## 概要
本ガイドでは、`config.yaml` を使用して新しい投資戦略を定義したり、既存の戦略を調整したりする方法について解説します。
[v9.0] より、Pythonコードを書くことなく、設定ファイルの記述のみで新しい指標や評価ロジックを導入できるようになりました。

---

## 1. 戦略の基本構造
`strategies` セクションの下に、戦略固有のID（例: `my_custom_strategy`）をキーとして設定を記述します。

```yaml
strategies:
  my_custom_strategy:
    metrics_metadata: # [v9.0] 指標の定義 (方向性とカテゴリ)
      ...
    points:           # 配点設定
      ...
    thresholds:       # 閾値設定
      ...
    base_score: 50    # 基礎点
    default_style: "value_balanced" # スタイル (重み付け)
```

---

## 2. 指標メタデータ (`metrics_metadata`)
各指標が「低い方が良いのか、高い方が良いのか」および「どのカテゴリ（Value, Growth等）に属するか」を定義します。

### 指定可能なプロパティ
*   **direction**: スコア判定の方向性
    *   `"higher"`: 値が閾値 **以上** であれば加点 (例: ROE, 成長率)
    *   `"lower"`: 値が閾値 **以下** であれば加点 (例: PER, PBR)
*   **category**: スコアの集計先サブカテゴリ
    *   `"value"`: 割安性スコア (Score Value)
    *   `"growth"`: 成長性スコア (Score Growth)
    *   `"quality"`: 財務健全性・質スコア (Score Quality)
    *   `"trend"`: テクニカル・モメンタムスコア (Score Trend)

### 設定例
```yaml
metrics_metadata:
  # 高い方が良い指標 (Higher is Better)
  roe: {direction: "higher", category: "quality"}
  sales_growth: {direction: "higher", category: "growth"}
  
  # 低い方が良い指標 (Lower is Better)
  per: {direction: "lower", category: "value"}
  pbr: {direction: "lower", category: "value"}
  
  # カスタム指標の例
  my_custom_ratio: {direction: "lower", category: "value"}
```

---

## 3. 配点と閾値の設定 (`points` / `thresholds`)
実際に評価に使用する指標と、その基準値を設定します。

### 設定ルール
1.  **thresholds**: 加点条件となる閾値を指定します。
    *   `direction: "higher"` の場合: `実数値 >= 閾値` で加点
    *   `direction: "lower"` の場合: `実数値 <= 閾値` で加点
2.  **points**: 条件を満たした場合に加算されるポイントを指定します。

### 設定例
```yaml
points:
  roe: 20           # ROEが基準以上なら +20点
  per: 10           # PERが基準以下なら +10点
  my_custom_ratio: 30 # カスタム指標が基準以下なら +30点

thresholds:
  roe: 8.0          # ROE 8.0% 以上
  per: 15.0         # PER 15.0倍 以下
  my_custom_ratio: 1.5
```

---

## 4. 新しい指標の追加手順
新しい指標（CSVのカラム名）を評価対象に加えたい場合は、以下の手順のみで完了します。

1.  **CSVマッピングの確認**:
    *   `csv_mapping` セクションで、入力CSVの列名が適切なキー名（例: `my_new_metric`）にマッピングされていることを確認してください。
2.  **metrics_metadata の定義**:
    *   その指標の `direction` と `category` を定義します。
3.  **points / thresholds の追加**:
    *   評価基準と配点を設定します。

**これだけで、GenericStrategy が自動的に計算を行い、AIプロンプトにもその指標が追加されます。**

---

## 5. スタイルの設定 (`default_style`)
計算された「ファンダメンタルズスコア (`fund_score`)」と「テクニカルスコア (`score_trend`)」の統合比率を指定します。
定義済みのスタイルは `scoring_v2.styles` にあります。

*   **value_balanced**: Fund 70% / Tech 30% (標準)
*   **long_term_growth**: Fund 90% / Tech 10% (長期投資向け)
*   **short_term_momentum**: Fund 30% / Tech 70% (短期トレード向け)

---

## 6. 設定の全体例

```yaml
strategies:
  # 新しいグロース重視戦略の例
  super_growth_strategy:
    base_score: 20
    default_style: "long_term_growth"
    persona: "Aggressive Growth Investor"
    
    metrics_metadata:
      sales_growth: {direction: "higher", category: "growth"}
      profit_growth: {direction: "higher", category: "growth"}
      peg_ratio: {direction: "lower", category: "value"}
      
    points:
      sales_growth: 40  # 売上成長を最重視
      profit_growth: 30
      peg_ratio: 10
      
    thresholds:
      sales_growth: 20.0 # 20%成長以上
      profit_growth: 15.0
      peg_ratio: 1.5
```
