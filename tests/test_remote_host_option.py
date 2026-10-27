"""`--remote HOST` and `XEDA_REMOTE`: an empty `--remote` is a usage error, and an empty
`XEDA_REMOTE` is the same as an unset one, so the request runs locally."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.remote import client


def _design(tmp_path: Path) -> Path:
    design = tmp_path / "top.yaml"
    design.write_text("name: top\nrtl:\n  sources: [top.v]\n  top: top\n")
    (tmp_path / "top.v").write_text("module top(input a, output y); assign y = a; endmodule\n")
    return design


def _no_remote_run(*args, **kwargs):
    raise AssertionError("the request must not run remotely")


@pytest.mark.parametrize("json_output", [False, True])
def test_an_empty_remote_host_is_a_usage_error(tmp_path, monkeypatch, json_output):
    monkeypatch.delenv("XEDA_REMOTE", raising=False)
    monkeypatch.setattr(client, "run_remote", _no_remote_run)
    arguments = ["run", "yosys", str(_design(tmp_path)), "--remote", "", "--dry-run"]
    arguments += ["--run-root", str(tmp_path / "xeda_run")]
    if json_output:
        arguments.append("--json")
    result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 2, result.output
    if json_output:
        document = json.loads(result.stdout)
        assert document["outcome"] is None
        assert document["error"]["code"] == "usage_error"
        assert "`--remote` needs a host" in document["error"]["message"]
    else:
        assert "`--remote` needs a host" in result.output


def test_an_empty_xeda_remote_runs_the_request_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_REMOTE", "")
    monkeypatch.setattr(client, "run_remote", _no_remote_run)
    result = CliRunner().invoke(
        cli,
        ["run", "yosys", str(_design(tmp_path)), "--dry-run", "--json"]
        + ["--run-root", str(tmp_path / "xeda_run")],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["dry_run"] is True and "remote" not in document
