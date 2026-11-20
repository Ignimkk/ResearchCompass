import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from rcompass.cli import app, ensure_utf8_output
from rcompass.config import ENV_DATA_DIR

runner = CliRunner()


def test_redirected_cp1252_stream_switches_to_utf8() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    ensure_utf8_output(stream)
    stream.write("데이터 디렉터리")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "데이터 디렉터리"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], "연구 나침반"),  # no_args_is_help: 콜백 전에 도움말 출력
        (["--help"], "연구 나침반"),
        (["serve", "--help"], "데이터 디렉터리"),
    ],
)
def test_help_works_when_output_is_redirected(args: list[str], expected: str) -> None:
    # 콘솔이 아닌 파이프 + cp1252 인코딩에서도 한국어 도움말이 깨지지 않아야 합니다.
    result = subprocess.run(
        [sys.executable, "-c", "from rcompass.cli import run; run()", *args],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )
    stdout = result.stdout.decode("utf-8")
    assert "Traceback" not in result.stderr.decode("utf-8", "replace")
    assert expected in stdout


@pytest.fixture
def captured_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    """uvicorn.run을 가로채 실제 서버 없이 serve 명령의 결정 결과를 확인합니다."""
    captured: dict = {}

    def fake_run(asgi_app, host: str, port: int) -> None:
        captured.update(app=asgi_app, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return captured


def test_serve_binds_localhost_with_cli_data_dir(tmp_path: Path, captured_run: dict) -> None:
    result = runner.invoke(app, ["serve", "--data-dir", str(tmp_path), "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert captured_run["host"] == "127.0.0.1"
    assert captured_run["port"] == 9000
    assert captured_run["app"].state.settings.data_dir == tmp_path.resolve()


def test_empty_data_dir_option_falls_back_to_env(tmp_path: Path, captured_run: dict) -> None:
    env_dir = tmp_path / "from-env"
    result = runner.invoke(app, ["serve", "--data-dir", ""], env={ENV_DATA_DIR: str(env_dir)})
    assert result.exit_code == 0, result.output
    assert captured_run["app"].state.settings.data_dir == env_dir.resolve()


@pytest.mark.parametrize("port", ["0", "70000"])
def test_invalid_port_is_rejected(port: str, captured_run: dict) -> None:
    result = runner.invoke(app, ["serve", "--port", port])
    assert result.exit_code == 2
    assert captured_run == {}
