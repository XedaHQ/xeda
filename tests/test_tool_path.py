"""The directories in front of `PATH`: the private `path` and `tools` settings and the suite of the
tool store.

A node's `PATH` is, in this order: the `path` directories (the project-local file before the user
file), the `bin` of the selected OSS CAD Suite, then the `PATH` of the caller (or the private
`env`). A flow's `env: {PATH: ...}` replaces the whole of it, and a container gets none of it.
The directories change the freshness of a node and never its identity. No test starts a real tool:
the probe flow starts Python and records the environment of the process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

import xeda._internal.tool_store as tool_store
from xeda import local_settings
from xeda.errors import ProjectError
from xeda._internal.flow_runner.environment import (
    EnvironmentSnapshot,
    admitted,
    container_environment,
    environment_evidence,
    node_environment,
    snapshot_with_tools,
)
from xeda._internal.flow_runner.trace import read_trace
from xeda._internal.local_settings import LocalSettings
from xeda._internal.project import Project

from .launch_utils import empty_project
from .test_tool_environment import _docker_flow, _launch, _plan, _probe_flow, _seen

pytestmark = pytest.mark.python_compat

PLATFORM = tool_store.platform_tag()
needs_platform = pytest.mark.skipif(PLATFORM is None, reason="the tool store has no build for this")


def put_suite(root: Path, version: str, sha: str = "ab") -> Path:
    """Put a complete installation of the suite into the store `root`, as `install` leaves it.
    Return its `bin`."""
    location = root / "oss-cad-suite" / version / str(PLATFORM)
    program = location / "oss-cad-suite" / "bin" / "yosys"
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    (location / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tool": "oss-cad-suite",
                "version": version,
                "platform": PLATFORM,
                "url": "https://example.org/suite.tgz",
                "asset_id": 1,
                "sha256": sha * 32,
                "installed_at": "2026-11-06T00:00:00+00:00",
            }
        )
    )
    return program.parent


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty tool store, which the launches of the test use."""
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setenv("XEDA_TOOLS_ROOT", str(root))
    return root


def directories(*names: str, base: Path) -> list[Path]:
    paths = [base / name for name in names]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def path_of(ran) -> list[str]:
    return _seen(ran)["PATH"].split(os.pathsep)


# ------------------------------------------------------------------------ the private file


def write_config(path: Path, content: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(content))
    return path


def test_path_resolves_against_private_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "tools").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "cfg" / "bin").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    config = write_config(
        tmp_path / "cfg" / "config.yaml",
        {"path": ["bin", "~/tools", "../other", "bin/../bin", str(tmp_path / "other")]},
    )
    settings = local_settings(config=config)
    assert settings.path == (tmp_path / "cfg" / "bin", home / "tools", tmp_path / "other")
    assert all(entry.is_absolute() for entry in settings.path)
    assert LocalSettings().path == () and LocalSettings(path=[tmp_path]).path == (tmp_path,)


def test_project_path_precedes_user_path(tmp_path):
    project_dir = tmp_path / "project"
    a, b, c = directories("a", "b", "c", base=tmp_path)
    project_file = write_config(project_dir / "xedaproject.yaml", {"flows": {}})
    write_config(project_dir / "xedaproject.local.yaml", {"path": [str(b), str(a)]})
    user = write_config(tmp_path / "user.yaml", {"path": [str(c), str(a)]})
    settings = local_settings(Project.load(project_file), config=user)
    assert settings.path == (b, a, c)
    # An empty project list adds nothing and does not clear the user list.
    write_config(project_dir / "xedaproject.local.yaml", {"path": []})
    assert local_settings(Project.load(project_file), config=user).path == (c, a)


@pytest.mark.parametrize(
    "entry, problem",
    [
        ("missing", "is not an existing directory"),
        ("a-file", "is not an existing directory"),
        ("~someone/bin", "only `~` and `~/` are expanded"),
        ("", "must be a directory name"),
        (f"a{os.pathsep}b", "separates the directories of PATH"),
        ("a\0b", "NUL"),
        (7, "must be a directory name"),
    ],
)
def test_path_rejects_invalid_directory(tmp_path, entry, problem):
    (tmp_path / "a-file").write_text("")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"path": ["ok-dir", entry]}))
    (tmp_path / "ok-dir").mkdir()
    with pytest.raises(ProjectError) as raised:
        local_settings(config=config)
    assert raised.value.code == "invalid_local_settings"
    assert str(config) in str(raised.value) and "path[1]" in str(raised.value)
    assert problem in str(raised.value)


def test_path_must_be_a_list(tmp_path):
    config = write_config(tmp_path / "config.yaml", {"path": "bin"})
    with pytest.raises(ProjectError, match="is a list of directories"):
        local_settings(config=config)


def test_a_wrong_path_of_a_caller_is_refused_at_planning(tmp_path):
    flow = _probe_flow()
    for local, name in (
        (LocalSettings(path=[tmp_path / "missing"]), "LocalSettings.path[0]"),
        (LocalSettings(path=["relative"]), "LocalSettings.path[0]"),
        (LocalSettings(path=[tmp_path, tmp_path / "missing"]), "LocalSettings.path[1]"),
    ):
        with pytest.raises(ProjectError) as raised:
            _plan(tmp_path, flow, local=local)
        assert raised.value.code == "invalid_local_settings" and name in str(raised.value)
    # A directory that goes away between the reading of the file and the planning is refused too.
    gone = tmp_path / "gone"
    gone.mkdir()
    local = LocalSettings(path=[gone])
    gone.rmdir()
    with pytest.raises(ProjectError, match="LocalSettings.path"):
        _plan(tmp_path, flow, local=local)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "tools, problem",
    [
        ({"oss-cad-suite": "on"}, "`off` or the date"),
        ({"oss-cad-suite": "2026-10"}, "`off` or the date"),
        ({"oss-cad-suite": 3}, "`off` or the date"),
        ({"oss-cad-suite": True}, "`off` or the date"),
        ({"yosys": "off"}, "no tool of the tool store"),
    ],
)
def test_tools_rejects_invalid_selection(tmp_path, tools, problem):
    config = write_config(tmp_path / "config.yaml", {"tools": tools})
    with pytest.raises(ProjectError) as raised:
        local_settings(config=config)
    assert raised.value.code == "invalid_local_settings" and problem in str(raised.value)
    with pytest.raises(ProjectError, match="LocalSettings.tools"):
        _plan(tmp_path, _probe_flow(), local=LocalSettings(tools=tools))


def test_the_project_tools_entry_wins_over_the_user_entry(tmp_path):
    project_dir = tmp_path / "project"
    project_file = write_config(project_dir / "xedaproject.yaml", {"flows": {}})
    write_config(project_dir / "xedaproject.local.yaml", {"tools": {"oss-cad-suite": "off"}})
    user = write_config(tmp_path / "user.yaml", {"tools": {"oss-cad-suite": "2026-10-08"}})
    assert local_settings(Project.load(project_file), config=user).tools == {"oss-cad-suite": "off"}
    assert local_settings(config=user).tools == {"oss-cad-suite": "2026-10-08"}


# ------------------------------------------------------------------------ what a node sees


def snapshot(**kwargs) -> EnvironmentSnapshot:
    return EnvironmentSnapshot({"PATH": "/usr/bin", "HOME": "/h", "OTHER": "1"}, {}, **kwargs)


def test_path_override_and_container_boundary():
    flow = _probe_flow()
    prefix = ("/p/one", "/p/two", "/suite/bin")
    taken = snapshot(prefix=prefix)
    plain = flow.Settings()
    expected = os.pathsep.join([*prefix, "/usr/bin"])
    assert admitted(taken, plain, flow)["PATH"] == expected
    assert node_environment(taken, plain, flow, {})["PATH"] == expected
    # A flow `env` that sets PATH replaces the whole assembled PATH, with no prefix left.
    own = flow.Settings(env={"PATH": "/mine"})
    assert node_environment(taken, own, flow, {})["PATH"] == "/mine"
    # It holds in the clean mode too, where PATH is an essential variable of the caller.
    clean = flow.Settings(inherit_env=False)
    assert node_environment(taken, clean, flow, {})["PATH"] == expected
    assert "OTHER" not in admitted(taken, clean, flow)
    clean_own = flow.Settings(inherit_env=False, env={"PATH": "/mine"})
    assert node_environment(taken, clean_own, flow, {})["PATH"] == "/mine"
    # A container gets none of it.
    assert "PATH" not in container_environment(taken, plain, {})
    assert container_environment(taken, own, {})["PATH"] == "/mine"
    # The private PATH comes in front of the caller's, and the prefix in front of that.
    private = EnvironmentSnapshot({"PATH": "/usr/bin"}, {"PATH": "/private"}, prefix=("/p",))
    assert admitted(private, plain, flow)["PATH"] == f"/p{os.pathsep}/private"
    # Without any PATH, the default path follows.
    bare = EnvironmentSnapshot({}, {}, prefix=("/p",))
    assert admitted(bare, plain, flow)["PATH"] == f"/p{os.pathsep}{os.defpath}"
    # An empty PATH adds no empty entry, which would mean the current directory.
    empty = EnvironmentSnapshot({"PATH": ""}, {}, prefix=("/p",))
    assert admitted(empty, plain, flow)["PATH"] == "/p"
    # No prefix, no change.
    assert admitted(snapshot(), plain, flow)["PATH"] == "/usr/bin"
    assert "PATH" not in admitted(EnvironmentSnapshot({}, {}), plain, flow)


def test_the_probe_sees_the_private_path_then_the_suite_then_the_caller(tmp_path, store):
    suite_bin = put_suite(store, "2026-10-08")
    first, second = directories("first", "second", base=tmp_path)
    local = LocalSettings(path=[first, second])
    ran = _launch(tmp_path, _probe_flow(), local=local)
    parts = path_of(ran)
    assert parts[:3] == [str(first), str(second), str(suite_bin)]
    assert parts[3:] == os.environ["PATH"].split(os.pathsep)


def test_a_flow_env_path_replaces_everything(tmp_path, store):
    put_suite(store, "2026-10-08")
    (mine,) = directories("mine", base=tmp_path)
    local = LocalSettings(path=[tmp_path])
    ran = _launch(tmp_path, _probe_flow(), {"env": {"PATH": str(mine)}}, local=local)
    assert path_of(ran) == [str(mine)]


def test_a_docker_container_gets_neither_directory(tmp_path, store, monkeypatch):
    import xeda._internal.tool

    put_suite(store, "2026-10-08")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        xeda._internal.tool,
        "run_process",
        lambda executable, args=None, **kwargs: calls.append(
            ([str(a) for a in args or []], kwargs)
        ),
    )
    ran = _launch(tmp_path, _docker_flow(), {"docker": True}, local=LocalSettings(path=[tmp_path]))
    inside = (ran.run_path / ".some-tool_docker.env").read_text()
    assert "PATH" not in inside
    (kwargs,) = [kwargs for args, kwargs in calls if "arg" in args]
    assert str(tmp_path) in kwargs["env"]["PATH"]  # the `docker` client has both


# ------------------------------------------------------------------------ freshness, not identity


def test_path_changes_freshness_not_identity(tmp_path, store):
    flow = _probe_flow()
    one, two = directories("one", "two", base=tmp_path)
    ran = _launch(tmp_path, flow, local=LocalSettings(path=[one]))
    plan, states = _plan(tmp_path, flow, local=LocalSettings(path=[one]))
    assert states[flow.name].state == "fresh"
    other, other_states = _plan(tmp_path, flow, local=LocalSettings(path=[two]))
    assert other_states[flow.name].state == "stale"
    assert "changed" in other_states[flow.name].reason
    assert str(two) not in other_states[flow.name].reason
    none, _ = _plan(tmp_path, flow)
    hashes = {p.node(flow.name).flowrun_hash for p in (plan, other, none)}
    assert len(hashes) == 1
    # The trace holds the name of PATH and no directory.
    trace, _ns = read_trace(ran.run_path)
    assert "PATH" in trace.environment_names
    assert str(one) not in (ran.run_path / "trace.json").read_text()


def test_the_evidence_of_a_launch_without_directories_is_unchanged():
    flow = _probe_flow()
    plain = flow.Settings()
    base = EnvironmentSnapshot({"PATH": "/usr/bin"}, {"X": "1"})
    digest, names = environment_evidence(base, plain, flow)
    with_directories = EnvironmentSnapshot({"PATH": "/usr/bin"}, {"X": "1"}, prefix=("/p",))
    assert environment_evidence(with_directories, plain, flow)[0] != digest
    assert names == environment_evidence(with_directories, plain, flow)[1] == ["PATH", "X"]
    from xeda._internal.utils import semantic_hash

    assert digest == semantic_hash({"inherited": {"PATH": "/usr/bin"}, "private": {"X": "1"}})


# ------------------------------------------------------------------------ the suite


@needs_platform
def test_nothing_installed_adds_nothing(tmp_path, store):
    ran = _launch(tmp_path, _probe_flow())
    assert _seen(ran)["PATH"] == os.environ["PATH"]
    assert snapshot_with_tools({}).prefix == ()


@needs_platform
def test_auto_selects_the_newest_installed_suite(tmp_path, store):
    old = put_suite(store, "2026-09-15")
    pin = put_suite(store, "2026-10-08")
    newest = put_suite(store, "2026-12-01")
    assert len({old, pin, newest}) == 3
    ran = _launch(tmp_path, _probe_flow())
    assert path_of(ran)[0] == str(newest)
    assert str(old) not in path_of(ran) and str(pin) not in path_of(ran)


@needs_platform
def test_off_and_exact_version(tmp_path, store):
    old = put_suite(store, "2026-09-15")
    newest = put_suite(store, "2026-12-01")
    flow = _probe_flow()
    off = _launch(tmp_path, flow, local=LocalSettings(tools={"oss-cad-suite": "off"}), run="off")
    assert str(newest) not in path_of(off) and _seen(off)["PATH"] == os.environ["PATH"]
    exact = _launch(
        tmp_path, flow, local=LocalSettings(tools={"oss-cad-suite": "2026-09-15"}), run="exact"
    )
    assert path_of(exact)[0] == str(old) and str(newest) not in path_of(exact)
    # A date that the store lacks is refused at planning, before any run directory exists.
    from xeda.errors import SpecError

    missing = LocalSettings(tools={"oss-cad-suite": "2026-01-01"})
    with pytest.raises(SpecError) as raised:
        _plan(tmp_path, flow, local=missing, run="missing")
    assert raised.value.code == "tool_not_installed" and "2026-01-01" in str(raised.value)
    with pytest.raises(SpecError) as raised:
        _launch(tmp_path, flow, local=missing, run="missing")
    assert raised.value.code == "tool_not_installed"
    assert not (tmp_path / "missing").exists()


@needs_platform
def test_the_private_env_names_the_store_of_a_run(tmp_path, store):
    elsewhere = tmp_path / "elsewhere"
    in_elsewhere = put_suite(elsewhere, "2026-09-15")
    put_suite(store, "2026-12-01")
    local = LocalSettings(env={"XEDA_TOOLS_ROOT": str(elsewhere)})
    ran = _launch(tmp_path, _probe_flow(), local=local)
    assert path_of(ran)[0] == str(in_elsewhere)
    assert _seen(ran)["XEDA_TOOLS_ROOT"] == str(elsewhere)


def test_a_relative_store_root_is_refused_at_planning(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_TOOLS_ROOT", "relative")
    from xeda.errors import SettingsError

    with pytest.raises(SettingsError) as raised:
        _plan(tmp_path, _probe_flow())
    assert raised.value.code == "invalid_value"


@needs_platform
def test_suite_preserves_python_environment(tmp_path, store, monkeypatch):
    """Only PATH changes: no Python, library or activation variable, and Xeda's own process."""
    put_suite(store, "2026-10-08")
    monkeypatch.setenv("PYTHONPATH", "/xeda-sentinel/pythonpath")
    monkeypatch.setenv("VIRTUAL_ENV", "/xeda-sentinel/venv")
    before = dict(os.environ)
    ran = _launch(tmp_path, _probe_flow())
    seen = _seen(ran)
    assert dict(os.environ) == before
    changed = {
        name
        for name in seen.keys() | before.keys()
        if seen.get(name) != before.get(name) and not name.startswith("PYTEST_")
    }
    assert "PATH" in changed and changed <= {"PATH", "PWD"}  # PWD: the working directory
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        assert seen.get(name) == before.get(name)
    assert not any(key.startswith("QT_") for key in set(seen) - set(before))


@needs_platform
def test_a_new_installation_changes_freshness_not_identity(tmp_path, store):
    flow = _probe_flow()
    _launch(tmp_path, flow)
    plan, states = _plan(tmp_path, flow)
    assert states[flow.name].state == "fresh"
    put_suite(store, "2026-12-01")
    after, states = _plan(tmp_path, flow)
    assert states[flow.name].state == "stale"
    assert after.node(flow.name).flowrun_hash == plan.node(flow.name).flowrun_hash
    # Another build of the same date (other receipt) is another suite.
    ran = _launch(tmp_path, flow)
    put_suite(store, "2026-12-01", sha="cd")
    _, states = _plan(tmp_path, flow)
    assert states[flow.name].state == "stale"
    assert ran is not None


@needs_platform
def test_remote_uses_host_tool_selection(tmp_path, store):
    """The remote entry gives the launcher no private settings: the host reads its own store from
    its own environment, and a client's `path` and `tools` never reach it."""
    from xeda._internal.package_paths import impl_root

    suite_bin = put_suite(store, "2026-10-08")
    assert snapshot_with_tools({}).prefix == (str(suite_bin),)
    entry = (impl_root() / "remote" / "launch_entry.py").read_text(encoding="utf-8")
    assert "local_settings=None" in entry
    remote = "".join(p.read_text(encoding="utf-8") for p in (impl_root() / "remote").glob("*.py"))
    assert "private_path" not in remote and "private_tools" not in remote
    assert "LocalSettings.path" not in remote and ".tools" not in remote.replace("tools_", "")
    assert empty_project(tmp_path)
