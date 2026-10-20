"""`xeda remote check` and `xeda remote clean` on a fake remote host.

The fake remote provisions an environment with the fake `uv` and runs the real agent from a
snapshot of this checkout, as the tests of the client do (`tests/test_remote_client.py`). The
agent's `check` and `clean` run for real on this machine, in the home directory of the fake host.
"""

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

import click
import psutil
import pytest
from click.testing import CliRunner, Result

from xeda.cli import cli
from xeda.introspect import flow_executables

from .remote_fixtures import FakeRemote, fake_remote, fake_ssh, remote_host

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = [pytest.mark.skipif(os.name != "posix", reason="POSIX")]

DAY = 86400


def _invoke(*arguments: str, input: str | None = None) -> Result:
    return CliRunner().invoke(cli, ["remote", *arguments], input=input)


def _document(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout)


def _text(result: Result) -> str:
    return click.unstyle(result.output)


def _age(path: Path, days: float) -> None:
    """Make `path` and everything below it look unused for `days` days."""
    when = time.time() - days * DAY
    for found in [path, *path.rglob("*")]:
        os.utime(found, (when, when), follow_symlinks=False)


def _workspace(remote: FakeRemote, name: str, days: float) -> Path:
    path = remote.layout / "ws" / name
    (path / "inputs").mkdir(parents=True)
    (path / "lock").write_text("")
    (path / "inputs" / "f").write_text(name)
    _age(path, days)
    return path


# --- check ------------------------------------------------------------------------------------


def test_check_reports_the_host_and_the_environment_as_json(remote_host):
    result = _invoke("check", "somewhere", "--json")

    assert result.exit_code == 0, _text(result)
    document = _document(result)
    assert document["success"] is True and document["host"] == "somewhere"
    assert document["platform"]["system"] == "Linux"
    assert document["python"]["version"].count(".") == 2
    environment = document["environment"]
    assert environment["key"] and environment["code"]
    assert environment["lock_file"] == f"{environment['prefix']}/xeda-lock.txt"
    assert environment["prefix"]
    assert isinstance(environment["lock"], list)
    assert document["disk"]["free"] > 0 and document["disk"]["total"] >= document["disk"]["free"]
    assert document["kill_user_processes"] in ("yes", "no", "unknown")
    assert isinstance(document["warnings"], list)


def test_check_looks_for_the_executables_of_the_flows_and_is_not_an_error_for_a_missing_one(
    remote_host,
):
    (remote_host.home / ".bash_profile").write_text(f"export PATH={remote_host.toolchain}:$PATH\n")

    result = _invoke("check", "somewhere", "--json")

    assert result.exit_code == 0, _text(result)
    executables = _document(result)["executables"]
    found, missing = set(executables["found"]), set(executables["missing"])
    assert found | missing == set(flow_executables()) and not found & missing
    assert {"yosys", "nextpnr-ecp5", "ecppack"} <= found
    assert executables["found"]["yosys"] == str(remote_host.toolchain / "yosys")


def test_check_reports_a_missing_executable(remote_host, monkeypatch):
    monkeypatch.setattr("xeda.introspect.flow_executables", lambda: ["sh", "no-such-tool-xeda"])

    document = _document(_invoke("check", "somewhere", "--json"))

    assert list(document["executables"]["found"]) == ["sh"]
    assert document["executables"]["missing"] == ["no-such-tool-xeda"]


def test_check_prints_the_facts_and_warns_when_logout_kills_a_launch(remote_host):
    loginctl = remote_host.remote.bin / "loginctl"
    loginctl.write_text("#!/bin/sh\necho yes\n")
    loginctl.chmod(0o755)

    result = _invoke("check", "somewhere")

    text = _text(result)
    assert result.exit_code == 0, text
    for expected in (
        "Host somewhere: Linux x86_64",
        "Python 3.",
        "Lock file ",
        "Disk ",
        "KillUserProcesses: yes",
        "Executables on the login PATH",
        "Warning: systemd-logind kills the processes",
    ):
        assert expected in text, text
    document = _document(_invoke("check", "somewhere", "--json"))
    assert document["kill_user_processes"] == "yes"
    assert "KillUserProcesses=yes" in document["warnings"][0]


def test_check_provisions_once(remote_host):
    assert _invoke("check", "somewhere", "--json").exit_code == 0
    assert _invoke("check", "somewhere", "--json").exit_code == 0

    assert remote_host.remote.venv_builds() == 1


def test_check_of_a_host_that_is_not_linux_fails_with_a_document(remote_host):
    remote_host.remote.set_platform("Darwin", "arm64")

    result = _invoke("check", "somewhere", "--json")

    document = _document(result)
    assert result.exit_code == 1 and document["success"] is False
    assert document["error"]["type"] == "RemoteProvisionError"
    assert "Linux" in document["error"]["message"]


def test_check_of_a_host_that_is_not_linux_says_why_without_json(remote_host):
    remote_host.remote.set_platform("Darwin", "arm64")

    result = _invoke("check", "somewhere")

    assert result.exit_code == 1
    assert "Linux" in _text(result)


def test_check_with_a_bad_profile_fails_with_a_document(remote_host):
    remote_host.profile_file.write_text("hosts: [not, a, table]\n")

    result = _invoke("check", "somewhere", "--json")

    document = _document(result)
    assert result.exit_code == 1 and document["success"] is False
    assert document["error"]["type"] == "RemoteConfigError"


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        (["check"], "MissingParameter"),
        (["check", "somewhere", "--no-such-option"], "NoSuchOption"),
        (["clean", "somewhere", "--older-than", "-1"], "BadParameter"),
        (["clean", "somewhere", "--older-than", "soon"], "BadParameter"),
    ],
    ids=lambda value: value if isinstance(value, str) else " ".join(value),
)
def test_a_usage_error_is_a_document_with_json(arguments, error_type):
    result = _invoke(*arguments, "--json")

    document = _document(result)
    assert result.exit_code == 2 and document["success"] is False
    assert document["error"]["type"] == error_type


# --- clean ------------------------------------------------------------------------------------


def _clean_calls(remote: FakeRemote) -> list[list[str]]:
    """The remote commands of `clean`, as the fake ssh recorded them."""
    return [call for call in remote.calls("ssh") if any(" clean " in word for word in call)]


def test_clean_removes_only_what_is_old_and_unused(remote_host):
    remote = remote_host.remote
    old = _workspace(remote, "old", 60)
    recent = _workspace(remote, "recent", 1)
    locked = _workspace(remote, "locked", 60)

    with (locked / "lock").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _invoke("clean", "somewhere", "--yes", "--json")

    assert result.exit_code == 0, _text(result)
    document = _document(result)
    assert document["success"] is True and document["host"] == "somewhere"
    assert [Path(item["path"]).name for item in document["removed"]] == ["old"]
    reasons = {Path(item["path"]).name: item["reason"] for item in document["kept"]}
    assert reasons["recent"] == "recently used" and reasons["locked"] == "locked"
    assert not old.exists() and recent.exists() and locked.exists()
    # the environment that the agent runs from is never removed
    assert any(item["reason"] == "in use" for item in document["kept"])
    assert (remote.layout / "env").is_dir() and any((remote.layout / "env").iterdir())


def test_clean_removes_an_old_environment_and_code_copy_that_nobody_uses(remote_host):
    """The removal names the paths on the command line of the agent. That is no use of them."""
    remote = remote_host.remote
    assert _invoke("check", "somewhere").exit_code == 0
    stale = [remote.layout / "env" / "stale-env", remote.layout / "code" / "stale-code"]
    for path in stale:
        (path / "lib").mkdir(parents=True)
        (path / "lib" / "f").write_text(path.name)
        _age(path, 60)

    result = _invoke("clean", "somewhere", "--yes", "--json")

    assert result.exit_code == 0, _text(result)
    document = _document(result)
    assert sorted(Path(item["path"]).name for item in document["removed"]) == [
        "stale-code",
        "stale-env",
    ]
    assert not any(path.exists() for path in stale)
    # what the check made is kept (it is recent)
    assert any((remote.layout / "env").iterdir()) and any((remote.layout / "code").iterdir())


def test_clean_with_all_removes_what_nobody_holds_however_recent(remote_host):
    remote = remote_host.remote
    recent = _workspace(remote, "recent", 1)

    document = _document(_invoke("clean", "somewhere", "--all", "--json"))

    workspaces = [item for item in document["removed"] if item["kind"] == "workspace"]
    assert [Path(item["path"]).name for item in workspaces] == ["recent"]
    assert not recent.exists()


def test_clean_with_older_than_changes_the_age(remote_host):
    remote = remote_host.remote
    middle = _workspace(remote, "middle", 10)

    kept = _document(_invoke("clean", "somewhere", "--json"))
    removed = _document(_invoke("clean", "somewhere", "--older-than", "5", "--json"))

    assert kept["removed"] == [] and any(Path(i["path"]).name == "middle" for i in kept["kept"])
    assert [Path(item["path"]).name for item in removed["removed"]] == ["middle"]
    assert not middle.exists()


def test_clean_lists_first_and_removes_only_after_a_yes(remote_host):
    remote = remote_host.remote
    old = _workspace(remote, "old", 60)

    result = _invoke("clean", "somewhere", input="y\n")

    text = _text(result)
    assert result.exit_code == 0, text
    assert "Xeda can remove 1 entries on somewhere" in text and "Remove them?" in text
    assert "Removed 1 entries on somewhere" in text and str(old) in text
    assert not old.exists()
    calls = [" ".join(call) for call in _clean_calls(remote)]
    assert len(calls) == 2 and "--list" in calls[0] and "--list" not in calls[1]


def test_clean_removes_nothing_after_a_no(remote_host):
    remote = remote_host.remote
    old = _workspace(remote, "old", 60)

    result = _invoke("clean", "somewhere", input="n\n")

    assert result.exit_code == 0, _text(result)
    assert "Nothing was removed." in _text(result)
    assert old.exists()
    (listing,) = _clean_calls(remote)
    assert "--list" in " ".join(listing)


def test_clean_without_an_answer_removes_nothing_and_names_yes(remote_host):
    old = _workspace(remote_host.remote, "old", 60)

    result = _invoke("clean", "somewhere", input="")

    assert result.exit_code == 1
    assert "--yes" in _text(result) and old.exists()


def test_clean_with_json_does_not_ask(remote_host):
    old = _workspace(remote_host.remote, "old", 60)

    result = _invoke("clean", "somewhere", "--json", input="")

    assert result.exit_code == 0, _text(result)
    assert [Path(item["path"]).name for item in _document(result)["removed"]] == ["old"]
    assert not old.exists()


def test_clean_with_nothing_to_remove_asks_nothing_and_removes_nothing(remote_host):
    remote = remote_host.remote
    _workspace(remote, "recent", 1)

    result = _invoke("clean", "somewhere")

    assert result.exit_code == 0, _text(result)
    assert "Nothing to remove on somewhere." in _text(result)
    assert "Remove them?" not in _text(result)
    (listing,) = _clean_calls(remote)
    assert "--list" in " ".join(listing)


def test_clean_of_a_host_that_is_not_linux_fails_with_a_document(remote_host):
    remote_host.remote.set_platform("Darwin", "arm64")

    result = _invoke("clean", "somewhere", "--yes", "--json")

    document = _document(result)
    assert result.exit_code == 1 and document["success"] is False
    assert document["error"]["type"] == "RemoteProvisionError"


def test_the_remote_group_is_listed_in_the_help():
    result = CliRunner().invoke(cli, ["--help"])

    assert "remote" in click.unstyle(result.output)
    help_text = click.unstyle(CliRunner().invoke(cli, ["remote", "--help"]).output)
    assert "check" in help_text and "clean" in help_text


def test_check_reports_docker_apart_from_the_executables_of_the_flows(remote_host):
    docker = remote_host.toolchain / "docker"
    docker.write_text("#!/bin/sh\n")
    docker.chmod(0o755)
    (remote_host.home / ".bash_profile").write_text(f"export PATH={remote_host.toolchain}:$PATH\n")

    document = _document(_invoke("check", "somewhere", "--json"))

    assert document["docker"] == {"found": True, "path": str(docker)}
    assert "docker" not in document["executables"]["found"]
    assert "docker" not in document["executables"]["missing"]
    text = _text(_invoke("check", "somewhere"))
    assert f"docker (needed only for dockerized flows): found  {docker}" in text


def test_check_reports_a_missing_docker_without_a_warning(remote_host, monkeypatch):
    monkeypatch.setattr("xeda.remote.commands.DOCKER", "no-such-docker-xeda")

    document = _document(_invoke("check", "somewhere", "--json"))

    assert document["docker"] == {"found": False, "path": None}
    assert not any("docker" in warning for warning in document["warnings"])


# --- A failed connection, a running launch, and exactly what was confirmed ---------------------


@pytest.mark.parametrize("command", ["check", "clean"])
def test_a_failed_connection_gives_exit_1_and_one_document(remote_host, monkeypatch, command):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")

    result = _invoke(command, "somewhere", "--json")

    document = _document(result)  # the whole of stdout is this one document
    assert result.exit_code == 1 and document["success"] is False
    assert document["error"]["type"] == "RemoteConnectionError"
    assert document["error"]["message"]


@pytest.mark.parametrize("command", ["check", "clean"])
def test_a_failed_connection_says_why_without_json(remote_host, monkeypatch, command):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")

    result = _invoke(command, "somewhere")

    assert result.exit_code == 1 and "somewhere" in _text(result)


def test_clean_keeps_a_workspace_with_a_running_launch(remote_host):
    remote = remote_host.remote
    running = _workspace(remote, "running", 60)
    idle = _workspace(remote, "idle", 60)
    me = psutil.Process()
    launch = running / "launches" / "x"
    launch.mkdir(parents=True)
    (launch / "state.json").write_text(
        json.dumps({"id": "x", "pid": me.pid, "pid_created": me.create_time(), "state": "running"})
    )
    _age(running, 60)

    document = _document(_invoke("clean", "somewhere", "--all", "--json"))

    removed = {Path(item["path"]).name for item in document["removed"]}
    reasons = {Path(item["path"]).name: item["reason"] for item in document["kept"]}
    assert "idle" in removed and "running" not in removed
    assert reasons["running"] == "a launch runs"
    assert running.exists() and not idle.exists()


def test_clean_keeps_a_directory_that_has_no_lock_file_yet(remote_host):
    starting = remote_host.remote.layout / "ws" / "starting"
    (starting / "inputs").mkdir(parents=True)
    _age(starting, 60)

    document = _document(_invoke("clean", "somewhere", "--all", "--json"))

    assert not any(Path(i["path"]).name == "starting" for i in document["removed"])
    assert any("no lock file" in item["reason"] for item in document["kept"])
    assert starting.exists()


def test_clean_removes_exactly_what_was_listed_and_confirmed(remote_host, monkeypatch):
    remote = remote_host.remote
    listed = _workspace(remote, "listed", 60)
    used = _workspace(remote, "used-after", 60)
    answers = []

    def confirm(text: str, **keywords: Any) -> bool:
        """The user says yes. Meanwhile, one workspace is used and another one is made."""
        answers.append(text)
        (used / "inputs" / "g").write_text("new")
        os.utime(used / "inputs")  # the workspace was used after the listing
        _workspace(remote, "late", 60)
        return True

    monkeypatch.setattr(click, "confirm", confirm)

    result = _invoke("clean", "somewhere", "--older-than", "30")

    text = _text(result)
    assert result.exit_code == 0 and answers == ["Remove them?"], text
    assert "Removed 1 entries on somewhere" in text and str(listed) in text
    assert not listed.exists()
    assert used.exists() and (remote.layout / "ws" / "late").exists()
    assert "recently used" in text
