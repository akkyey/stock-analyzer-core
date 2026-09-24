from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from src.orchestration.phases.integration import IntegrationPhase
from tests.helpers.stubs import StubOrchestratorContext


@pytest.fixture
def stub_context():
    context = StubOrchestratorContext()
    context.reporter = MagicMock()
    context.notifier = MagicMock()
    return context


def test_integration_phase_execute_success(stub_context):
    """正常系: レポート生成と外部連携の呼び出しを確認"""
    # 入力データ
    df = pl.DataFrame({"code": ["1001"], "quant_score": [85.0]})

    # Reporter のモック設定
    stub_context.reporter.generate_reports.return_value = {"main_csv": "/tmp/dummy.csv"}

    # 外部連携のパッチ
    with (
        patch("os.path.exists") as MockExists,
        patch("os.path.getsize") as MockSize,
        patch("src.colab_tools.ColabTools.upload_file_to_drive") as MockUpload,
        patch.object(
            IntegrationPhase, "_upload_summary_to_gspread"
        ) as MockGspread,
    ):
        MockExists.return_value = True
        MockSize.return_value = 100
        MockUpload.return_value = "https://drive.google.com/test"

        # 実行
        phase = IntegrationPhase(stub_context)
        result = phase.execute(df)

        # 検証
        assert result is df
        stub_context.reporter.generate_reports.assert_called_once()
        MockUpload.assert_called_once_with("/tmp/dummy.csv")
        MockGspread.assert_called_once()
        stub_context.notifier.notify_success.assert_called_once()
        assert stub_context.report_url == "https://drive.google.com/test"


def test_integration_phase_no_data(stub_context):
    """準正常系: データ空の場合は処理をスキップすること"""
    phase = IntegrationPhase(stub_context)
    result = phase.execute(pl.DataFrame())

    assert result is None
    stub_context.reporter.generate_reports.assert_not_called()


def test_integration_phase_with_data_map(stub_context):
    """正常系: data_map (dict) 形式での入力を正しく処理できること"""
    df = pl.DataFrame({"code": ["1001"], "quant_score": [85.0]})
    data_map = {"df_eval": df}

    stub_context.reporter.generate_reports.return_value = {}

    phase = IntegrationPhase(stub_context)
    # パッチなしでも generate_reports までは進むはず
    with patch("src.orchestration.phases.integration.isinstance", return_value=True):
        result = phase.execute(data_map)
        assert result is df


def test_integration_phase_upload_failure(stub_context):
    """異常系: Google Drive へのアップロードが失敗した場合のエラーハンドリング"""
    df = pl.DataFrame({"code": ["1001"], "quant_score": [85.0]})
    stub_context.reporter.generate_reports.return_value = {"main_csv": "/tmp/dummy.csv"}

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.getsize", return_value=100),
        patch(
            "src.colab_tools.ColabTools.upload_file_to_drive",
            side_effect=Exception("Upload Error"),
        ),
        patch.object(IntegrationPhase, "_upload_summary_to_gspread"),
    ):
        phase = IntegrationPhase(stub_context)
        # 内部で例外をキャッチしてエラーログを出すはず
        phase.execute(df)

        assert stub_context.logger.error.called
        # アップロード失敗でも通知は行われる（失敗通知または成功通知のどちらか、実装に依存）
        assert (
            stub_context.notifier.notify_success.called
            or stub_context.notifier.notify_error.called
        )


def test_integration_phase_data_contract_violation(stub_context):
    """異常系: 必須カラムに欠損値がある場合は AssertionError で即座に物理遮断されること"""
    # price に null が混入したデータ
    df_invalid = pl.DataFrame({
        "code": ["1001", "1002"],
        "price": [1500.0, None],
        "verdict": ["BUY", "WATCH"]
    })
    phase = IntegrationPhase(stub_context)
    with pytest.raises(AssertionError, match="Data Contract Violation"):
        phase.execute(df_invalid)
