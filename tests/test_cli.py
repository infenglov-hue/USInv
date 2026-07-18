import pytest

from usinv.cli import main


def test_config_check_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config-check"]) == 0
    output = capsys.readouterr().out
    assert "config_ok schema=1" in output
    assert "evidence=undecided execution=paper holdings=15 overlay=O0" in output
