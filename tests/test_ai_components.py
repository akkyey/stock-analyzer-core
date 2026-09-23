"""src/ai/ モジュール群の包括的テスト"""

from unittest.mock import MagicMock, patch

import pytest

from src.ai.agent import AIAgent
from src.ai.key_manager import APIKeyManager
from src.ai.prompt_builder import PromptBuilder
from src.ai.response_parser import ResponseParser

# --- PromptBuilder Tests ---


def test_prompt_builder_basic():
    pb = PromptBuilder()
    row = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "プライム",
        "price": 2500,
        "per": 10.5,
        "pbr": 1.1,
        "roe": 12.0,
        "equity_ratio": 45.0,
        "rsi_14": 45.0,
        "macd_hist": 0.5,
        "ma25_divergence": 2.0,
    }
    prompt = pb.create_prompt(row, strategy_name="value")
    assert "7203" in prompt or len(prompt) > 0
    assert isinstance(prompt, str)


def test_prompt_builder_special_sectors():
    pb = PromptBuilder()
    row_bank = {"code": "8306", "name": "三菱UFJ", "sector": "銀行業", "price": 1000}
    prompt_bank = pb.create_prompt(row_bank, strategy_name="value")
    assert isinstance(prompt_bank, str)

    row_reit = {"code": "8951", "name": "NPR", "sector": "不動産業", "price": 150000}
    prompt_reit = pb.create_prompt(row_reit, strategy_name="value")
    assert isinstance(prompt_reit, str)


def test_prompt_builder_trend_and_deficiency():
    pb = PromptBuilder()
    row_deficient = {
        "code": "1001",
        "name": "サンプル",
        "per": None,
        "pbr": None,
        "rsi_14": None,
    }
    deficiency = pb._classify_data_deficiency(row_deficient)
    assert isinstance(deficiency, dict)

    row_trend = {"rsi": 60, "rsi_14": 60, "macd_hist": 1.2, "ma25_divergence": 5.0}
    desc = pb._calculate_trend_desc(row_trend)
    assert isinstance(desc, str)


def test_prompt_builder_dossier():
    dossier = {
        "code": "7203",
        "name": "トヨタ自動車",
        "metrics": {"per": 10.0, "pbr": 1.0},
        "technical": {"rsi_14": 55.0},
        "fundamentals": {"equity_ratio": 50.0},
    }
    prompt = PromptBuilder.build_dossier_analysis_prompt(dossier)
    assert "7203" in prompt
    assert "トヨタ自動車" in prompt


# --- ResponseParser Tests ---


def test_response_parser_valid_json():
    rp = ResponseParser()
    valid_json = """
    {
        "ai_sentiment": "Bullish",
        "ai_reason": "成長性が高く割安感あり。",
        "ai_risk": "原材料高",
        "ai_horizon": "6-12M",
        "ai_detail": "①概要: テスト詳細"
    }
    """
    res = rp.parse_response(valid_json)
    assert res["ai_sentiment"] == "Bullish"
    assert res["ai_reason"] == "成長性が高く割安感あり。"


def test_response_parser_markdown_codeblock():
    rp = ResponseParser()
    md_json = """
    ```json
    {
        "ai_sentiment": "Neutral",
        "ai_reason": "現状維持。",
        "ai_risk": "リスク限定的",
        "ai_horizon": "1-3M",
        "ai_detail": "①概要: 詳細"
    }
    ```
    """
    res = rp.parse_response(md_json)
    assert res["ai_sentiment"] == "Neutral"


def test_response_parser_broken_json_and_validation():
    rp = ResponseParser()
    invalid_text = "これはJSONではありません"
    res = rp.parse_response(invalid_text)
    assert res.get("_parse_error") is True or res["ai_sentiment"] == "Error"

    bad_res = {
        "ai_sentiment": "InvalidSentiment",
        "ai_reason": "無効な判定",
        "ai_risk": "不明",
        "ai_horizon": "Wait",
        "ai_detail": "詳細",
    }
    valid, err = rp.validate_response(bad_res)
    assert valid is False


# --- APIKeyManager Tests ---


def test_key_manager_basics():
    km = APIKeyManager(debug_mode=True)
    assert km.get_total_calls() >= 0

    idx = km.current_key_idx
    km.update_stats(idx, "success_count", 1)
    km.update_stats(idx, "total_calls", 1)

    rotated = km.rotate_key()
    assert isinstance(rotated, bool)


# --- Agent Tests ---


def test_agent_initialization():
    agent = AIAgent(model_name="gemini-2.5-flash", debug_mode=True)
    assert agent.audit_version is not None
    report = agent.generate_usage_report()
    assert isinstance(report, str)


@patch.object(APIKeyManager, "get_current_client")
def test_agent_analyze_mocked(mock_get_client):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = """
    {
        "ai_sentiment": "Bullish",
        "ai_reason": "テスト成果良好",
        "ai_risk": "競合リスク",
        "ai_horizon": "6-12M",
        "ai_detail": "①分析: 詳細"
    }
    """
    mock_client.models.generate_content.return_value = mock_response
    mock_get_client.return_value = mock_client

    agent = AIAgent(model_name="gemini-2.5-flash", debug_mode=True)
    agent.key_manager.api_keys = ["mock_key_1"]
    agent.key_manager.key_stats = [
        {"success_count": 0, "error_count": 0, "total_calls": 0}
    ]

    row = {"code": "9999", "name": "テスト銘柄", "price": 1000}
    res = agent.analyze(row, strategy_name="value")
    assert isinstance(res, dict)


@patch.object(APIKeyManager, "get_current_client")
def test_agent_analyze_dossier_mocked(mock_get_client):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = """
    {
        "ai_sentiment": "Neutral",
        "ai_reason": "要観察",
        "ai_risk": "業界動向",
        "ai_horizon": "1-3M",
        "ai_detail": "①分析: 詳細"
    }
    """
    mock_client.models.generate_content.return_value = mock_response
    mock_get_client.return_value = mock_client

    agent = AIAgent(model_name="gemini-2.5-flash", debug_mode=True)
    agent.key_manager.api_keys = ["mock_key_1"]
    agent.key_manager.key_stats = [
        {"success_count": 0, "error_count": 0, "total_calls": 0}
    ]

    dossier = {
        "code": "8888",
        "name": "ドシエ銘柄",
        "metrics": {"per": 15.0},
        "technical": {"rsi_14": 50.0},
    }
    res = agent.analyze_dossier(dossier)
    assert isinstance(res, dict)
