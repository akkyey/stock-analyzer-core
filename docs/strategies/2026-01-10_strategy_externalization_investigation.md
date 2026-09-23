# 調査結果報告書: 戦略定義の完全YAML外部化

## 1. 既存ロジックの分析

### 1.1 現状の実装
- **GenericStrategy (`src/calc/strategies/generic.py`)**:
  - スコアリング計算のコアロジックを実装。
  - しかし、指標の「方向性（低い方が良いか高い方が良いか）」や「カテゴリ（Value/Growth/Trend）」の判定において、`src/constants.py` の `METRIC_CATEGORY` や `LOWER_IS_BETTER_DEFAULTS` に依存しています。
  - `_evaluate_metric_vectorized` メソッド内で `rsi_oversold` や `rsi_overbought` などの特定名称に対するハードコードされた分岐が存在します。

- **ValueStrictStrategy (`src/calc/strategies/value_strict.py`)**:
  - `GenericStrategy` を継承し、単に `STRATEGY_NAME` を固定しているだけのクラスです。実質的なロジックはすべて `GenericStrategy` にあります。
  - `STRATEGY_REGISTRY` に登録されているため、クラスとしてインスタンス化されています。

- **ScoringEngine (`src/calc/engine.py`)**:
  - `STRATEGY_REGISTRY` に `value_strict` 等のキーでクラスが登録されており、これがある戦略はクラスをインスタンス化、ない場合は `GenericStrategy` を使用する分岐になっています。

### 1.2 設定スキーマ (`src/config_schema.py`)
- 現在の `StrategyConfig` は `points: Dict[str, int]` および `thresholds: Dict[str, float]` という単純な構造です。
- 指標ごとのメタデータ（方向性、カテゴリ）を保持する場所がありません。

## 2. 拡張案の策定

### 2.1 Config Schema の拡張
`config.yaml` 内の各戦略定義において、各指標の振る舞いを定義可能にします。
`points` キーを単純な `Dict[str, int]` から、よりリッチな定義も許容する構造、あるいは別途 `metrics_metadata` セクションを追加する方式を提案します。
互換性を維持するため、既存の `points` はそのままにし、オプションで `metrics_metadata` を追加するのが安全です。

**提案スキーマ:**
```python
class MetricMetadata(BaseModel):
    direction: str = "higher"  # 'higher' or 'lower'
    category: str = "quality"  # 'value', 'growth', 'trend', 'quality'

class StrategyConfig(BaseModel):
    # ... 既存フィールド ...
    metrics_metadata: Dict[str, MetricMetadata] = {}
```

### 2.2 GenericStrategy の汎用化
`GenericStrategy` の計算ループを変更し、定数ファイル (`constants.py`) への依存を排除（またはフォールバック化）します。

1.  指標 `M` についてループ。
2.  `metrics_metadata` から `M` の情報を取得。ない場合は `constants.py` のデフォルトを使用。
3.  `direction` が `lower` ならば「値 <= 閾値」で判定、`higher` ならば「値 >= 閾値」で判定。
4.  `category` に基づいて `score_value` や `score_growth` などのサブスコアに加算。

### 2.3 PromptBuilder の動的認識
- 現在の `PromptBuilder.prepare_variables` は特定のフィールドのみを辞書に入れています。これを改善し、`row` に含まれるすべてのデータを変数として利用可能にします。
- さらに、戦略で定義されているがプロンプトテンプレートに含まれていない指標を検知し、`metrics_section` の末尾に自動的に `"- {MetricName}: {Value}"` の形式で追記するロジックを追加します。これにより、YAML で新しい指標を追加するだけで、AIプロンプトにもその指標が反映されるようになります。

## 3. 影響範囲とリスク

- **影響範囲**: `ScoringEngine`, `GenericStrategy`, `BaseCalculator`, `PromptBuilder`, `config_schema.py`。
- **レガシー戦略**: `ValueStrictStrategy` クラスを削除し、`config.yaml` の定義のみで動作するように `ScoringEngine` のレジストリから登録を解除します。
- **結果整合性**: ロジックが正しく移行できていれば、計算結果は $10^{-4}$ の精度で完全に一致するはずです。

## 4. 実行計画 (Next Steps)
ユーザーの承認が得られ次第、Phase 2 に移行し、実装を開始します。
