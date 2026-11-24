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

from xeda._internal.cli import cli
from xeda._internal.design import Design
from xeda.errors import RemoteError, StalePlanError
from xeda._internal.events import Event
from xeda._internal.outcomes import Disposition, Outcome
from xeda._internal.plan import Plan, plan_from_dict
from xeda._internal.remote import client
from xeda._internal.remote_value import Remote
from xeda._internal.spec import Spec
from xeda._internal.workspace import Workspace

from .test_schema_baseline import _validate
from .remote_fixtures import fake_remote, fake_ssh, host_profile_of, remote_host
from .test_git_dependencies import git_log, remote
from .test_remote_client import PART, TIMEOUT, _design
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
from .source_layout import layout_script

FIXTURES = (fake_ssh, fake_remote, remote_host, cleanup, leftovers, watch, git_log, remote)

pytestmark = [pytest.mark.python_compat, pytest.mark.skipif(os.name != "posix", reason="POSIX")]

HOST = "somewhere"


def _spec(tmp_path: Path, flow: str = "nextpnr") -> Spec:
    return Spec(flow, Design.load(_design(tmp_path)))


def _remote(**options: Any) -> Remote:
    """The fake host with its profile, as the caller of a script passes it."""
    return Remote(HOST, profile=host_profile_of(HOST).model_dump(mode="python"), **options)


def _workspace(tmp_path: Path, **remote_options: Any) -> Workspace:
    return Workspace(tmp_path / "local" / "xeda_run", remote=_remote(**remote_options))


def _with_dependencies(tmp_path: Path, *entries: str) -> Spec:
    """The spec of `_design` with the dependencies `entries` (a design file or a Git URI)."""
    design = _design(tmp_path)
    design.write_text(
        design.read_text() + "dependencies:\n" + "".join(f"  - {entry}\n" for entry in entries)
    )
    return Spec("nextpnr", Design.load(design))


def _local_dependency(tmp_path: Path, *sources: str) -> Path:
    """A design file in a directory of the design, with the Verilog `sources`."""
    root = tmp_path / "design" / "dep"
    root.mkdir(exist_ok=True)
    for source in sources or ("lib.v",):
        (root / source).write_text(f"module {Path(source).stem}; endmodule\n")
    file = root / "dep.yaml"
    file.write_text(f"name: dep\nrtl:\n  sources: [{', '.join(sources or ('lib.v',))}]\n")
    return file


def _host_requests(remote_host) -> list[dict[str, Any]]:
    """The requests that the host received, in the order of their launches."""
    return [json.loads((d / "request.json").read_text()) for d in _launch_dirs(remote_host)]


def _files(root: Path) -> set[Path]:
    return set(root.rglob("*")) if root.exists() else set()


# --- The value ------------------------------------------------------------------------------


def test_importing_the_workspace_loads_no_ssh_client():
    program = layout_script(
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


def test_a_remote_launch_tells_the_events_of_a_launch_on_this_machine(tmp_path, remote_host):
    """The producer fails and blocks its consumer: both have their events, as in a launch on this
    machine, with their outcomes in the result."""
    remote_host.configure(env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "yosys"})
    seen: list[Event] = []
    result = _workspace(tmp_path).run(_spec(tmp_path), on_event=seen.append)
    assert [(node.outcome, node.disposition) for node in result.nodes.values()] == [
        (Outcome.FAILED, Disposition.RAN),
        (Outcome.BLOCKED, Disposition.NOT_RUN),
    ]
    assert [(e.kind, e.node, e.outcome, e.disposition) for e in seen] == [
        ("launch_started", None, None, None),
        ("node_started", "yosys_fpga", None, None),
        ("node_finished", "yosys_fpga", Outcome.FAILED, Disposition.RAN),
        ("node_started", "nextpnr", None, None),
        ("node_finished", "nextpnr", Outcome.BLOCKED, Disposition.NOT_RUN),
        ("launch_finished", None, Outcome.FAILED, None),
    ]


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
    from xeda._internal.cancel import CancelToken

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
    """A `Remote` without a profile has the default profile and never reads the file, whose setup
    fails. A profile given as a value is used: the same setup fails the plan. A plan runs no
    tool, so the test needs none on the host."""
    failing = {"setup": ["echo the setup ran >&2", "exit 3"]}
    remote_host.profile_file.write_text(json.dumps({"hosts": {HOST: failing}}))
    root = tmp_path / "local" / "xeda_run"
    spec = _spec(tmp_path)
    assert isinstance(Workspace(root, remote=Remote(HOST)).plan(spec), Plan)
    with pytest.raises(RemoteError) as caught:
        Workspace(root, remote=Remote(HOST, profile=failing)).plan(spec)
    assert caught.value.code == "remote_setup_failed" and caught.value.host == HOST


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


def test_a_remote_plan_runs_when_nothing_changed(tmp_path, remote_host):
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _local_dependency(tmp_path)
    plan = workspace.plan(spec)
    result = workspace.run(plan)
    assert result.outcome is Outcome.PASSED and result.spec is spec
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True, False]


def test_a_remote_plan_and_run_accept_a_dependency_that_lists_a_file_of_the_design(
    tmp_path, remote_host
):
    """`base.yaml` lies beside the design file and lists the same `top.v`. The package holds one
    file, so the host plans what this side planned, and the plan runs."""
    spec = _with_dependencies(tmp_path, "base.yaml")
    (tmp_path / "design" / "base.yaml").write_text("name: base\nrtl:\n  sources: [top.v]\n")
    workspace = _workspace(tmp_path)
    plan = workspace.plan(spec)
    result = workspace.run(plan)
    assert result.outcome is Outcome.PASSED
    assert workspace.run(spec).outcome is Outcome.PASSED


def test_a_remote_plan_that_the_host_would_plan_differently_is_stale(tmp_path, remote_host):
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _local_dependency(tmp_path)
    plan = workspace.plan(spec)
    _local_dependency(tmp_path, "lib.v", "more.v")
    events: list[Event] = []
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan, on_event=events.append)
    assert raised.value.code == "stale_plan"
    assert raised.value.difference == "the identity of node yosys_fpga changed"
    assert events == []
    assert not (tmp_path / "local" / "xeda_run" / "@somewhere").exists()
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True]
    assert _files(remote_host.workspace() / "xeda_run") == set(), "the host ran nothing"


def test_a_refusal_now_is_a_difference_and_an_unreachable_host_is_not(
    tmp_path, remote_host, monkeypatch
):
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    dependency = _local_dependency(tmp_path)
    plan = workspace.plan(spec)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    with pytest.raises(RemoteError) as unreachable:
        workspace.run(plan)
    assert unreachable.value.code == "remote_unreachable"
    monkeypatch.delenv("XEDA_FAKE_TOOL_FAIL")
    dependency.unlink()
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert raised.value.difference.startswith("planning is refused now: ")
    assert raised.value.__cause__ is not None


def test_a_dependency_that_moved_makes_a_remote_plan_stale(tmp_path, remote_host, remote):
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, f"git+{remote.url}?branch=main#dep.yaml")
    plan = workspace.plan(spec)
    (entry,) = plan.to_dict()["dependencies"]
    assert (entry["repository"], entry["reference"], entry["commit"]) == (
        remote.url,
        "main",
        remote.first,
    )
    restored = plan_from_dict(plan.to_dict(), spec=spec, workspace=workspace._options())
    assert workspace.run(restored).passed  # the commit is the same: no difference
    moved = remote.advance()
    for stale in (plan, restored):
        with pytest.raises(StalePlanError) as raised:
            workspace.run(stale)
        assert raised.value.code == "stale_plan"
    assert workspace.plan(spec).to_dict()["dependencies"][0]["commit"] == moved


def test_a_remote_plan_runs_with_the_commit_that_was_compared(tmp_path, remote_host, remote):
    """The branch of a Git dependency moves after the host compared its plan with the saved one,
    and before the launch is prepared. The launch still uses the saved commit, as the plan says."""
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, f"git+{remote.url}?branch=main#dep.yaml")
    plan = workspace.plan(spec)
    first = plan.to_dict()["dependencies"][0]["commit"]
    assert first == remote.first
    real, moved = client._plan, []

    def move_before_the_launch(*arguments, **options):
        if not options["dry_run"] and not moved:  # the comparison is over: the branch moves
            moved.append(remote.advance())
        return real(*arguments, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", move_before_the_launch)
        result = workspace.run(plan)
    assert moved and moved[0] != first
    assert result.passed, result.error
    (launch,) = [r for r in _host_requests(remote_host) if not r["dry_run"]]
    assert set(launch["dependency_commits"].values()) == {first}
    assert result.nodes["nextpnr"].record is not None
    (entry,) = result.nodes["nextpnr"].record.dependencies
    assert entry["commit"] == first
    assert workspace.plan(spec).to_dict()["dependencies"][0]["commit"] == moved[0]


def test_a_remote_plan_whose_request_changed_before_the_launch_is_stale(tmp_path, remote_host):
    """A local dependency changes after the comparison and before the launch is prepared. The
    host runs nothing: the request of the launch is not the plan that the caller saved."""
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _local_dependency(tmp_path)
    plan = workspace.plan(spec)
    real, changed = client._plan, []

    def change_before_the_launch(*arguments, **options):
        if not options["dry_run"] and not changed:
            changed.append(_local_dependency(tmp_path, "lib.v", "more.v"))
        return real(*arguments, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", change_before_the_launch)
        with pytest.raises(StalePlanError) as raised:
            workspace.run(plan)
    assert changed and raised.value.difference == "the identity of node yosys_fpga changed"
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True]
    assert not (tmp_path / "local" / "xeda_run" / "@somewhere").exists()


def _dependency_on_commit(tmp_path: Path, repository: Any, commit: str) -> None:
    """The local dependency file, which lists a Git dependency at the explicit `commit`."""
    file = _local_dependency(tmp_path)
    file.write_text(
        file.read_text() + f"dependencies:\n  - git+{repository.url}?commit={commit}#dep.yaml\n"
    )


def test_a_remote_plan_is_stale_when_a_nested_explicit_commit_changes(
    tmp_path, remote_host, remote
):
    """A local dependency names another explicit commit of a Git dependency after the comparison.
    No node identity counts a commit, and the names are the same, so only the resolved
    dependencies show it. The host runs nothing."""
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _dependency_on_commit(tmp_path, remote, remote.first)
    plan = workspace.plan(spec)
    moved = remote.advance()
    real, edited = client._plan, []

    def edit_before_the_launch(*arguments, **options):
        if not options["dry_run"] and not edited:  # the comparison is over
            edited.append(moved)
            _dependency_on_commit(tmp_path, remote, moved)
        return real(*arguments, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", edit_before_the_launch)
        with pytest.raises(StalePlanError) as raised:
            workspace.run(plan)
    assert edited and raised.value.difference == f"dependency {remote.url} now resolves to {moved}"
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True]


def _edit_while_packaging(tmp_path: Path, remote: Any, moved: str | None):
    """Patch `package_inputs` so that the local dependency file names the explicit commit
    `moved` (or no Git dependency, for `None`) just before the launch is packaged (not the
    comparison)."""
    real_plan, real_package, planned = client._plan, client.package_inputs, []

    def plan(*arguments, **options):
        result = real_plan(*arguments, **options)
        if not options["dry_run"]:
            planned.append(True)
        return result

    def package(*arguments, **options):
        if planned:  # the request is planned: the edit comes after it
            planned.clear()
            if moved is None:
                _local_dependency(tmp_path)
            else:
                _dependency_on_commit(tmp_path, remote, moved)
        return real_package(*arguments, **options)

    return plan, package


def test_a_remote_plan_is_stale_when_a_dependency_file_changes_while_it_is_packaged(
    tmp_path, remote_host, remote
):
    """The local dependency file changes after the request was planned and before it is
    packaged. The package would carry the new commit, so the launch is refused."""
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _dependency_on_commit(tmp_path, remote, remote.first)
    plan = workspace.plan(spec)
    moved = remote.advance()
    planning, packaging = _edit_while_packaging(tmp_path, remote, moved)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", planning)
        patch.setattr(client, "package_inputs", packaging)
        with pytest.raises(StalePlanError) as raised:
            workspace.run(plan)
    assert raised.value.difference == (
        f"dependency {remote.url} was resolved at {remote.first} and now names {moved}"
    )
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True]


def test_a_remote_plan_is_stale_when_a_git_dependency_leaves_a_file_while_it_is_packaged(
    tmp_path, remote_host, remote
):
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _dependency_on_commit(tmp_path, remote, remote.first)
    plan = workspace.plan(spec)
    planning, packaging = _edit_while_packaging(tmp_path, remote, None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", planning)
        patch.setattr(client, "package_inputs", packaging)
        with pytest.raises(StalePlanError) as raised:
            workspace.run(plan)
    assert raised.value.difference == f"dependency {remote.url} was removed"
    assert [request["dry_run"] for request in _host_requests(remote_host)] == [True, True]


def test_a_remote_run_refuses_a_dependency_file_that_changes_while_it_is_packaged(
    tmp_path, remote_host, remote
):
    """The same edit during a run without a plan: no plan is stale, and the host runs nothing."""
    workspace = _workspace(tmp_path)
    spec = _with_dependencies(tmp_path, "dep/dep.yaml")
    _dependency_on_commit(tmp_path, remote, remote.first)
    moved = remote.advance()
    planning, packaging = _edit_while_packaging(tmp_path, remote, moved)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_plan", planning)
        patch.setattr(client, "package_inputs", packaging)
        with pytest.raises(RemoteError, match="changed while the request was prepared"):
            workspace.run(spec)
    assert not remote_host.workspaces.exists()  # the host got no inputs and no launch


def test_a_remote_plan_and_run_accept_a_target_dependency_that_depends_on_the_root_file(
    tmp_path, remote_host
):
    """The file of the root design has a target that depends on `lib.yaml`, and `lib.yaml` depends
    on the file of the root. The second load of that file takes no target and ends the chain, so
    the planner accepts it and the packaging does too."""
    root = tmp_path / "design"
    root.mkdir()
    (root / "lib.v").write_text("module lib(input clk, output q); assign q = clk; endmodule\n")
    (root / "lib.yaml").write_text(
        "name: lib\ndependencies: [top.yaml]\nrtl:\n  sources: [lib.v]\n"
    )
    design = _design(tmp_path)
    design.write_text(
        "name: top\nrtl:\n  sources: []\n  top: lib\n  clocks: [{port: clk}]\nflows:\n  nextpnr:\n    fpga.part: "
        + PART
        + "\n"
        "    clocks.main_clock.freq: 100 MHz\ntargets:\n  test:\n    dependencies: [lib.yaml]\n"
    )
    spec = Spec("nextpnr", Design.load(design, target="test"))
    workspace = _workspace(tmp_path)
    plan = workspace.plan(spec)
    assert workspace.run(plan).outcome == "passed"


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


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry run"])
@pytest.mark.parametrize(
    "failure, code", [("unreachable", "remote_unreachable"), ("setup", "remote_setup_failed")]
)
def test_a_host_that_fails_before_the_launch_gives_no_outcome_and_exit_status_2(
    tmp_path, remote_host, monkeypatch, failure, code, dry_run
):
    if failure == "unreachable":
        monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    else:
        remote_host.remote.set_platform("Darwin", "arm64")
    design = _design(tmp_path)
    arguments = ["run", "nextpnr", str(design), "--remote", HOST, "--json"]
    arguments += ["--run-root", str(tmp_path / "local" / "xeda_run")]
    result = CliRunner().invoke(cli, arguments + (["--dry-run"] if dry_run else []))
    assert result.exit_code == 2, result.output
    document = json.loads(result.stdout)  # one document on stdout
    assert list(document) == [
        "schema_version",
        "outcome",
        "error",
        "launch_id",
        "spec",
        "roots",
        "nodes",
    ]
    assert document["outcome"] is None and document["launch_id"] is None
    assert document["roots"] == [] and document["nodes"] == []
    assert document["error"]["code"] == code and document["error"]["node"] is None
    assert document["spec"]["flow"] == "nextpnr" and document["spec"]["design_name"] == "top"
    assert document["spec"]["design_file"] == str(design)
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
    _validate(document, "run")

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
    _validate(document, "run")

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
