"""Tests for predict input parsing and the ``predict`` command."""

from pathlib import Path

import pytest

import bentloy


def test_key_value_pairs_are_typed() -> None:
    """``key=value`` pairs are converted to int, float, bool or string."""
    data = bentloy.parse_predict_input(["x1=1", "x2=2.5", "name=abc", "flag=true"], "")
    assert data == {"x1": 1, "x2": 2.5, "name": "abc", "flag": True}


def test_json_object_and_list() -> None:
    """A JSON object is used as is and a JSON list becomes ``features``."""
    assert bentloy.parse_predict_input(['{"x1": 1}'], "") == {"x1": 1}
    assert bentloy.parse_predict_input(["[1, 2, 3]"], "") == {"features": [1, 2, 3]}


def test_file_input(tmp_path: Path) -> None:
    """Input can be read from a file, but not combined with inline values."""
    path = tmp_path / "in.json"
    path.write_text('{"x1": 5, "x2": 2}', encoding="utf-8")
    assert bentloy.parse_predict_input([], str(path)) == {"x1": 5, "x2": 2}
    with pytest.raises(bentloy.CliError):
        bentloy.parse_predict_input(["x1=1"], str(path))


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([], id="empty"),
        pytest.param(["x1"], id="no-equals"),
        pytest.param(["=1"], id="empty-key"),
        pytest.param(['{"x1": }'], id="invalid-json"),
        pytest.param(['"just a string"'], id="json-string"),
    ],
)
def test_invalid_inputs(values: list[str]) -> None:
    """Malformed predict inputs raise CliError."""
    with pytest.raises(bentloy.CliError):
        bentloy.parse_predict_input(values, "")


@pytest.mark.usefixtures("fake_gateway")
def test_predict_command_calls_gateway(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The predict command posts the typed input and prints the predictions."""
    calls = []

    def fake_call(url: str, endpoint: str, payload: dict) -> dict:
        """Record the call and return a canned prediction."""
        calls.append((endpoint, payload))
        return {"model": "m", "predictions": [{"y_predictions": 13.9}]}

    monkeypatch.setattr(bentloy, "call_gateway", fake_call)
    args = bentloy.build_parser().parse_args(["predict", "m", "x1=5", "x2=2", "x3=3"])

    bentloy.cmd_predict(args, config)

    assert calls == [("predict", {"model_name": "m", "data": {"x1": 5, "x2": 2, "x3": 3}})]
    assert "13.9" in capsys.readouterr().out


@pytest.mark.usefixtures("fake_gateway")
def test_predict_error_exits_nonzero(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> None:
    """An error payload from the gateway becomes a CliError."""
    monkeypatch.setattr(bentloy, "call_gateway", lambda *a: {"error": "expects 3 features"})
    args = bentloy.build_parser().parse_args(["predict", "m", "[1, 2]"])

    with pytest.raises(bentloy.CliError, match="expects 3 features"):
        bentloy.cmd_predict(args, config)
