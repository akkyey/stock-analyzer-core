# 品質監査レポート (QMS Report): 2026-04-21

## 1. 障害の全列記
現在の実装および検証プロセスにおいて、以下の不具合が確認された。

- **E1: AttributeError (config)**
  - 現象: `EvaluationPhase` が `self.config` にアクセスしようとして失敗。
  - 状態: 修正済み（`self.context.config` へ移行）。
- **E2: AttributeError (collect)**
  - 現象: `pl.DataFrame` に対して `.collect()` を呼び出し、不具合が発生。
  - 状態: 未修正。
- **E3: ValueError (concat empty list)**
  - 現象: E2 の影響で全戦略計算が失敗し、空リストの結合を試みて落ちる。
  - 状態: 未修正（E2 に起因）。
- **E4: Test Script Misalignment (verify_logic_fast.py)**
  - 現象: テストコードが `BasePhase` の新しいコンストラクタ（`context` 要求）に追従できていない。
  - 状態: 未修正。

## 2. 根本原因 (Root Cause)
### A. 「具体化境界」の定義の揺らぎ
Turbo パイプラインの核心である「具体化（Materialization）」の境界が、ライブラリレベル (`PolarsEngine`) とオーケストレーションレベル (`EvaluationPhase`) で重複しており、**「どこまでが Lazy で、どこからが Eager なのか」というプロトコルが設計レベルで曖昧**になっていた。

### B. 暗黙の前提の書き換えによる回帰
`BasePhase` の依存性を `config` から `context` へ変更した際、プロダクトコードの修正に終止符を打ち、検証スクリプトという「品質の砦」の修正を疎かにした。これにより、テスト自体がコードの正しさを保証できない状態（False Negative）になっていた。

## 3. 傾向分析 (Trend)
一連の不具合には **「情報の権威 (Source of Truth) の不一致」** という共通パターンが見られる。
- 設定情報の権威：`config` or `context.config`?
- 実行状態の権威：`pl.DataFrame` or `pl.LazyFrame`?
これらがモジュール間で齟齬をきたした結果、静的型付けではない Python 環境においてランタイムエラーとして噴出した。

## 4. 弱点分析 (Weakness)
- **テストの孤立**: `verify_logic_fast.py` 等の検証スクリプトが、実際の `BasePhase` インターフェースから乖離しており、リファクタリングの影響を検知できなかった。
- **モックの深さ**: 本質的な「プランニングの巨大化」をテストで再現できておらず、論理（ロジック）のみの等価性確認に留まっていた。

## 5. ネクストアクション案
その場凌ぎのパッチ当て（`.collect()` の除去など）を排し、以下の設計レベルでの是正を提案する。

1.  **Scoring 契約の再定義 (`/deepen`)**:
    - `ScoringEngine` および `PolarsEngine` の戻り値を「常に物理 DataFrame」と定義し、型ヒントで明示する。
    - `EvaluationPhase` 側での不必要な `collect()` 呼び出しを構造的に排除する。
2.  **テストハーネスの統合**:
    - 孤立した検証スクリプトを `pytest` ベースの統合テスト環境へ吸収し、`BasePhase` への依存関係を常に最新に保つ。
3.  **コンテキストアクセスの統一**:
    - 全フェーズにおける設定アクセスを `self.context.config` に統一し、旧来の `self.config` への依存を完全に廃止する（ADR化）。

📄 **これらの分析に基づき、解決策の再設計を行うため、設計深化モード (`/deepen`) への移行を推奨する。**
