"""E2E 総合テスト: エージェント協調型評価パイプライン

データ取得・財務修復・指標補完 → EvaluationPhase (一次足切り & StockDossier構築) 
→ AIAgent (Dossier分析 & 最終ランク付け) の End-to-End 連携を総合検証する。
"""

import json
from unittest.mock import MagicMock, patch
import polars as pl
import pytest

from src.ai.agent import AIAgent
from src.orchestration.phases.evaluation import EvaluationPhase
from tests.helpers.stubs import StubOrchestratorContext


class TestAgenticPipelineE2E:
    """エージェント協調型評価パイプラインの総合テストスイート"""

    @pytest.fixture
    def setup_pipeline_data(self):
        """パイプライン入力用のモック市場データとコンテキストを準備"""
        config = {
            "system": {"use_polars": True},
            "strategies": {
                "Balanced Strategy": {
                    "base_score": 50.0,
                    "points": {"roe": 10.0},
                    "thresholds": {"roe": 10.0}
                }
            }
        }
        context = StubOrchestratorContext(config)

        # 3銘柄のサンプルデータ
        # 1. 7203: トヨタ (優良・割安・健全) -> 足切り通過 & 高評価
        # 2. 9984: ソフトバンクG (高モメンタム) -> 足切り通過
        # 3. 9999: ボロ株 (価格 10円, 出来高極小) -> 一次足切りで除外
        df_metrics = pl.DataFrame({
            "code": ["7203", "9984", "9999"],
            "entry_date": ["2026-03-27", "2026-03-27", "2026-03-27"],
            "close_price": [2850.0, 8500.0, 10.0],
            "price": [2850.0, 8500.0, 10.0],
            "rsi_14": [28.5, 72.0, 50.0],
            "volume_ratio": [2.5, 1.8, 0.1],
            "ma_divergence": [-5.2, 8.4, 0.0],
            "macd": [12.0, 45.0, 0.0],
            "macd_signal": [8.0, 30.0, 0.0],
            "trend_signal": [3, 2, 0],
            "volume_20d_avg": [5000000.0, 3000000.0, 100.0],
            "per": [10.2, 25.0, None],
            "pbr": [1.1, 1.8, None],
            "roe": [14.5, 8.2, -5.0],
            "equity_ratio": [55.0, 35.0, -10.0],
            "dividend_yield": [2.8, 1.5, 0.0],
            "market_cap": [350000.0, 120000.0, 50.0],
            "sales": [450000.0, 60000.0, 100.0],
            "operating_income": [45000.0, 8000.0, -50.0],
            "operating_margin": [10.0, 13.3, -50.0],
            "net_profit": [35000.0, 5000.0, -60.0],
            "profit_growth_raw": [15.2, 5.0, -20.0],
            "is_turnaround": [False, False, False],
        })

        df_stocks = pl.DataFrame({
            "code": ["7203", "9984", "9999"],
            "name": ["トヨタ自動車", "ソフトバンクグループ", "ペニー株"],
            "sector": ["輸送用機器", "情報・通信業", "サービス業"],
            "market": ["Prime", "Prime", "Standard"],
            "status": ["active", "active", "active"],
        })

        return context, df_metrics, df_stocks

    def test_e2e_pipeline_flow(self, setup_pipeline_data):
        """【総合検証】EvaluationPhase から Dossier 抽出、AIAgent バッチ評価までの一気通貫テスト"""
        context, df_metrics, df_stocks = setup_pipeline_data

        # context のモック設定
        context.duck_repo.load_metrics.return_value = df_metrics
        context.duck_repo.load_stocks.return_value = df_stocks

        # ScoringEngine は足切り通過銘柄を模倣
        with patch("src.calc.engine.ScoringEngine") as MockEngine, \
             patch("src.repositories.fundamentals_repository.FundamentalsRepository") as MockFunda:

            mock_engine_inst = MockEngine.return_value
            def mock_calc_score(df, strategy_name):
                # 一次足切り (ボロ株除外: price >= 100, volume_20d_avg >= 1000)
                filtered = df.filter((pl.col("price") >= 100.0) & (pl.col("volume_20d_avg") >= 1000.0))
                return filtered.with_columns([
                    pl.lit(80.0).alias("quant_score"),
                    pl.lit(1).alias("rank"),
                    pl.lit(strategy_name).alias("strategy_name")
                ])
            mock_engine_inst.calculate_score.side_effect = mock_calc_score
            MockFunda.return_value.get_all_pl.return_value = pl.DataFrame()

            # 1. EvaluationPhase の実行
            phase = EvaluationPhase(context)
            ranked_df = phase.execute(data_map=None)

            # 一次足切りの検証
            assert ranked_df is not None
            passed_codes = ranked_df["code"].to_list()
            assert "7203" in passed_codes
            assert "9984" in passed_codes

            # context.stock_dossiers にカルテが格納されていることを確認
            assert hasattr(context, "stock_dossiers")
            dossiers = context.stock_dossiers
            assert len(dossiers) >= 2
            dossier_codes = [d["code"] for d in dossiers]
            assert "7203" in dossier_codes
            assert "9984" in dossier_codes

            # カルテ内容の検証 (トヨタ)
            toyota_dossier = next(d for d in dossiers if d["code"] == "7203")
            assert toyota_dossier["name"] == "トヨタ自動車"
            assert toyota_dossier["fundamentals"]["per"] == 10.2
            assert toyota_dossier["technicals"]["rsi_14"] == 28.5
            assert any("RSI売られすぎ" in r for r in toyota_dossier["trigger_reasons"])
            assert any("高ROE" in r for r in toyota_dossier["trigger_reasons"])

        # 2. AIAgent による StockDossier バッチ分析と最終ランク付け
        with patch("src.ai.agent.AIAgent._generate_content_with_retry") as mock_gen:
            # AI のレスポンスをモック
            def mock_ai_response(prompt):
                mock_resp = MagicMock()
                if "7203" in prompt:
                    mock_resp.text = json.dumps({
                        "code": "7203",
                        "verdict": "STRONG_BUY",
                        "agent_score": 92.0,
                        "investment_thesis": "低PBR・割安水準かつ健全な財務基盤。RSI反発モメンタム良好。",
                        "risk_factors": ["為替変動リスク"],
                        "time_horizon": "Swing (2〜6週)",
                    })
                else:
                    mock_resp.text = json.dumps({
                        "code": "9984",
                        "verdict": "BUY",
                        "agent_score": 78.5,
                        "investment_thesis": "高い成長性はあるがボラティリティに注意。",
                        "risk_factors": ["市場全体のテック株下落"],
                        "time_horizon": "Short (1〜2週)",
                    })
                return mock_resp, 1

            mock_gen.side_effect = mock_ai_response

            agent = AIAgent("gemini-1.5-flash", interval_sec=0, debug_mode=False)
            agent_results = agent.analyze_dossiers_batch(dossiers)

            # 最終結果の検証
            assert len(agent_results) == 2

            # スコア順にソートされ、Rank が付与されていること
            top_1 = agent_results[0]
            top_2 = agent_results[1]

            assert top_1["code"] == "7203"
            assert top_1["rank"] == 1
            assert top_1["agent_score"] == 92.0
            assert top_1["verdict"] == "STRONG_BUY"
            assert "低PBR・割安水準" in top_1["investment_thesis"]

            assert top_2["code"] == "9984"
            assert top_2["rank"] == 2
            assert top_2["agent_score"] == 78.5
            assert top_2["verdict"] == "BUY"
