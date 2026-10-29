"""The private settings files: the user file and the project-local file.

`local_settings()` and the command line read them. The launcher is only handed the result, and it
puts the two `flows` tables in their places among the layers of the settings of a request.
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design, errors
from xeda.cli import cli
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import (
    PROJECT_LOCAL_ORIGIN,
    USER_CONFIG_ORIGIN,
    project_local_origin,
    user_config_origin,
)
from xeda.frozen import FrozenDict
from xeda.local_settings import LOCAL_SETTINGS_KEYS, LocalSettings, local_settings
from xeda.project import Project
from xeda.remote.errors import RemoteConfigError
from xeda.types import UNSET

from .io_flows import TReadsInstalled
from .project_files import PROJECT_FILE
from .test_project import PART, design_file, rtl, write_project


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """The user-level directories of the test are below `tmp_path`: no test reads the real
    config of the user."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XEDA_RUN_ROOT", raising=False)
    return tmp_path / "cfg" / "xeda"


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False))
    return path


def user_file(private_home: Path, data) -> Path:
    return write(private_home / "config.yaml", data)


def project_of(tmp_path: Path, name: str = PROJECT_FILE, **keys) -> Project:
    return Project.load(
        write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}], **keys}, name)
    )


def local_file_of(project: Project) -> Path:
    return project.path.with_name("xedaproject.local.yaml")


# ------------------------------------------------------------------------------ the value


def test_local_settings_is_a_plain_keyword_value():
    empty = LocalSettings()
    assert empty.user == {} and empty.local == {} and empty.env == {}
    assert empty.run_root is None and empty.host_profiles == {}
    assert empty.user_file is None and empty.local_file is None
    with pytest.raises(TypeError):
        LocalSettings({"a": {}})  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        empty.run_root = Path("x")  # type: ignore[misc]


def test_the_tables_of_a_local_settings_cannot_change():
    table = {"vivado_synth": {"nthreads": 8}}
    ls = LocalSettings(user=table)
    table["vivado_synth"]["nthreads"] = 1
    assert ls.user["vivado_synth"]["nthreads"] == 8
    assert isinstance(ls.user, FrozenDict)
    with pytest.raises(TypeError):
        ls.user["x"] = 1  # type: ignore[index]


def test_the_private_keys_are_these():
    assert LOCAL_SETTINGS_KEYS == ("flows", "env", "run_root")


# ------------------------------------------------------------------------------ the files


def test_the_user_file_is_in_the_xdg_config_home(tmp_path, private_home):
    path = user_file(
        private_home,
        {"flows": {"vivado_synth": {"nthreads": 8}}, "env": {"LM_LICENSE_FILE": "2100@lic"}},
    )
    ls = local_settings()
    assert ls.user == {"vivado_synth": {"nthreads": 8}}
    assert ls.env == {"LM_LICENSE_FILE": "2100@lic"}
    assert ls.user_file == path
    assert ls.local == {} and ls.local_file is None and ls.run_root is None


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_an_empty_or_relative_xdg_config_home_means_the_home_config(tmp_path, monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    path = write(tmp_path / "home" / ".config" / "xeda" / "config.yaml", {"run_root": "r"})
    assert local_settings().user_file == path


def test_absent_files_are_no_layer(tmp_path):
    project = project_of(tmp_path)
    ls = local_settings(project)
    assert ls == LocalSettings(run_root=project.run_root)


def test_an_empty_file_is_no_layer(tmp_path, private_home):
    user_file(private_home, "")
    ls = local_settings()
    assert ls.user == {} and ls.user_file == private_home / "config.yaml"


@pytest.mark.parametrize("name", ["xedaproject.yaml", "xedaproject.yml", "xedaproject.toml"])
def test_the_project_local_file_is_beside_the_project_file(tmp_path, name):
    directory = tmp_path / "repo"
    if name.endswith(".toml"):
        directory.mkdir()
        (directory / "knight.v").write_text("module knight(); endmodule\n")
        path = directory / name
        path.write_text('[[designs]]\nname = "d"\n[designs.rtl]\nsources = ["knight.v"]\n')
        project = Project.load(path)
    else:
        project = Project.load(
            write_project(directory, {"designs": [{"name": "d", "rtl": rtl()}]}, name)
        )
    local = write(directory / "xedaproject.local.yaml", {"flows": {"yosys": {"abc9": True}}})
    ls = local_settings(project)
    assert ls.local == {"yosys": {"abc9": True}}
    assert ls.local_file == local


def test_there_is_no_upward_search_for_the_project_local_file(tmp_path):
    project = project_of(tmp_path)
    write(tmp_path / "xedaproject.local.yaml", {"run_root": "up"})
    write(tmp_path / "repo" / "sub" / "xedaproject.local.yaml", {"run_root": "down"})
    ls = local_settings(project)
    assert ls.local_file is None and ls.run_root == project.run_root


def test_without_a_project_there_is_no_project_local_file(tmp_path):
    write(tmp_path / "xedaproject.local.yaml", {"run_root": "x"})
    assert local_settings().local_file is None


def test_config_names_the_file_in_place_of_the_user_file(tmp_path, private_home):
    user_file(private_home, {"run_root": "from_user"})
    other = write(tmp_path / "other.yaml", {"flows": {"yosys": {"abc9": True}}})
    project = project_of(tmp_path)
    local = write(local_file_of(project), {"run_root": "local"})
    ls = local_settings(project, config=other)
    assert ls.user_file == other and ls.user == {"yosys": {"abc9": True}}
    assert ls.local_file == local, "--config leaves the project-local file as it is"
    assert ls.run_root == (tmp_path / "repo" / "local")


def test_no_config_reads_neither_private_file(tmp_path, private_home):
    user_file(private_home, {"run_root": "u", "flows": {"yosys": {"abc9": True}}})
    project = project_of(tmp_path, run_root="checked_in")
    write(local_file_of(project), {"run_root": "l", "env": {"A": "b"}})
    ls = local_settings(project, config=None)
    assert ls.user == {} and ls.local == {} and ls.env == {}
    assert ls.user_file is None and ls.local_file is None
    assert ls.run_root == tmp_path / "repo" / "checked_in"


def test_the_environments_of_both_files_merge_and_the_project_local_one_wins(
    tmp_path, private_home
):
    user_file(private_home, {"env": {"A": "user", "B": "user"}})
    project = project_of(tmp_path)
    write(local_file_of(project), {"env": {"B": "local", "C": "local"}})
    assert local_settings(project).env == {"A": "user", "B": "local", "C": "local"}


def test_the_run_root_comes_from_the_project_local_file_the_user_file_then_the_project(
    tmp_path, private_home
):
    project = project_of(tmp_path, run_root="checked_in")
    root = tmp_path / "repo"
    assert local_settings(project).run_root == root / "checked_in"
    user_file(private_home, {"run_root": "/abs/user"})
    assert local_settings(project).run_root == Path("/abs/user")
    write(local_file_of(project), {"run_root": "../local"})
    assert local_settings(project).run_root == tmp_path / "local"
    assert local_settings(project, config=None).run_root == root / "checked_in"


def test_a_project_without_a_run_root_gives_the_default_beside_the_project_file(tmp_path):
    project = project_of(tmp_path)
    assert local_settings(project).run_root == project.run_root == tmp_path / "repo" / "xeda_run"
    assert local_settings().run_root is None


def test_a_run_root_is_relative_to_the_file_that_names_it(tmp_path, private_home):
    user_file(private_home, {"run_root": "runs"})
    assert local_settings().run_root == private_home / "runs"


def test_dotted_keys_expand_as_in_a_project_file(tmp_path, private_home):
    user_file(private_home, "flows.vivado_synth.nthreads: 8\nflows.yosys.abc9: true\n")
    assert local_settings().user == {"vivado_synth": {"nthreads": 8}, "yosys": {"abc9": True}}


@pytest.mark.parametrize(
    "text, message",
    [
        ("- a\n- b\n", "must hold a mapping"),
        ("flows: [", "line"),
        ("flowz: {}\n", "did you mean 'flows'"),
        ("colour: 1\n", "unknown key(s) 'colour'"),
        ("flows: [a]\n", "must be a mapping of flow names"),
        ("flows: {yosys: 3}\n", "yosys"),
        ("env: [a]\n", "`env` is a mapping"),
        ("env: {JOBS: 8}\n", "`env.JOBS` is text"),
        ("env: {'A=B': x}\n", "no environment variable name"),
        ("run_root: 3\n", "`run_root` is a path"),
        ("run_root: ''\n", "`run_root` is a path"),
    ],
)
@pytest.mark.parametrize("which", ["user", "local"])
def test_a_malformed_private_file_is_refused_and_named(
    tmp_path, private_home, text, message, which
):
    project = project_of(tmp_path)
    path = (
        write(private_home / "config.yaml", text)
        if which == "user"
        else write(local_file_of(project), text)
    )
    with pytest.raises(errors.ProjectError) as raised:
        local_settings(project)
    assert raised.value.code == "invalid_local_settings"
    assert str(path) in str(raised.value) and message in str(raised.value)


@pytest.mark.parametrize("key", ["designs", "design"])
def test_a_designs_key_in_a_private_file_is_refused(tmp_path, private_home, key):
    path = user_file(private_home, {key: [{"name": "x", "rtl": rtl()}]})
    with pytest.raises(errors.ProjectError) as raised:
        local_settings()
    assert raised.value.code == "invalid_local_settings"
    assert "never declares a design" in str(raised.value) and str(path) in str(raised.value)


def test_a_private_file_that_is_a_directory_is_refused(tmp_path):
    (tmp_path / "dir.yaml").mkdir()
    with pytest.raises(errors.ProjectError) as raised:
        local_settings(config=tmp_path / "dir.yaml")
    assert raised.value.code == "invalid_local_settings"


def test_the_host_profiles_are_read_in_every_case(tmp_path, private_home):
    write(private_home / "remotes.yaml", {"hosts": {"venona": {"root": "~/x"}}})
    project = project_of(tmp_path)
    for ls in (
        local_settings(),
        local_settings(project, config=None),
        local_settings(project, config=write(tmp_path / "other.yaml", {})),
    ):
        assert ls.host_profiles["venona"]["root"] == "~/x"
        assert set(ls.host_profiles) == {"venona"}


def test_no_library_call_reads_a_private_file_unasked(tmp_path, private_home):
    """Planning with no `local_settings` ignores a user file that would not load."""
    user_file(private_home, "colour: 1\n")
    project = project_of(tmp_path)
    write(local_file_of(project), "colour: 1\n")
    design = project.design("d")
    plan = DefaultRunner(tmp_path / "run").plan(
        "vivado_synth", design, flow_settings=["fpga.part=xc7a100tcsg324-1"]
    )
    assert plan.requested == "vivado_synth"


# ------------------------------------------------------------------------------ the layers


SEEDS = {
    "user": 1,
    "host": 2,
    "project": 3,
    "design": 4,
    "target": 5,
    "local": 6,
    "explicit": 7,
}
ORDER = list(SEEDS)


def layered_request(tmp_path: Path, present: list[str]):
    """The request of `nextpnr` with `seed` written in each layer of `present`."""
    flows = lambda layer: {"nextpnr": {"seed": SEEDS[layer]}}  # noqa: E731
    entry = {"name": "d", "rtl": rtl()}
    if "design" in present:
        entry["flows"] = flows("design")
    if "target" in present:
        entry["targets"] = {"t": {"flows": flows("target")}}
    else:
        entry["targets"] = {"t": {}}
    project = Project.load(
        write_project(
            tmp_path / "repo",
            {"designs": [entry], **({"flows": flows("project")} if "project" in present else {})},
        )
    )
    design = project.design("d", target="t")
    ls = LocalSettings(
        user=flows("user") if "user" in present else {},
        local=flows("local") if "local" in present else {},
        user_file=tmp_path / "u.yaml",
        local_file=tmp_path / "l.yaml",
    )
    return DefaultRunner(tmp_path / "run")._request(
        "nextpnr",
        design,
        local_settings=ls,
        host_layer=("the host profile of h", flows("host")) if "host" in present else None,
        flow_overrides=flows("explicit")["nextpnr"] if "explicit" in present else {},
    )


def test_the_eight_layers_apply_in_order_with_their_origins(tmp_path):
    present = list(ORDER)
    while present:
        request = layered_request(tmp_path / str(len(present)), present)
        assert request.settings["seed"] == SEEDS[present[-1]], present
        present.pop()
    assert layered_request(tmp_path / "none", []).settings.get("seed") is None


def test_each_file_layer_is_labeled_with_its_file(tmp_path):
    request = layered_request(tmp_path, ORDER)
    labels = [label for label, _ in request.origins]
    assert labels[0] == f"the user config {tmp_path / 'u.yaml'}"
    assert labels[1] == "the host profile of h"
    assert labels[-1] == f"the project-local file {tmp_path / 'l.yaml'}"
    assert len(labels) == 5  # user, host, project, design (with the target), project-local


def test_the_origin_texts_name_the_file_or_the_layer():
    assert user_config_origin(None) == USER_CONFIG_ORIGIN == "the user config"
    assert project_local_origin(None) == PROJECT_LOCAL_ORIGIN == "the project-local file"
    assert user_config_origin(Path("/a/config.yaml")) == "the user config /a/config.yaml"
    assert project_local_origin(Path("/a/x.local.yaml")) == "the project-local file /a/x.local.yaml"


def test_the_layers_merge_key_by_key(tmp_path):
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": [], "top": "t"},
        flows={"nextpnr": {"seed": 4, "router": "router1"}},
    )
    ls = LocalSettings(
        user={"nextpnr": {"seed": 1, "placer": "heap"}}, local={"nextpnr": {"router": "router2"}}
    )
    request = DefaultRunner(tmp_path / "run")._request("nextpnr", design, local_settings=ls)
    assert request.settings == {"seed": 4, "router": "router2", "placer": "heap"}


def test_an_entry_for_a_flow_outside_the_plan_is_ignored(tmp_path):
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    ls = LocalSettings(user={"yosys": {"abc9": True}, "vivado_synth": {"nthreads": 3}})
    plan = DefaultRunner(tmp_path / "run").plan(TReadsInstalled, design, local_settings=ls)
    assert [node.flow_class.name for node in plan.nodes] == [TReadsInstalled.name]


def test_a_removed_flow_name_in_a_private_file_is_refused(tmp_path):
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    with pytest.raises(errors.FlowNotFoundError):
        DefaultRunner(tmp_path / "run").plan(
            TReadsInstalled, design, local_settings=LocalSettings(user={"openxc7": {}})
        )


def test_a_relative_path_in_a_private_file_is_relative_to_that_file(tmp_path):
    directory = tmp_path / "cfg"
    (tmp_path / "other").mkdir()
    design = Design(name="d", root=tmp_path / "other", rtl={"sources": [], "top": "t"})
    ls = LocalSettings(
        user={TReadsInstalled.name: {"store": "pdks"}}, user_file=directory / "config.yaml"
    )
    request = DefaultRunner(tmp_path / "run")._request(TReadsInstalled, design, local_settings=ls)
    assert Path(request.settings["store"]) == directory.resolve() / "pdks"


def test_a_private_value_and_the_same_checked_in_value_give_one_identity(tmp_path):
    def hash_of(design: Design, ls: LocalSettings | None) -> str:
        plan = DefaultRunner(tmp_path / "run").plan(TReadsInstalled, design, local_settings=ls)
        return plan.node(plan.requested).flowrun_hash

    name = TReadsInstalled.name
    bare = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    written = Design(
        name="d", root=tmp_path, rtl={"sources": [], "top": "t"}, flows={name: {"text": "x"}}
    )
    checked_in = hash_of(written, None)
    assert hash_of(bare, LocalSettings(user={name: {"text": "x"}})) == checked_in
    assert hash_of(bare, LocalSettings(local={name: {"text": "x"}})) == checked_in
    assert hash_of(bare, LocalSettings()) != checked_in


def test_a_pdk_root_from_any_layer_changes_no_identity(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    name = TReadsInstalled.name
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})

    def hash_of(ls: LocalSettings | None) -> str:
        plan = DefaultRunner(tmp_path / "run").plan(TReadsInstalled, design, local_settings=ls)
        return plan.node(plan.requested).flowrun_hash

    plain = hash_of(None)
    for ls in (
        LocalSettings(user={name: {"store": str(tmp_path / "a")}}),
        LocalSettings(local={name: {"store": str(tmp_path / "b")}}),
    ):
        assert hash_of(ls) == plain


# ------------------------------------------------------------------------------ the doors


def test_the_doors_that_take_a_project_take_the_private_settings():
    import inspect

    from xeda.flow_runner.default_runner import FlowLauncher

    for name in ("run", "plan", "plan_with_freshness", "prepare_request", "resolve_run"):
        assert "local_settings" in inspect.signature(getattr(FlowLauncher, name)).parameters, name
    for name in ("resolve", "run_flow", "launch_flow"):
        assert "local_settings" not in inspect.signature(getattr(FlowLauncher, name)).parameters


# ------------------------------------------------------------------------------ the command line


def invoke(*args: str):
    result = CliRunner().invoke(cli, [*args, "--json"], catch_exceptions=False)
    return result, json.loads(result.stdout)


def dry_run(project: Path, *extra: str):
    return invoke(
        "run", "vivado_synth", "--xedaproject", str(project), "--design-name", "d", "--dry-run",
        *PART, *extra,
    )  # fmt: skip


def run_path_of(document) -> Path:
    return Path(document["plan"]["nodes"][-1]["run_path"])


def hash_of(document) -> str:
    return document["plan"]["nodes"][-1]["flowrun_hash"]


def test_the_cli_run_root_order(tmp_path, private_home, monkeypatch):
    repo = tmp_path / "repo"
    path = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "project"})
    arguments = ("--xedaproject", str(path), "--design-name", "d", "--dry-run", *PART)
    local = write(repo / "xedaproject.local.yaml", {"run_root": "local"})
    user_file(private_home, {"run_root": "user"})

    def root_of(*extra: str, env=None) -> Path:
        result = CliRunner().invoke(
            cli, ["run", "vivado_synth", *arguments, *extra, "--json"], env=env
        )
        assert result.exit_code == 0, result.output
        return run_path_of(json.loads(result.stdout))

    given = str(tmp_path / "given")
    assert root_of("--run-root", given, env={"XEDA_RUN_ROOT": "env"}).is_relative_to(
        Path(given).resolve()
    )
    assert root_of(env={"XEDA_RUN_ROOT": str(tmp_path / "env")}).is_relative_to(tmp_path / "env")
    assert root_of().is_relative_to((repo / "local").resolve())
    local.unlink()
    assert root_of().is_relative_to(private_home / "user")
    (private_home / "config.yaml").unlink()
    assert root_of().is_relative_to((repo / "project").resolve())
    path.write_text(yaml.safe_dump({"designs": [{"name": "d", "rtl": rtl()}]}))
    assert root_of().is_relative_to(repo / "xeda_run")


def test_no_config_leaves_out_the_private_run_roots(tmp_path, private_home):
    path = write_project(
        tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "project"}
    )
    user_file(private_home, {"run_root": "user"})
    result, document = dry_run(path, "--no-config")
    assert run_path_of(document).is_relative_to((tmp_path / "repo" / "project").resolve())


def test_config_names_the_user_file(tmp_path, private_home):
    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    user_file(private_home, {"run_root": "user"})
    other = write(tmp_path / "other.yaml", {"run_root": "other"})
    result, document = dry_run(path, "--config", str(other))
    assert run_path_of(document).is_relative_to(tmp_path / "other")


def test_config_and_no_config_exclude_each_other(tmp_path):
    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(path), "--design-name", "d",
         "--config", str(tmp_path / "c.yaml"), "--no-config", "--json"],
    )  # fmt: skip
    assert result.exit_code == 2
    assert (
        "`--config` and `--no-config` exclude each other"
        in json.loads(result.stdout)["error"]["message"]
    )


def test_a_private_value_and_the_same_value_on_the_command_line_give_one_identity(
    tmp_path, private_home
):
    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    _, plain = dry_run(path)
    _, on_the_line = dry_run(path, "-s", "vivado_synth.nthreads=3")
    assert hash_of(plain) != hash_of(on_the_line)
    user_file(private_home, {"flows": {"vivado_synth": {"nthreads": 3}}})
    _, from_user = dry_run(path)
    assert hash_of(from_user) == hash_of(on_the_line)
    _, ignored = dry_run(path, "--no-config")
    assert hash_of(ignored) == hash_of(plain)
    (private_home / "config.yaml").unlink()
    write(
        tmp_path / "repo" / "xedaproject.local.yaml", {"flows": {"vivado_synth": {"nthreads": 3}}}
    )
    _, from_local = dry_run(path)
    assert hash_of(from_local) == hash_of(on_the_line)
    # The value on the command line wins over both.
    _, over = dry_run(path, "-s", "vivado_synth.nthreads=3")
    assert hash_of(over) == hash_of(on_the_line)


def test_the_project_local_file_overrides_the_project_and_the_user_file_is_below_it(
    tmp_path, private_home
):
    path = write_project(
        tmp_path / "repo",
        {
            "designs": [{"name": "d", "rtl": rtl()}],
            "flows": {"vivado_synth": {"nthreads": 2}},
        },
    )
    user_file(private_home, {"flows": {"vivado_synth": {"nthreads": 5}}})
    _, from_project = dry_run(path, "--no-config")
    _, user_below_project = dry_run(path)
    assert hash_of(user_below_project) == hash_of(from_project)
    write(
        tmp_path / "repo" / "xedaproject.local.yaml", {"flows": {"vivado_synth": {"nthreads": 9}}}
    )
    _, local_wins = dry_run(path)
    _, nine = dry_run(path, "--no-config", "-s", "vivado_synth.nthreads=9")
    assert hash_of(local_wins) == hash_of(nine)


def test_a_malformed_private_file_fails_the_command(tmp_path, private_home):
    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    user_file(private_home, "colour: 1\n")
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(path), "--design-name", "d", *PART,
         "--dry-run", "--json"],
    )  # fmt: skip
    document = json.loads(result.stdout)
    assert result.exit_code == 2
    assert document["error"]["code"] == "invalid_local_settings"


def test_dse_takes_the_private_options_and_run_root(tmp_path, private_home, monkeypatch):
    from xeda.flow_runner.dse import Dse

    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    user_file(private_home, {"run_root": "dse_runs"})
    seen = {}

    class Stop(Exception):
        pass

    def fake_init(self, *args, **kwargs):
        seen["run_root"] = kwargs["run_root"]
        raise Stop

    monkeypatch.setattr(Dse, "__init__", fake_init)
    CliRunner().invoke(
        cli,
        ["dse", "vivado_synth", "--xedaproject", str(path), "--design-name", "d",
         "--optimizer", "fmax_optimizer", *PART],
    )  # fmt: skip
    assert Path(seen["run_root"]) == private_home / "dse_runs"
    seen.clear()
    CliRunner().invoke(
        cli,
        ["dse", "vivado_synth", "--xedaproject", str(path), "--design-name", "d",
         "--optimizer", "fmax_optimizer", "--no-config", *PART],
    )  # fmt: skip
    assert Path(seen["run_root"]) == tmp_path / "repo" / "xeda_run"


def test_the_cli_passes_the_host_profile_and_the_private_settings_to_the_remote(
    tmp_path, private_home, monkeypatch
):
    from xeda.remote import client

    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    write(private_home / "remotes.yaml", {"hosts": {"venona": {"root": "~/x"}}})
    user_file(private_home, {"flows": {"vivado_synth": {"nthreads": 3}}})
    seen = {}

    def fake_run_remote(chain, **arguments):
        seen.update(arguments)
        raise SystemExit(0)

    monkeypatch.setattr(client, "run_remote", fake_run_remote)
    CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(path), "--design-name", "d",
         "--remote", "venona", *PART],
    )  # fmt: skip
    assert seen["profile"].root == "~/x"
    assert seen["local_settings"].user == {"vivado_synth": {"nthreads": 3}}
    assert seen["local_settings"].user_file == private_home / "config.yaml"
    seen.clear()
    CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(path), "--design-name", "d",
         "--remote", "other", "--no-config", *PART],
    )  # fmt: skip
    assert seen["profile"].root is None
    assert seen["local_settings"].user == {}


def test_the_unset_default_of_config_is_the_user_file(tmp_path, private_home):
    user_file(private_home, {"run_root": "x"})
    assert local_settings(config=UNSET).user_file == private_home / "config.yaml"
    assert os.fspath(local_settings().user_file) == os.fspath(private_home / "config.yaml")


# ------------------------------------------------------------------------------ one place


def test_only_the_command_line_and_local_settings_look_for_the_private_files():
    """The library is handed settings and a host profile. Four modules name a private file or
    ask where the profile file is: `local_settings`, the command line, and the `xeda remote`
    commands, which are command line too."""
    source = Path(__file__).parent.parent / "src" / "xeda"
    allowed = {"local_settings.py", "cli.py", "remote/commands.py", "remote/profile.py"}
    found = {
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if any(
            text in path.read_text()
            for text in ("remotes_file()", "config_directory()", '".local.yaml"', "config.yaml")
        )
    }
    assert found <= allowed, found - allowed


def test_a_remote_request_carries_the_private_layers_in_its_sections(tmp_path):
    """The host runs the merged sections: it reads no private file of its own."""
    from xeda.local_settings import LocalSettings

    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    ls = LocalSettings(
        user={TReadsInstalled.name: {"text": "user"}},
        local={TReadsInstalled.name: {"text": "local"}},
    )
    prepared = DefaultRunner(tmp_path / "run").prepare_request(
        TReadsInstalled, design, host_flows={TReadsInstalled.name: {"text": "host"}},
        local_settings=ls,
    )  # fmt: skip
    assert prepared.sections[TReadsInstalled.name]["text"] == "local"


def test_a_config_file_that_does_not_exist_is_an_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(errors.ProjectError) as raised:
        local_settings(config=missing)
    assert raised.value.code == "invalid_local_settings"
    assert str(missing) in str(raised.value) and "no such file" in str(raised.value)


def test_the_default_user_file_and_the_project_local_file_may_be_missing(tmp_path):
    project = project_of(tmp_path)
    ls = local_settings(project)
    assert ls.user_file is None and ls.local_file is None


def test_the_command_line_refuses_a_config_file_that_does_not_exist(tmp_path):
    path = write_project(tmp_path / "repo", {"designs": [{"name": "d", "rtl": rtl()}]})
    missing = tmp_path / "nope.yaml"
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(path), "--design-name", "d", *PART,
         "--dry-run", "--config", str(missing), "--json"],
    )  # fmt: skip
    document = json.loads(result.stdout)
    assert result.exit_code == 2
    assert document["error"]["code"] == "invalid_local_settings"
    assert str(missing) in document["error"]["message"]


@pytest.mark.parametrize("text", ["hosts: [", "- venona\n", "hosts:\n  venona: {root: 'a b'}\n"])
def test_malformed_host_profiles_are_an_invalid_local_settings_error(private_home, text):
    path = write(private_home / "remotes.yaml", text)
    with pytest.raises(errors.ProjectError) as raised:
        local_settings(config=None)
    assert raised.value.code == "invalid_local_settings"
    assert str(path) in str(raised.value)
    assert not isinstance(raised.value, RemoteConfigError)


def test_remote_commands_still_report_malformed_host_profiles_as_remote_config_errors(tmp_path):
    from xeda.remote.profile import read_host_profiles

    path = write(tmp_path / "remotes.yaml", "hosts: [")
    with pytest.raises(RemoteConfigError):
        read_host_profiles(path)


@pytest.mark.parametrize("which", ["user", "local"])
def test_a_flow_name_that_is_no_flow_in_a_private_file_is_refused_naming_the_file(tmp_path, which):
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    file = tmp_path / ("config.yaml" if which == "user" else "x.local.yaml")
    ls = LocalSettings(**{which: {"vivadosynth": {"nthreads": 3}}, f"{which}_file": file})
    with pytest.raises(errors.FlowNotFoundError) as raised:
        DefaultRunner(tmp_path / "run").plan(TReadsInstalled, design, local_settings=ls)
    assert raised.value.code == "flow_not_found" and raised.value.name == "vivadosynth"
    assert "vivado_synth" in raised.value.suggestions
    assert str(file) in str(raised.value)


def test_a_flow_name_that_is_no_flow_in_the_other_layers_is_still_left_alone(tmp_path):
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": [], "top": "t"},
        flows={"a_plugin_not_installed": {"x": 1}},
    )
    plan = DefaultRunner(tmp_path / "run").plan(TReadsInstalled, design)
    assert plan.requested == TReadsInstalled.name


def test_a_design_file_gets_the_user_file_and_no_project_or_project_local_layer(
    tmp_path, private_home
):
    repo = tmp_path / "repo"
    path = write_project(
        repo,
        {
            "designs": [{"name": "d", "rtl": rtl()}],
            "run_root": "project",
            "flows": {"vivado_synth": {"nthreads": 2}},
        },
    )
    other = design_file(repo, "other.yaml")
    write(repo / "xedaproject.local.yaml", {"flows": {"vivado_synth": {"nthreads": 9}}})
    user_file(private_home, {"flows": {"vivado_synth": {"nthreads": 3}}})
    arguments = ("run", "vivado_synth", "--xedaproject", str(path), str(other), "--dry-run", *PART)
    _, document = invoke(*arguments)
    _, expected = invoke(*arguments, "--no-config", "-s", "vivado_synth.nthreads=3")
    assert hash_of(document) == hash_of(expected)
    assert run_path_of(document).is_relative_to((repo / "project").resolve())


def test_the_run_root_of_a_design_file_follows_the_order_with_the_project_local_one(
    tmp_path, private_home
):
    repo = tmp_path / "repo"
    path = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "project"})
    other = design_file(repo, "other.yaml")
    arguments = ("run", "vivado_synth", "--xedaproject", str(path), str(other), "--dry-run", *PART)
    local = write(repo / "xedaproject.local.yaml", {"run_root": "local"})
    user_file(private_home, {"run_root": "user"})
    _, document = invoke(*arguments)
    assert run_path_of(document).is_relative_to((repo / "local").resolve())
    local.unlink()
    _, document = invoke(*arguments)
    assert run_path_of(document).is_relative_to(private_home / "user")
    _, document = invoke(*arguments, "--no-config")
    assert run_path_of(document).is_relative_to((repo / "project").resolve())


def test_dse_of_a_design_file_follows_the_same_run_root_order(tmp_path, private_home, monkeypatch):
    from xeda.flow_runner.dse import Dse

    repo = tmp_path / "repo"
    path = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}]})
    other = design_file(repo, "other.yaml")
    write(repo / "xedaproject.local.yaml", {"run_root": "local"})
    seen = {}

    def fake_init(self, *args, **kwargs):
        seen["run_root"] = kwargs["run_root"]
        raise RuntimeError("stop")

    monkeypatch.setattr(Dse, "__init__", fake_init)
    CliRunner().invoke(
        cli,
        ["dse", "vivado_synth", str(other), "--xedaproject", str(path),
         "--optimizer", "fmax_optimizer", *PART],
    )  # fmt: skip
    assert Path(seen["run_root"]) == (repo / "local").resolve()


def test_a_launch_protects_the_private_files_it_loaded(tmp_path, private_home):
    user = user_file(private_home, {"run_root": "runs"})
    project = project_of(tmp_path)
    local = write(local_file_of(project), {"run_root": "local"})
    runner = DefaultRunner(tmp_path / "run")
    runner._request("vivado_synth", project.design("d"), local_settings=local_settings(project))
    assert {user.resolve(), local.resolve()} <= set(runner._launch_inputs)
    runner._request("vivado_synth", project.design("d"), local_settings=local_settings(config=None))
    assert not {user.resolve(), local.resolve()} & set(runner._launch_inputs)


def test_a_remote_launch_protects_the_private_files_it_loaded(tmp_path, private_home):
    from xeda.deliver import ReadInputs
    from xeda.remote import client

    user = user_file(private_home, {"run_root": "runs"})
    project = project_of(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    prepared = runner.prepare_request(
        "vivado_synth",
        project.design("d"),
        flow_settings=["fpga.part=xc7a100tcsg324-1"],
        local_settings=local_settings(project),
    )
    seen: list = []

    def inputs(design, settings, sections, launch_inputs, **kwargs):
        seen.extend(launch_inputs)
        return ReadInputs(launch_inputs)

    from types import SimpleNamespace

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "remote_read_inputs", inputs)
        client._check_outputs_to(
            runner,
            prepared,
            SimpleNamespace(mirror_root=tmp_path / "mirror", design="d"),
            outputs_to=tmp_path / "out",
            overwrite_outputs=False,
            confirm_overwrite=None,
        )
    assert user.resolve() in seen


def test_a_design_file_beside_a_project_still_protects_the_project_local_file(
    tmp_path, private_home
):
    """The design file gets no layer from the project-local file, but the file was read (its
    `run_root` counts), so a deliverable may not replace it."""
    from xeda.cli import _cli_local_settings

    project = project_of(tmp_path)
    local = write(local_file_of(project), {"run_root": "local", "flows": {"x": {"a": 1}}})
    settings = _cli_local_settings(project, tmp_path / "d.yaml", None, False)
    assert settings.local == {} and settings.run_root == (local.parent / "local").resolve()
    assert settings.local_file == local
