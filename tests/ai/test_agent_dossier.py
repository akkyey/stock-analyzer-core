import json
from unittest.mock import MagicMock, patch
from src.ai.agent import AIAgent


def test_analyze_dossier_debug_mode():
    agent = AIAgent("gemini-1.5-flash", interval_sec=0, debug_mode=True)
    dossier = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "Prime",
        "price": 3200.0,
    }

    result = agent.analyze_dossier(dossier)
    assert result["code"] == "7203"
    assert result["name"] == "トヨタ自動車"
    assert result["verdict"] == "BUY"
    assert result["agent_score"] == 85.0
    assert result["_mock"] is True


@patch("src.ai.agent.AIAgent._generate_content_with_retry")
def test_analyze_dossier_success(mock_generate):
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "code": "7203",
        "verdict": "STRONG_BUY",
        "agent_score": 92.5,
        "investment_thesis": "圧倒的な財務健全性と反発余地あり",
        "risk_factors": ["為替リスク"],
        "time_horizon": "Swing (2〜6週)",
    })
    mock_generate.return_value = (mock_resp, 1)

    agent = AIAgent("gemini-1.5-flash", interval_sec=0, debug_mode=False)
    dossier = {
        "code": "7203",
        "name": "トヨタ自動車",
        "sector": "輸送用機器",
        "market": "Prime",
        "price": 3200.0,
        "trigger_reasons": ["高ROE (14.2%)"],
        "technicals": {"rsi_14": 28.5},
        "fundamentals": {"per": 10.2},
    }

    result = agent.analyze_dossier(dossier)
    assert result["code"] == "7203"
    assert result["verdict"] == "STRONG_BUY"
    assert result["agent_score"] == 92.5
    assert "圧倒的な財務健全性" in result["investment_thesis"]


@patch("src.ai.agent.AIAgent._generate_content_with_retry")
def test_analyze_dossier_api_failure(mock_generate):
    mock_generate.return_value = (None, 3)

    agent = AIAgent("gemini-1.5-flash", interval_sec=0, debug_mode=False)
    dossier = {
        "code": "9984",
        "name": "ソフトバンクグループ",
    }

    result = agent.analyze_dossier(dossier)
    assert result["code"] == "9984"
    assert result["verdict"] == "WATCH"
    assert result.get("_analysis_failed") is True


@patch("src.ai.agent.AIAgent._generate_content_with_retry")
def test_analyze_dossiers_batch(mock_generate):
    def fake_generate(prompt):
        resp = MagicMock()
        if "1001" in prompt:
            resp.text = json.dumps({"code": "1001", "verdict": "BUY", "agent_score": 70.0})
        else:
            resp.text = json.dumps({"code": "1002", "verdict": "STRONG_BUY", "agent_score": 90.0})
        return resp, 1

    mock_generate.side_effect = fake_generate

    agent = AIAgent("gemini-1.5-flash", interval_sec=0, debug_mode=False)
    dossiers = [
        {"code": "1001", "name": "Corp A"},
        {"code": "1002", "name": "Corp B"},
    ]

    results = agent.analyze_dossiers_batch(dossiers)
    assert len(results) == 2
    # 降順ソートされているため 1002 (90.0) が rank 1, 1001 (70.0) が rank 2
    assert results[0]["code"] == "1002"
    assert results[0]["rank"] == 1
    assert results[1]["code"] == "1001"
    assert results[1]["rank"] == 2
