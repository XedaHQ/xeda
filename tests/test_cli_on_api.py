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
from xeda.cli_types import FlowChoice
from xeda.frozen import thaw
from xeda.outcomes import result_from_dict
from xeda.run_root import ensure_run_root
from xeda.spec import Spec
from xeda.types import UNSET, Question
from xeda.workspace import Workspace

from . import io_flows  # noqa: F401 - registers the declared test flows
from .flow_registry import preserved_flow_registry

pytestmark = pytest.mark.python_compat

FLOW = "yosys_fpga"
#: A question of a launch that removes run directories, as a workspace asks it.
REMOVAL = Question(
    "Xeda would remove these 1 run directories:\n  x\nRemove them?",
    removal=True,
    undone="Not removing 1 run directory",
)
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
            "answers.removal": workspace.confirm is not None and workspace.confirm(REMOVAL),
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
        "--yes": (["--yes"], {"answers.removal": True}),
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
    "--yes",
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


def test_the_target_of_a_project_design_is_the_default_target_unless_one_is_given(calls, tmp_path):
    """`--target` and `--no-target` are the `target` of `Project.design`; neither is its default."""
    path = tmp_path / "p" / "xedaproject.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "designs": [
                    {
                        "name": "first",
                        "rtl": {"sources": [], "top": "first_top"},
                        "targets": {"fast": {"rtl": {"top": "fast_top"}}, "slow": {}},
                    }
                ],
                "default_target": "fast",
            }
        )
    )
    start = ["run", FLOW, "--xedaproject", path, "--design-name", "first"]
    assert view_of(calls, *start)["design.target"] == "fast"
    assert view_of(calls, *start, "--target", "slow")["design.target"] == "slow"
    assert view_of(calls, *start, "--no-target")["design.target"] is None


def test_two_items_that_set_one_setting_to_two_values_never_reach_the_workspace(calls, world):
    """A spec holds a table, which keeps one value for a setting, so the command refuses the
    items. An item that repeats another is that item."""
    items = ["-s", "yosys_fpga.flatten=true"]
    answer = invoke("run", FLOW, world["design"], *items, "yosys_fpga.flatten=false", "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and calls == [] and document["outcome"] is None
    assert document["error"]["code"] == "conflicting_keys"
    assert "-s yosys_fpga.flatten=true" in document["error"]["message"]
    assert view_of(calls, "run", FLOW, world["design"], *items, "yosys_fpga.flatten=true")


def test_the_flow_and_the_tool_output_are_those_of_the_command(calls, world):
    given = view_of(calls, "run", "yosys_fpga+nextpnr", world["design"])
    assert given["flow"] == "yosys_fpga+nextpnr" and given["tool_output"] == "terminal"
    assert given["call"] == "run" and given["answers.removal"] is False


def test_a_question_keeps_its_meaning_through_a_copy_and_a_pickle():
    import copy
    import pickle

    for again in (copy.deepcopy(REMOVAL), pickle.loads(pickle.dumps(REMOVAL))):
        assert again == REMOVAL and (again.removal, again.undone) == (True, REMOVAL.undone)


def test_a_question_is_asked_on_a_terminal_and_not_with_json(calls, world, monkeypatch):
    monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: not json_flag)
    asked = []
    monkeypatch.setattr(cli_module, "_prompt_question", lambda question: asked.append(1) or True)
    assert view_of(calls, "run", FLOW, world["design"])["answers.removal"] is True
    assert asked == [1]
    assert view_of(calls, "run", FLOW, world["design"], "--json")["answers.removal"] is False
    assert asked == [1], "with --json nothing is asked"


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
            "roots": [FLOW],
            "nodes": [
                {
                    "name": FLOW,
                    "outcome": outcome,
                    "disposition": "ran" if outcome == "passed" else "not_run",
                    "reason": "",
                    "blocked_by": None,
                    "elapsed": 0.0,
                    "record": None,
                    "error": error and {"exit_status": None, "log": None, **error},
                    "deliveries": [],
                    "run_dir": "/x/demo/yosys_fpga",
                    "flow": FLOW,
                }
            ],
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


def test_a_bug_in_a_launch_keeps_its_traceback_without_json(monkeypatch, world):
    def broken(self, what, /, **options):
        raise errors.XedaError("a bug", code="internal_error") from RuntimeError("the cause")

    monkeypatch.setattr(Workspace, "run", broken)
    answer = invoke("run", FLOW, world["design"])
    assert answer.exit_code == 1 and isinstance(answer.exception, errors.XedaError)
    assert isinstance(answer.exception.__cause__, RuntimeError)


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


@pytest.mark.parametrize(
    "item, reason",
    [
        ("yosys_fpga.nope=1", "the flow has no such setting"),
        ("yosys_fpga.flatten=maybe", "`maybe` is text, not a boolean: write `true` or `false`"),
    ],
)
def test_a_settings_error_names_the_node_and_the_setting_as_the_user_writes_them(
    world, caplog, item, reason
):
    """The log line and the `--json` message are one text: `<node>.<key>: <reason>`."""
    expected = item.split("=")[0] + ": " + reason
    answer = invoke("run", FLOW, world["design"], "-s", item, "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and document["outcome"] is None
    assert document["error"]["message"] == expected
    assert f"SettingsError: {expected}" in [record.getMessage() for record in caplog.records]


def _design(path: Path):
    from xeda import Design

    return Design.load(path)


# --------------------------------------------------------------------------- the questions


def variants_of(run_root: Path) -> list[Path]:
    return sorted(p for p in (run_root / "demo").glob("t_maker_*") if p.is_dir())


def variant(maker, text: str, *arguments) -> tuple[Any, list[Path]]:
    """Run `t_maker` with its own `text` (so, in a run directory of its own) and return the answer
    and the run directories of the flow that exist then."""
    design, run_root = maker
    answer = invoke(
        "run", "t_maker", design, "--run-root", run_root, "--hashed-run-dirs", "--json",
        "-s", f"t_maker.text={text}", *arguments,
    )  # fmt: skip
    return answer, variants_of(run_root)


NO_TERMINAL = (
    "Not removing {what}: no terminal to ask on. Run the command in a terminal, pass --yes, or "
    "call Workspace(confirm=...) from Python to answer the question."
)


def test_without_a_terminal_and_without_yes_a_prune_removes_nothing_and_says_why(maker, caplog):
    variant(maker, "one")
    answer, directories = variant(maker, "two", "--prune-variants")
    assert answer.exit_code == 0 and len(directories) == 2
    assert NO_TERMINAL.format(what="1 run directory") in [r.getMessage() for r in caplog.records]
    assert "nothing confirmed the removal" not in caplog.text


def test_yes_prunes_without_a_terminal_and_the_log_says_how_many(maker, caplog):
    variant(maker, "one")
    variant(maker, "two")
    answer, directories = variant(maker, "three", "--prune-variants", "--yes")
    assert answer.exit_code == 0 and len(directories) == 1
    assert "2 run directories removed." in [r.getMessage() for r in caplog.records]
    assert "no terminal" not in caplog.text


def test_the_log_of_a_prune_of_one_run_directory_uses_the_singular(maker, caplog):
    variant(maker, "one")
    variant(maker, "two", "--prune-variants", "--yes")
    assert "1 run directory removed." in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("reply, kept", [("y\n", 1), ("n\n", 2), ("\n", 2)])
def test_on_a_terminal_a_prune_shows_the_directories_and_asks(maker, monkeypatch, reply, kept):
    monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: True)
    variant(maker, "one")
    design, run_root = maker
    answer = invoke(
        "run", "t_maker", design, "--run-root", run_root, "--hashed-run-dirs", "--prune-variants",
        "-s", "t_maker.text=two", input=reply,
    )  # fmt: skip
    text = click.unstyle(answer.output)
    assert answer.exit_code == 0 and "Xeda would remove these 1 run directories:" in text
    assert "Remove them? [y/N]:" in text
    assert len(variants_of(run_root)) == kept


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


def test_scrub_asks_on_a_terminal_only(scrubs, tmp_path, monkeypatch, caplog):
    """As the prune of `xeda run` does: on a terminal the user answers, else the answer is no and
    the log says why, in the words of the prune."""
    monkeypatch.setattr(cli_module, "_prompt_removal", lambda question: True)
    monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: not json_flag)
    invoke("scrub", FLOW, "demo", "--run-root", tmp_path / "root")
    assert scrubs[0][0].confirm(REMOVAL) is True
    for arguments in (["--json"], []):
        monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: False)
        invoke("scrub", FLOW, "demo", "--run-root", tmp_path / "root", *arguments)
        assert scrubs[-1][0].confirm(REMOVAL) is False
    assert NO_TERMINAL.format(what="1 run directory") in [r.getMessage() for r in caplog.records]


def test_scrub_with_yes_answers_the_question_with_yes(scrubs, tmp_path):
    invoke("scrub", FLOW, "demo", "--yes", "--run-root", tmp_path / "root")
    assert scrubs[0][0].confirm(REMOVAL) is True


@pytest.mark.parametrize("spelling", ["open_xc7", "openxc7", "OpenXC7", "open-xc7"])
def test_workspace_scrub_takes_the_name_of_a_removed_flow_as_the_command_does(tmp_path, spelling):
    """The run directories of a removed flow still exist, and a scrub removes directories. Any
    spelling of its name is its canonical name, as `FlowChoice(removed=True)` makes it."""
    assert FlowChoice(removed=True).convert(spelling, None, None) == "open_xc7"
    root = ensure_run_root(tmp_path / "xeda_run")
    directory = root / "demo" / "open_xc7"
    directory.mkdir(parents=True)
    (directory / "results.json").write_text("{}\n")
    result = Workspace(root, confirm=lambda question: True).scrub(design_name="demo", flow=spelling)
    assert result.removed == [directory] and not directory.exists()


@pytest.mark.parametrize("spelling", ["ghdl-sim", "GhdlSim", "ghdl", "nosuch_flow"])
def test_workspace_scrub_refuses_a_flow_that_is_no_flow_as_the_command_does(tmp_path, spelling):
    with pytest.raises(errors.FlowNotFoundError):
        Workspace(tmp_path / "root").scrub(design_name="demo", flow=spelling)
    with pytest.raises(click.BadParameter):
        FlowChoice(removed=True).convert(spelling, None, None)


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
    answer = invoke("scrub", FLOW, "demo", "--run-root", root, "--json", "--yes")
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


@pytest.mark.parametrize(
    "arguments, given, message",
    [
        ([], (None, None, None, None), "Missing argument 'FLOW_NAME'"),
        ([FLOW], (FLOW, None, None, None), "Missing argument 'DESIGN_NAME'"),
        ([FLOW, "demo", "--bogus"], (FLOW, "demo", None, None), "No such option '--bogus'"),
        (["nosuch_flow", "demo"], ("nosuch_flow", "demo", None, None), "nosuch_flow"),
        (
            [FLOW, "demo", "--target", "fast", "--run-root", "{root}", "--incremental"],
            (FLOW, "demo", "fast", "{root}"),
            "`--incremental` was removed",
        ),
    ],
    ids=["no-arguments", "no-design", "unknown-option", "unknown-flow", "removed-option"],
)
def test_a_usage_error_of_scrub_prints_the_scrub_document(tmp_path, arguments, given, message):
    """The document has the keys of every scrub document. A failure before anything is scanned has
    no outcome, the fields that the command line gave, empty lists and the exit status 2."""
    root = tmp_path / "root"
    arguments = [argument.replace("{root}", str(root)) for argument in arguments]
    answer = invoke("scrub", *arguments, "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 2 and list(document) == SCRUB_KEYS
    assert (document["schema_version"], document["outcome"]) == (1, None)
    assert document["error"]["code"] in ("usage_error", "flow_not_found")
    assert message in document["error"]["message"]
    expected = tuple(item and item.replace("{root}", str(root)) for item in given)
    assert tuple(document[key] for key in ("flow", "design", "target", "run_root")) == expected
    assert [document[name] for name in ("scanned", "removed", "kept", "gone", "skipped")] == [
        []
    ] * 5


def test_a_cancel_while_the_request_is_planned_prints_the_document_of_no_launch(maker, monkeypatch):
    """The library ends a launch that is canceled while it plans as a `Result` with the outcome
    `canceled`. The command says what it says of any Ctrl-C before the launch starts: no outcome,
    no launch, the exit status 1."""
    from xeda.flow_runner.default_runner import FlowLauncher
    from xeda.utils import Canceled

    def canceled(self, *arguments, **keywords):
        raise Canceled("the launch was canceled while it was planned")

    monkeypatch.setattr(FlowLauncher, "_judged_request", canceled)
    design, run_root = maker
    answer = invoke("run", "t_maker", design, "--run-root", run_root, "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 1 and document["outcome"] is None
    assert document["error"]["code"] == "canceled" and document["nodes"] == []
    assert document["launch_id"] is None
    assert "before the launch started" in document["error"]["message"]


def test_a_question_is_asked_with_the_default_handler_of_ctrl_c(monkeypatch):
    """While the command asks, Ctrl-C reaches the prompt (it answers no), as it does without the
    handler that cancels a launch. The handler is back when the question is answered."""
    import signal

    from xeda.cancel import CancelToken

    seen = []

    def confirm(text, **keywords):
        seen.append(signal.getsignal(signal.SIGINT))
        return True

    monkeypatch.setattr(click, "confirm", confirm)
    token = CancelToken()
    with cli_module._cancel_on_interrupt(token):
        installed = signal.getsignal(signal.SIGINT)
        assert cli_module._prompt_question("Remove them?") is True
        assert signal.getsignal(signal.SIGINT) is installed
    assert seen == [signal.default_int_handler] and installed is not signal.default_int_handler


def test_a_second_interrupt_while_the_launch_runs_names_the_launch(world, monkeypatch):
    from xeda.log_context import tag_launch_id

    def interrupted(self, what, /, **options):
        interrupt = KeyboardInterrupt()
        tag_launch_id(interrupt, "abc123")
        raise interrupt

    monkeypatch.setattr(Workspace, "run", interrupted)
    answer = invoke("run", FLOW, world["design"], "--json")
    document = json.loads(answer.stdout)
    assert answer.exit_code == 1 and document["outcome"] is None
    assert document["launch_id"] == "abc123" and document["error"]["code"] == "canceled"
    assert "while the launch was running" in document["error"]["message"]
