"""`xeda run` is a client of the workspace API.

It builds a `Design` (`Design.load`, or `Project.design` for a design of a project), a `Spec`, a
`Workspace` and, with `--remote`, a `Remote`. Then it calls `Workspace.plan` for `--dry-run` and
`Workspace.run` for the rest, and prints the document of what it got back. Each option of the
command has the name of the API parameter that it sets.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

import xeda.cli as cli_module
from xeda import errors
from xeda.cli import cli, run
from xeda.frozen import thaw
from xeda.outcomes import result_from_dict
from xeda.run_root import ensure_run_root
from xeda.spec import Spec
from xeda.types import UNSET
from xeda.workspace import Workspace

from . import io_flows  # noqa: F401 - registers the declared test flows
from .flow_registry import preserved_flow_registry

pytestmark = pytest.mark.python_compat

FLOW = "yosys_fpga"
LAUNCH_OPTIONS = (
    "rebuild_all",
    "clean",
    "prune_variants",
    "outputs_to",
    "overwrite_outputs",
    "post_cleanup",
    "post_cleanup_purge",
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    with preserved_flow_registry():
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("XEDA_RUN_ROOT", raising=False)
        monkeypatch.delenv("XEDA_REMOTE", raising=False)
        yield


def write_design(directory: Path, name: str = "design.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "rtl": {"sources": [], "top": "top"},
                "targets": {"fast": {"rtl": {"top": "fast_top"}}},
            },
            sort_keys=False,
        )
    )
    return path


def write_project(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "xedaproject.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "designs": [
                    {"name": "first", "rtl": {"sources": [], "top": "first_top"}},
                    {"name": "second", "rtl": {"sources": [], "top": "second_top"}},
                ],
            },
            sort_keys=False,
        )
    )
    return path


def invoke(*args, **kwargs):
    return CliRunner().invoke(cli, [str(arg) for arg in args], **kwargs)


# ------------------------------------------------- what the command builds and asks of the API


@dataclass
class Call:
    """One call of the command to a workspace: the workspace it built, the spec and the options
    that it passed."""

    kind: str
    workspace: Workspace
    spec: Spec
    options: dict[str, Any]

    def view(self) -> dict[str, Any]:
        """The API parameters that the command set, by name."""
        workspace, spec, remote = self.workspace, self.spec, self.workspace.remote
        return {
            "call": self.kind,
            "flow": spec.flow,
            "design.name": spec.design.name,
            "design.target": spec.design.target,
            "design.rtl.top": spec.design.rtl.top,
            "settings": thaw(spec.settings),
            "run_root": workspace.run_root,
            "hashed_run_dirs": workspace.hashed_run_dirs,
            "tool_output": workspace.tool_output,
            "confirm": workspace.confirm is not None,
            "local_settings.user_file": workspace.local_settings.user_file,
            "remote.host": remote and remote.host,
            "remote.profile.root": remote and remote.profile and remote.profile["root"],
            "remote.fetch_all": remote and remote.fetch_all,
            "remote.keep_running": remote and remote.keep_running,
            **{f"options.{name}": self.options[name] for name in LAUNCH_OPTIONS},
        }


class Stop(Exception):
    """Raised by the recording workspace, so that nothing plans or runs."""


@pytest.fixture
def calls(monkeypatch) -> list[Call]:
    """The calls to a workspace of the command: the command builds a recording `Workspace`."""
    seen: list[Call] = []

    class Recording(Workspace):
        def plan(self, spec, /, **options):
            seen.append(Call("plan", self, spec, options))
            raise Stop

        def run(self, what, /, **options):
            seen.append(Call("run", self, what, options))
            raise Stop

    monkeypatch.setattr(cli_module, "Workspace", Recording)
    return seen


def view_of(calls: list[Call], *args) -> dict[str, Any]:
    calls.clear()
    invoke(*args)
    assert len(calls) == 1, f"the command made {len(calls)} calls to the workspace: {args}"
    return calls[0].view()


# --------------------------------------------------------- every option maps to a parameter


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A design, a project, a user file and a host profile, all below `tmp_path`."""
    config = tmp_path / "cfg" / "xeda"
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    (config / "config.yaml").write_text("{}\n")
    (config / "remotes.yaml").write_text(yaml.safe_dump({"hosts": {"venona": {"root": "~/xeda"}}}))
    other = tmp_path / "other.yaml"
    other.write_text("{}\n")
    return {
        "design": write_design(tmp_path / "d"),
        "project": write_project(tmp_path / "p"),
        "other_config": other,
        "out": tmp_path / "out",
        "root": tmp_path / "root",
    }


#: For each option of `xeda run` that sets a parameter: its arguments, and the parameters that
#: then differ from a run without it (a parameter is named as `Call.view` names it). A `True` after
#: them says that the option needs `--remote`.
def rows(world: dict[str, Path]) -> dict[str, tuple]:
    out, root = world["out"], world["root"]
    return {
        "--run-root": (["--run-root", root], {"run_root": root.resolve()}),
        "--hashed-run-dirs": (["--hashed-run-dirs"], {"hashed_run_dirs": True}),
        "--rebuild-all": (["--rebuild-all"], {"options.rebuild_all": True}),
        "--clean": (["--clean"], {"options.clean": True}),
        "--prune-variants": (["--prune-variants"], {"options.prune_variants": True}),
        "--post-cleanup": (["--post-cleanup"], {"options.post_cleanup": True}),
        "--post-cleanup-purge": (
            ["--post-cleanup-purge"],
            {"options.post_cleanup_purge": True},
        ),
        "--outputs-to": (["--outputs-to", out], {"options.outputs_to": out.resolve()}),
        "--overwrite-outputs": (["--overwrite-outputs"], {"options.overwrite_outputs": True}),
        "--target": (
            ["--target", "fast"],
            {"design.target": "fast", "design.rtl.top": "fast_top"},
        ),
        "--design-updates": (
            ["--design-updates", "rtl.top=changed"],
            {"design.rtl.top": "changed"},
        ),
        "--settings": (
            ["-s", "yosys_fpga.flatten=true"],
            {"settings": {"yosys_fpga": {"flatten": "true"}}},
        ),
        "--config": (
            ["--config", world["other_config"]],
            {"local_settings.user_file": world["other_config"]},
        ),
        "--no-config": (["--no-config"], {"local_settings.user_file": None}),
        "--remote": (
            ["--remote", "venona"],
            {
                "remote.host": "venona",
                "remote.profile.root": "~/xeda",
                "remote.fetch_all": False,
                "remote.keep_running": False,
            },
        ),
        "--fetch-all": (["--fetch-all"], {"remote.fetch_all": True}, True),
        "--keep-running": (["--keep-running"], {"remote.keep_running": True}, True),
        "--dry-run": (["--dry-run"], {"call": "plan"}),
    }


OPTIONS = [
    "--run-root",
    "--hashed-run-dirs",
    "--rebuild-all",
    "--clean",
    "--prune-variants",
    "--post-cleanup",
    "--post-cleanup-purge",
    "--outputs-to",
    "--overwrite-outputs",
    "--target",
    "--design-updates",
    "--settings",
    "--config",
    "--no-config",
    "--remote",
    "--fetch-all",
    "--keep-running",
    "--dry-run",
]

#: The options that no parameter of the API takes, and what they do instead. The selection of a
#: design from a project is checked in `test_a_design_of_a_project_is_selected_by_name`.
NOT_PARAMETERS = {
    "--json": "the command prints the document of the plan or of the result",
    "--log-level": "the command sets up logging",
    "--detailed-logs": "the command sets up logging",
    "--xedaproject": "the project of `--design-name`",
    "--design-name": "the design that the project selects",
    "--no-target": "no target, whatever `default_target` the project has",
}


def test_every_option_of_run_is_in_the_table_or_has_no_parameter(world):
    given = {
        option.opts[0]
        for option in run.params
        if isinstance(option, click.Option) and not option.hidden
    }
    assert given == set(OPTIONS) | set(NOT_PARAMETERS)
    assert set(OPTIONS) == set(rows(world))


@pytest.mark.parametrize("option", OPTIONS)
def test_an_option_sets_the_parameter_of_its_name(calls, world, option):
    args, changed, *needs_remote = rows(world)[option]
    start = ["run", FLOW, world["design"], *(["--remote", "venona"] if needs_remote else [])]
    baseline = view_of(calls, *start)
    given = view_of(calls, *start, *args)
    assert {key: given[key] for key in given if given[key] != baseline[key]} == changed


def test_the_launch_options_are_passed_by_name(calls, world):
    """Every launch option is a keyword of the call: the command never leaves one to a default."""
    view_of(calls, "run", FLOW, world["design"])
    assert set(calls[0].options) - {"cancel"} == set(LAUNCH_OPTIONS)


def test_a_design_of_a_project_is_selected_by_name(calls, world):
    project = world["project"]
    start = ["run", FLOW, "--xedaproject", project]
    by_name = view_of(calls, *start, "--design-name", "second")
    assert by_name["design.name"] == "second" and by_name["design.rtl.top"] == "second_top"
    assert view_of(calls, *start, "--design-name", "first")["design.name"] == "first"
    assert view_of(calls, "run", FLOW, world["design"])["design.name"] == "demo"


def test_the_flow_and_the_tool_output_are_those_of_the_command(calls, world):
    given = view_of(calls, "run", "yosys_fpga+nextpnr", world["design"])
    assert given["flow"] == "yosys_fpga+nextpnr" and given["tool_output"] == "terminal"
    assert given["call"] == "run" and given["confirm"] is False


def test_a_terminal_gets_the_prompt_as_the_confirmation(calls, world, monkeypatch):
    monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: not json_flag)
    assert view_of(calls, "run", FLOW, world["design"])["confirm"] is True
    assert view_of(calls, "run", FLOW, world["design"], "--json")["confirm"] is False


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--fetch-all"], "`--fetch-all` needs --remote"),
        (["--keep-running"], "`--keep-running` needs --remote"),
        (["--remote", "h", "--keep-running", "--dry-run"], "has no launch to keep running"),
        (["--config", "x.yaml", "--no-config"], "exclude each other"),
        (["--target", "fast", "--no-target"], "exclude each other"),
    ],
)
def test_a_combination_that_no_parameter_can_say_is_refused_by_the_command(
    calls, world, arguments, message
):
    result = invoke("run", FLOW, world["design"], *arguments)
    assert result.exit_code == 2 and calls == []
    assert message in click.unstyle(result.output)


# ------------------------------------------------------------------------- the exit statuses


def canned(outcome: str, code: str = "reported_failure"):
    """A `Result` of the given outcome, as a workspace would return it."""
    error = None if outcome == "passed" else dict(code=code, message="m", node=None)
    return result_from_dict(
        {
            "schema_version": 1,
            "outcome": outcome,
            "error": error and {"exit_status": None, "log": None, **error},
            "launch_id": "abc",
            "roots": [],
            "nodes": [],
            "spec": {
                "flow": FLOW,
                "design": "demo",
                "design_file": None,
                "target": None,
                "settings": {},
            },
        }
    )


@pytest.mark.parametrize(
    "outcome, status",
    [
        ("passed", 0),
        ("failed", 1),
        ("rejected", 1),
        ("canceled", 1),
        ("detached", 1),
    ],
)
@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_the_exit_status_follows_the_outcome(monkeypatch, world, outcome, status, json_flag):
    result = canned(outcome)
    monkeypatch.setattr(Workspace, "run", lambda self, what, /, **options: result)
    answer = invoke("run", FLOW, world["design"], *(["--json"] if json_flag else []))
    assert answer.exit_code == status, answer.output
    if json_flag:
        assert json.loads(answer.stdout) == json.loads(json.dumps(result.to_dict()))


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_an_invalid_spec_exits_with_2_and_has_no_outcome(monkeypatch, world, json_flag):
    def refuse(self, what, /, **options):
        raise errors.SpecError("no such setting", code="unknown_setting")

    monkeypatch.setattr(Workspace, "run", refuse)
    answer = invoke("run", FLOW, world["design"], *(["--json"] if json_flag else []))
    assert answer.exit_code == 2
    if json_flag:
        document = json.loads(answer.stdout)
        assert document["outcome"] is None and document["launch_id"] is None
        assert document["error"]["code"] == "unknown_setting"
        assert document["spec"]["flow"] == FLOW and document["spec"]["design"] == "demo"
        assert (document["roots"], document["nodes"]) == ([], [])


def test_a_remote_host_that_cannot_be_reached_exits_with_2_and_has_no_outcome(monkeypatch, world):
    def unreachable(self, what, /, **options):
        raise errors.RemoteError("no route", code="remote_unreachable", host="venona")

    monkeypatch.setattr(Workspace, "run", unreachable)
    answer = invoke("run", FLOW, world["design"], "--remote", "venona", "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and document["outcome"] is None
    assert document["error"]["code"] == "remote_unreachable" and document["nodes"] == []


def test_a_bug_in_a_launch_is_a_failed_launch_with_exit_status_1(monkeypatch, world):
    def broken(self, what, /, **options):
        raise RuntimeError("a bug")

    monkeypatch.setattr(Workspace, "run", broken)
    answer = invoke("run", FLOW, world["design"], "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 1 and document["outcome"] == "failed"
    assert document["error"]["code"] == "internal_error" and document["error"]["message"] == "a bug"
    assert document["spec"]["design"] == "demo"


@pytest.mark.parametrize(
    "arguments",
    [["--no-such-option"], [FLOW, "--run-root"], [FLOW, "--target"]],
    ids=["option", "missing-value", "missing-target"],
)
def test_a_command_line_that_click_cannot_parse_exits_with_2(world, arguments):
    answer = invoke("run", *arguments, "--json")
    assert answer.exit_code == 2
    document = json.loads(answer.stdout)
    assert document["outcome"] is None and document["error"]["code"] == "usage_error"


def test_an_unknown_flow_exits_with_2(world):
    answer = invoke("run", "no_such_flow", world["design"], "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and document["error"]["code"] == "flow_not_found"


def test_a_design_file_that_is_missing_exits_with_2(world):
    answer = invoke("run", FLOW, world["design"].parent / "missing.yaml", "--json")
    assert answer.exit_code == 2 and json.loads(answer.stdout)["outcome"] is None


# ------------------------------------------------------------------------------ the old option


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_scrub_is_a_removed_option_that_names_prune_variants(world, json_flag):
    answer = invoke("run", FLOW, world["design"], "--scrub", *(["--json"] if json_flag else []))
    message = "`--scrub` was removed: use --prune-variants"
    assert answer.exit_code == 2
    if json_flag:
        document = json.loads(answer.stdout)
        assert document["outcome"] is None and document["error"]["message"] == message
    else:
        assert message in click.unstyle(answer.output)


def test_the_removed_option_is_not_suggested_for_a_typo(world):
    answer = invoke("run", FLOW, world["design"], "--scrb")
    text = click.unstyle(answer.output)
    assert answer.exit_code == 2 and "No such option '--scrb'" in text
    assert "--scrub" not in text.replace("'--scrb'", "")


# ---------------------------------------------------------------------------- the real workspace


@pytest.fixture
def maker(tmp_path):
    """A design that the declared test flow `t_maker` runs on, and its run root."""
    path = write_design(tmp_path / "toy")
    return path, tmp_path / "xeda_run"


def test_the_dry_run_prints_the_plan_document(maker):
    design, run_root = maker
    answer = invoke("run", "t_maker", design, "--run-root", run_root, "--dry-run", "--json")
    assert answer.exit_code == 0, answer.output
    expected = Workspace(run_root).plan(Spec("t_maker", _design(design))).to_dict()
    assert json.loads(answer.stdout) == json.loads(json.dumps(expected))
    assert not run_root.exists(), "a dry run writes nothing"


def test_the_dry_run_text_names_the_plan(maker):
    design, run_root = maker
    answer = invoke("run", "t_maker", design, "--run-root", run_root, "--dry-run")
    text = click.unstyle(answer.output)
    assert answer.exit_code == 0 and "Plan for t_maker (a dry run: nothing runs)" in text


def test_a_run_prints_the_result_document(maker):
    design, run_root = maker
    answer = invoke("run", "t_maker", design, "--run-root", run_root, "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 0, answer.output
    assert document["outcome"] == "passed" and document["error"] is None
    assert [n["name"] for n in document["nodes"]] == ["t_maker"]
    assert document["nodes"][0]["record"]["outcome"] == "passed"


def test_an_invalid_setting_of_a_real_run_exits_with_2(maker):
    design, run_root = maker
    answer = invoke(
        "run", "t_maker", design, "--run-root", run_root, "-s", "t_maker.nope=1", "--json"
    )
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and document["outcome"] is None
    assert document["error"]["code"] == "unknown_setting"
    assert not run_root.exists(), "a refused request writes nothing"


def _design(path: Path):
    from xeda import Design

    return Design.load(path)


# -------------------------------------------------------------------------------------- Ctrl-C


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
def test_an_interrupt_before_the_launch_starts_prints_no_outcome(world, monkeypatch, dry_run):
    def interrupted(self, what, /, **options):
        raise KeyboardInterrupt

    monkeypatch.setattr(Workspace, "plan" if dry_run else "run", interrupted)
    answer = invoke("run", FLOW, world["design"], "--json", *(["--dry-run"] if dry_run else []))
    document = json.loads(answer.stdout)
    assert answer.exit_code == 1 and document["outcome"] is None
    assert document["error"]["code"] == "canceled" and document["nodes"] == []
    assert document["spec"]["flow"] == FLOW


# ------------------------------------------------------------------------------- xeda scrub


@pytest.fixture
def scrubs(monkeypatch) -> list[tuple[Workspace, dict[str, Any]]]:
    """The workspaces that `xeda scrub` builds, with the arguments of their `scrub` call."""
    seen: list[tuple[Workspace, dict[str, Any]]] = []

    class Recording(Workspace):
        def scrub(self, **arguments):
            seen.append((self, arguments))
            raise Stop

    monkeypatch.setattr(cli_module, "Workspace", Recording)
    return seen


@pytest.mark.parametrize(
    "arguments, target",
    [([], UNSET), (["--target", "fast"], "fast")],
    ids=["every-target", "one-target"],
)
def test_scrub_maps_its_arguments_to_the_workspace(scrubs, tmp_path, arguments, target):
    invoke("scrub", FLOW, "demo", "--run-root", tmp_path / "root", *arguments)
    ((workspace, called),) = scrubs
    assert called == {"design_name": "demo", "flow": FLOW, "target": target}
    assert workspace.run_root == (tmp_path / "root").resolve()
    assert workspace.confirm is cli_module._prompt_removal


def test_scrub_asks_even_when_nobody_can_see_the_prompt(scrubs, tmp_path):
    """The command asks with `--json` too, so that `echo yes | xeda scrub` works."""
    invoke("scrub", FLOW, "demo", "--json", "--run-root", tmp_path / "root")
    assert scrubs[0][0].confirm is cli_module._prompt_removal


SCRUB_KEYS = [
    "schema_version",
    "outcome",
    "error",
    "flow",
    "design",
    "target",
    "run_root",
    "scanned",
    "removed",
    "kept",
    "gone",
    "skipped",
]


def test_the_scrub_document_has_the_keys_of_the_contract(tmp_path):
    root = ensure_run_root(tmp_path / "xeda_run")
    variant = root / "demo" / f"{FLOW}_0123456789abcdef"
    variant.mkdir(parents=True)
    (variant / "results.json").write_text("{}\n")
    answer = invoke("scrub", FLOW, "demo", "--run-root", root, "--json", input="yes\n")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 0 and list(document) == SCRUB_KEYS
    assert (document["schema_version"], document["outcome"], document["error"]) == (
        1,
        "passed",
        None,
    )
    assert (document["flow"], document["design"], document["target"]) == (FLOW, "demo", None)
    assert document["run_root"] == str(root) and document["removed"] == [str(variant)]
    assert not variant.exists()


def test_a_scrub_that_cannot_run_prints_a_failed_document_with_exit_status_1(tmp_path):
    root = ensure_run_root(tmp_path / "xeda_run")
    answer = invoke("scrub", FLOW, "a/b", "--run-root", root, "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 1 and list(document) == SCRUB_KEYS
    assert document["outcome"] == "failed" and document["error"]["code"] == "invalid_run_root"
    assert "'a/b' is not a design name" in document["error"]["message"]
    assert [document[name] for name in ("scanned", "removed", "kept", "gone", "skipped")] == [
        []
    ] * 5
