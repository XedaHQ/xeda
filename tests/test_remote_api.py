"""A `Workspace` with a `Remote` plans and runs a `Spec` on a host, and returns a `Plan` or a `Result`.

The tests run the real client, agent and launch entry over the fake `ssh`, with the fake FPGA tools
(`tests/remote_fixtures.py`). Nothing opens a real SSH connection.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import Design
from xeda.errors import RemoteError
from xeda.events import Event
from xeda.outcomes import Outcome
from xeda.plan import Plan, plan_from_dict
from xeda.remote import client
from xeda.remote_value import Remote
from xeda.spec import Spec
from xeda.workspace import Workspace

from .remote_fixtures import fake_remote, fake_ssh, host_profile_of, remote_host
from .test_remote_client import TIMEOUT, _design
from .test_remote_keep_running import (
    Connections,
    Gate,
    _launch_dirs,
    _launch_state,
    _run_in_thread,
    cleanup,
    leftovers,
    watch,
)

FIXTURES = (fake_ssh, fake_remote, remote_host, cleanup, leftovers, watch)

pytestmark = [pytest.mark.python_compat, pytest.mark.skipif(os.name != "posix", reason="POSIX")]

HOST = "somewhere"


def _spec(tmp_path: Path, flow: str = "nextpnr") -> Spec:
    return Spec(flow, Design.load(_design(tmp_path)))


def _remote(**options: Any) -> Remote:
    """The fake host with its profile, as the caller of a script passes it."""
    return Remote(HOST, profile=host_profile_of(HOST).model_dump(mode="python"), **options)


def _workspace(tmp_path: Path, **remote_options: Any) -> Workspace:
    return Workspace(tmp_path / "local" / "xeda_run", remote=_remote(**remote_options))


def _files(root: Path) -> set[Path]:
    return set(root.rglob("*")) if root.exists() else set()


# --- The value ------------------------------------------------------------------------------


def test_importing_the_workspace_loads_no_ssh_client():
    program = (
        "import sys, xeda.workspace, xeda.remote_value\n"
        "print('xeda.remote.client' in sys.modules, 'xeda.remote.ssh' in sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert done.stdout.split() == ["False", "False"]


def test_a_remote_is_a_frozen_value_that_copies_its_profile():
    profile = {"setup": ["module load x"], "flows": {"nextpnr": {"seed": 1}}}
    remote = Remote("host", profile=profile)
    profile["setup"].append("changed")
    assert remote.profile is not None and remote.profile["setup"] == ("module load x",)
    assert remote == Remote(
        "host", profile={"setup": ["module load x"], "flows": {"nextpnr": {"seed": 1}}}
    )
    assert (remote.fetch_all, remote.keep_running) == (False, False)
    with pytest.raises(AttributeError):
        remote.host = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        Remote("host", {"setup": []})  # type: ignore[misc]


# --- A run ----------------------------------------------------------------------------------


def test_a_remote_run_returns_a_result_with_node_records(tmp_path, remote_host):
    spec = _spec(tmp_path)
    events: list[tuple[str, str | None]] = []
    result = _workspace(tmp_path).run(spec, on_event=lambda e: events.append((e.kind, e.node)))
    assert result.passed and result.outcome is Outcome.PASSED and result.error is None
    assert result.spec is spec
    assert result.roots == ("nextpnr",)
    assert list(result.nodes) == ["yosys_fpga", "nextpnr"]
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    for name, node in result.nodes.items():
        assert node.outcome is Outcome.PASSED and node.record is not None
        assert node.run_dir == mirror / "top" / name
    assert result.record.outcome == "passed"
    assert events == [
        ("launch_started", None),
        ("node_started", "yosys_fpga"),
        ("node_finished", "yosys_fpga"),
        ("node_started", "nextpnr"),
        ("node_finished", "nextpnr"),
        ("launch_finished", None),
    ]


def test_a_failed_node_is_an_outcome_and_not_an_exception(tmp_path, remote_host):
    remote_host.configure(
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"}
    )
    result = _workspace(tmp_path).run(_spec(tmp_path))
    assert result.outcome is Outcome.FAILED
    assert result.error is not None and result.error.node == "nextpnr"
    assert result.nodes["nextpnr"].outcome is Outcome.FAILED
    assert result.nodes["yosys_fpga"].outcome is Outcome.PASSED


def test_a_fetch_failure_after_node_success_is_a_launch_level_error(
    tmp_path, remote_host, monkeypatch
):
    real_fetch = client._fetch

    def fetch_after_drop(agent, fetch_all):
        agent.process.kill()  # the connection goes away after the launch finished
        return real_fetch(agent, fetch_all)

    monkeypatch.setattr(client, "_fetch", fetch_after_drop)
    result = _workspace(tmp_path).run(_spec(tmp_path))
    assert result.outcome is Outcome.FAILED
    assert result.error is not None
    assert result.error.code == "remote_fetch_failed" and result.error.node is None
    assert "fetching the results" in result.error.message
    assert [node.outcome for node in result.nodes.values()] == [Outcome.PASSED, Outcome.PASSED]
    assert all(node.record is not None for node in result.nodes.values())


def test_keep_running_and_a_lost_connection_give_detached(
    tmp_path, remote_host, monkeypatch, cleanup
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    workspace = _workspace(tmp_path, keep_running=True)
    spec = _spec(tmp_path)

    def cut() -> None:
        cleanup.append(gate.pid())
        connections.agents[0].kill()  # the connection goes away while the tool runs

    threading.Thread(target=cut, daemon=True).start()
    result = workspace.run(spec)
    assert result.outcome is Outcome.DETACHED
    assert result.error is not None and result.error.code == "remote_detached"
    (directory,) = _launch_dirs(remote_host)
    assert result.launch_id == directory.name and directory.name in result.error.message
    assert [(n.outcome, n.disposition.value) for n in result.nodes.values()] == [
        (Outcome.DETACHED, "not_run")
    ] * 2
    gate.release()


def test_following_a_running_identical_launch_gives_every_event_its_id(
    tmp_path, remote_host, monkeypatch, cleanup, watch
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    workspace = _workspace(tmp_path, keep_running=True)
    spec = _spec(tmp_path)

    def cut() -> None:
        cleanup.append(gate.pid())
        connections.agents[0].kill()  # the connection goes away while the tool runs

    threading.Thread(target=cut, daemon=True).start()
    assert workspace.run(spec).outcome is Outcome.DETACHED
    (directory,) = _launch_dirs(remote_host)
    seen = watch("The same request runs on somewhere")
    events: list[Event] = []

    thread, again = _run_in_thread(lambda: workspace.run(spec, on_event=events.append))
    seen.wait()
    gate.release()
    thread.join(TIMEOUT)
    result = again.get(timeout=TIMEOUT)
    assert result.outcome is Outcome.PASSED and result.launch_id == directory.name
    assert [event.kind for event in events] == [
        "launch_started",
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "launch_finished",
    ]
    assert {event.launch_id for event in events} == {directory.name}
    assert _launch_dirs(remote_host) == [directory], "no second launch"


def test_a_canceled_token_gives_a_canceled_result_before_any_connection(tmp_path, remote_host):
    from xeda.cancel import CancelToken

    token = CancelToken()
    token.cancel()
    result = _workspace(tmp_path).run(_spec(tmp_path), cancel=token)
    assert result.outcome is Outcome.CANCELED and result.error is not None
    assert result.error.code == "canceled" and not result.nodes
    assert not any("-M" in call for call in remote_host.remote.calls("ssh"))


def test_the_profile_comes_from_the_value_not_a_file(tmp_path, remote_host, monkeypatch):
    """A `remotes.yaml` that would break the launch is never read: the value has the setup."""
    workspace = _workspace(tmp_path)
    remote_host.profile_file.write_text(
        json.dumps({"hosts": {HOST: {"setup": ["echo the file was read >&2", "exit 3"]}}})
    )
    result = workspace.run(_spec(tmp_path))
    assert result.passed, result.error


def test_a_remote_without_a_profile_has_the_default_profile(tmp_path, remote_host):
    """The file makes the tool fail, and a profile with the same `env` makes it fail too. A
    `Remote` with no profile has the default profile, and ignores the file."""
    remote_host.configure(
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"}
    )
    root = tmp_path / "local" / "xeda_run"
    spec = _spec(tmp_path)
    assert Workspace(root, remote=Remote(HOST)).run(spec).passed
    failed = Workspace(root, remote=_remote()).run(spec, rebuild_all=True)
    assert failed.outcome is Outcome.FAILED


# --- A plan ---------------------------------------------------------------------------------


def test_a_remote_plan_is_a_plan(tmp_path, remote_host):
    spec = _spec(tmp_path)
    workspace = _workspace(tmp_path)
    local_before = _files(tmp_path / "local")
    plan = workspace.plan(spec, rebuild_all=True)
    assert isinstance(plan, Plan) and plan.spec is spec and plan.options["rebuild_all"] is True
    assert plan.roots == ("nextpnr",) and list(plan.nodes) == ["yosys_fpga", "nextpnr"]
    assert all(n.state == "stale" for n in plan.nodes.values())
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    assert plan.nodes["nextpnr"].run_dir == mirror / "top" / "nextpnr"
    assert plan_from_dict(plan.to_dict(), spec=spec, workspace=workspace._options()) == plan
    # Neither the local run root nor the run root of the host has a new file.
    assert _files(tmp_path / "local") == local_before
    assert _files(remote_host.workspace() / "xeda_run") == set()


def test_a_remote_plan_keeps_the_options_of_the_caller(tmp_path, remote_host):
    out = tmp_path / "out"
    plan = _workspace(tmp_path).plan(_spec(tmp_path), outputs_to=out, overwrite_outputs=True)
    assert plan.options["outputs_to"] == out and plan.options["overwrite_outputs"] is True
    assert plan.options["rebuild_all"] is False


def test_a_plan_with_keep_running_writes_no_record_and_keeps_nothing_running(tmp_path, remote_host):
    spec = _spec(tmp_path)
    before = _files(tmp_path / "local")
    plan = _workspace(tmp_path, keep_running=True).plan(spec)
    assert list(plan.nodes) == ["yosys_fpga", "nextpnr"]
    assert _files(tmp_path / "local") == before, "the plan wrote in the local run root"
    (directory,) = _launch_dirs(remote_host)
    assert _launch_state(directory)["keep_running"] is False, "the host would keep the plan running"


@pytest.mark.parametrize("option", ["prune_variants", "post_cleanup", "post_cleanup_purge"])
def test_an_option_that_removes_run_directories_is_refused(tmp_path, remote_host, option):
    from xeda.errors import SpecError

    for call in (_workspace(tmp_path).plan, _workspace(tmp_path).run):
        with pytest.raises(SpecError) as caught:
            call(_spec(tmp_path), **{option: True})
        assert caught.value.code == "unsupported_with_remote" and option in caught.value.message
    assert not remote_host.remote.calls("ssh")


# --- A host that cannot be reached --------------------------------------------------------------


@pytest.mark.parametrize("verb", ["plan", "run"])
def test_an_unreachable_host_raises_before_anything_is_written(
    tmp_path, remote_host, monkeypatch, verb
):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    workspace = _workspace(tmp_path)
    with pytest.raises(RemoteError) as caught:
        getattr(workspace, verb)(_spec(tmp_path))
    assert caught.value.code == "remote_unreachable" and caught.value.host == HOST
    assert "cannot connect to somewhere" in caught.value.message
    assert not (tmp_path / "local").exists()


def test_a_host_that_cannot_be_set_up_raises_remote_setup_failed(tmp_path, remote_host):
    remote_host.remote.set_platform("Darwin", "arm64")
    with pytest.raises(RemoteError) as caught:
        _workspace(tmp_path).run(_spec(tmp_path))
    assert caught.value.code == "remote_setup_failed" and caught.value.host == HOST
    assert "not-linux: " in caught.value.message
    assert not (tmp_path / "local" / "xeda_run" / "@somewhere").exists()


# --- Attach -----------------------------------------------------------------------------------


def _attach(tmp_path: Path, launch_id: str):
    return CliRunner().invoke(
        cli,
        ["remote", "attach", HOST, launch_id, "--run-root", str(tmp_path / "local" / "xeda_run")]
        + ["--json"],
    )


def test_attach_through_the_cli(tmp_path, remote_host, monkeypatch, cleanup, watch):
    spec = _spec(tmp_path)
    # A launch that passed.
    passed = _workspace(tmp_path, keep_running=True).run(spec)
    assert passed.passed
    result = _attach(tmp_path, passed.launch_id)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["outcome"] == "passed" and document["launch_id"] == passed.launch_id

    # A launch that failed.
    remote_host.configure(
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"}
    )
    failed = _workspace(tmp_path, keep_running=True).run(spec, rebuild_all=True)
    assert failed.outcome is Outcome.FAILED
    result = _attach(tmp_path, failed.launch_id)
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["outcome"] == "failed" and document["error"]["node"] == "nextpnr"

    # A launch that still runs when the connection of the attach drops.
    remote_host.configure(env={"XEDA_FAKE_FPGA_MODE": ""})
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    following = watch("Following the launch")
    threading.Thread(
        target=lambda: (cleanup.append(gate.pid()), connections.agents[0].kill()), daemon=True
    ).start()
    detached = _workspace(tmp_path, keep_running=True).run(spec, rebuild_all=True, clean=True)
    assert detached.outcome is Outcome.DETACHED
    thread, attached = _run_in_thread(lambda: _attach(tmp_path, detached.launch_id))
    following.wait()
    connections.agents[-1].kill()
    thread.join(TIMEOUT)
    result = attached.get(timeout=TIMEOUT)
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["outcome"] == "detached"
    assert document["error"]["code"] == "remote_detached"
    gate.release()
    assert len(_launch_dirs(remote_host)) == 3
