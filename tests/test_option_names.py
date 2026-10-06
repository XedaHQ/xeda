"""The names of the run-location and rebuild options, and the messages for the removed ones.

An option takes a value only when the value is data; a behavior switch is a flag. The run root is
the directory holding every run, a run directory one flow's.
"""

import inspect
import itertools
import json
import os
import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from xeda import cli as cli_module
from xeda.cli import cli, dse, run, scrub
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dse.dse_runner import Dse, Optimizer
from xeda.flow_runner.remote import RemoteRunner

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"

#: what each command's arguments are, apart from the options under test
COMMANDS = {
    "run": ["run", "vivado_synth", str(SQRT)],
    "dse": ["dse", "vivado_synth", "--design", str(SQRT)],
    "scrub": ["scrub", "vivado_synth", "sqrt"],
}


@pytest.fixture
def start(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _invoke(args, env=None):
    result = CliRunner().invoke(cli, [*args, "--json"], env=env, catch_exceptions=False)
    return result, json.loads(result.stdout)


# ------------------------------------------------------------------------------ removed names


@pytest.mark.parametrize("command", COMMANDS)
def test_the_removed_root_option_names_its_replacement(start, command):
    result, document = _invoke([*COMMANDS[command], "--xeda-run-dir", str(start / "r")])
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"] == {
        "type": "UsageError",
        "message": "`--xeda-run-dir` was removed: use --run-root",
    }
    assert not (start / "r").exists()


@pytest.mark.parametrize("command", COMMANDS)
def test_the_removed_root_variable_names_its_replacement(start, command):
    result, document = _invoke(COMMANDS[command], env={"XEDA_RUN_DIR": str(start / "r")})
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"] == {
        "type": "UsageError",
        "message": "`XEDA_RUN_DIR` was removed: use XEDA_RUN_ROOT",
    }
    assert not (start / "r").exists()


class _NoBatch(Optimizer):
    def next_batch(self):
        return None


@pytest.mark.parametrize(
    "launcher",
    [
        DefaultRunner,
        RemoteRunner,
        lambda **kwargs: Dse(_NoBatch, {}, variations={}, **kwargs),
    ],
    ids=["DefaultRunner", "RemoteRunner", "Dse"],
)
def test_the_removed_root_keyword_names_its_replacement(tmp_path, launcher):
    with pytest.raises(ValueError, match=re.escape("`xeda_run_dir` was removed: use run_root")):
        launcher(xeda_run_dir=tmp_path / "r")
    assert not (tmp_path / "r").exists()


def test_the_removed_root_property_names_its_replacement(tmp_path):
    launcher = DefaultRunner(tmp_path / "r")
    assert launcher.run_root == (tmp_path / "r").resolve()
    with pytest.raises(AttributeError, match=re.escape("`xeda_run_dir` was removed: use run_root")):
        launcher.xeda_run_dir  # noqa: B018


@pytest.mark.parametrize(
    "given, suggested",
    [
        (["--rebuild", "all"], "'--rebuild-all'"),
        (["--run-dirs", "hashed"], "'--hashed-run-dirs'"),
    ],
)
def test_names_that_never_shipped_get_click_s_suggestion(start, given, suggested):
    """`--rebuild` and `--run-dirs` never shipped: no removal message, click's own answer."""
    result, document = _invoke([*COMMANDS["run"], *given])
    assert result.exit_code != 0 and document["error"]["type"] == "NoSuchOption"
    assert document["error"]["message"].startswith(f"No such option '{given[0]}'. (Did you mean")
    assert suggested in document["error"]["message"]


@pytest.mark.parametrize("given", [dict(rebuild="all"), dict(run_dirs="hashed")], ids=str)
def test_launcher_settings_that_never_shipped_are_unknown(tmp_path, given):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DefaultRunner(tmp_path / "r", **given)


# ------------------------------------------------------------------------------ DSE's layout and root


def test_an_exploration_always_uses_hashed_run_directories(tmp_path):
    """Declared, not overwritten: asking for anything else is refused rather than ignored."""
    assert Dse(_NoBatch, {}, tmp_path / "r", variations={}).settings.hashed_run_dirs is True
    with pytest.raises(ValidationError, match="hashed_run_dirs"):
        Dse(_NoBatch, {}, tmp_path / "r", variations={}, hashed_run_dirs=False)


def test_an_exploration_has_one_default_run_root(start):
    """`xeda dse` and `Dse` alike: `./xeda_run`, shared with `xeda run --hashed-run-dirs`."""
    assert Dse(_NoBatch, {}, variations={}).run_root == (start / "xeda_run").resolve()
    ctx = dse.make_context("dse", ["vivado_synth"])
    assert ctx.params["run_root"] == (start / "xeda_run").resolve()


# ------------------------------------------------------------------------------ the oracle

#: `xeda run` options configuring the launcher under another name than a setting of theirs
LAUNCHER_NAME_EXCEPTIONS = {
    "--scrub": {"scrub_old_runs"},
    "--cwd": {"run_path", "dump_settings_json"},
}
#: options with which `xeda run` launches nothing locally
NOT_A_LOCAL_LAUNCH = {"--remote", "--help-settings", "--dry-run"}
#: the environment variables each command reads; `XEDA_RUN_DIR` only to refuse it
DECLARED_ENVVARS = {
    "run": {
        "XEDA_RUN_ROOT",
        "XEDA_RUN_DIR",
        "XEDA_DEBUG",
        "XEDA_REMOTE",
        "XEDA_LOG_LEVEL",
        "XEDA_DETAILED_LOGS",
    },
    "dse": {
        "XEDA_RUN_ROOT",
        "XEDA_RUN_DIR",
        "XEDA_DEBUG",
        "XEDA_LOG_LEVEL",
        "XEDA_DETAILED_LOGS",
        "XEDA_XEDAPROJECT",
        "XEDA_OPTIMIZER",
        "XEDA_DSE_SETTINGS",
        "XEDA_OPTIMIZER_SETTINGS",
        "XEDA_MAX_WORKERS",
    },
    "scrub": {"XEDA_RUN_ROOT", "XEDA_RUN_DIR"},
}


def _options(command: click.Command, hidden: bool = False):
    return [p for p in command.params if isinstance(p, click.Option) and (hidden or not p.hidden)]


def _envvars(param: click.Parameter) -> set:
    envvar = param.envvar
    return {envvar} if isinstance(envvar, str) else set(envvar or ())


def _non_default(option: click.Option, tmp_path: Path) -> list:
    """The option as given on the command line, with a value other than its default."""
    if option.is_flag:
        return [option.secondary_opts[0] if option.default is True else option.opts[0]]
    if isinstance(option.type, click.Path):
        if option.type.exists:
            return [option.opts[0], str(SQRT)]
        return [option.opts[0], str(tmp_path / option.name)]
    if option.type is click.INT:
        return [option.opts[0], "30"]
    if option.type is click.STRING:
        return [option.opts[0], "x"]
    return [option.opts[0], "a=1"]  # KEY=VALUE lists


def test_the_launcher_options_are_named_as_its_settings(start, monkeypatch):
    """(1) Every `xeda run` option that configures the launcher has the name of the setting it
    sets (or of the launcher's own parameter, the run root), or is in an explicit table; every
    other option leaves the launcher's configuration alone. (2) None of them is a choice."""
    seen: list = []

    class Recording(DefaultRunner):
        def run(self, *args, **kwargs):
            seen.append({"run_root": self.run_root, **self.settings.model_dump()})
            return None

    monkeypatch.setattr(cli_module, "DefaultRunner", Recording)
    fields = set(DefaultRunner.Settings.model_fields) | {
        name
        for name in inspect.signature(DefaultRunner.__init__).parameters
        if name not in ("self", "kwargs")
    }
    assert "run_root" in fields

    def configuration(*args) -> dict | None:
        seen.clear()
        CliRunner().invoke(cli, [*COMMANDS["run"], *args])
        return seen[0] if seen else None

    baseline = configuration()
    assert baseline is not None
    for option in _options(run):
        assert not isinstance(option.type, click.Choice), option.opts
        name = option.opts[0]
        if name in NOT_A_LOCAL_LAUNCH:
            continue
        configured = configuration(*_non_default(option, start))
        assert configured is not None, name
        changed = {key for key in baseline if configured[key] != baseline[key]}
        own = option.name if option.name in fields else None
        expected = LAUNCHER_NAME_EXCEPTIONS.get(name, {own} if own else set())
        assert changed == expected, name
        if own:
            assert name == "--" + own.replace("_", "-")


@pytest.mark.parametrize("command", [run, dse, scrub], ids=lambda c: c.name)
def test_each_command_reads_only_the_environment_variables_it_declares(command):
    """(3) No automatic `XEDA_<OPTION>` variables: `XEDA_CLEAN=1` would empty every run directory
    on every run, and nothing on the command line could turn it off."""
    read = set().union(*(_envvars(p) for p in _options(command, hidden=True)))
    assert read == DECLARED_ENVVARS[command.name]
    parent = click.Context(cli, info_name="xeda", auto_envvar_prefix="XEDA")
    args = COMMANDS[command.name][1:]
    ctx = command.make_context(command.name, list(args), parent=parent, resilient_parsing=True)
    assert ctx.auto_envvar_prefix is None


@pytest.mark.parametrize("command", [run, dse, scrub], ids=lambda c: c.name)
def test_no_two_names_differ_only_by_a_trailing_s(command):
    """(4) `--run-dirs` beside `--xeda-run-dir`, `XEDA_RUN_DIRS` beside `XEDA_RUN_DIR`."""
    options = _options(command, hidden=True)
    names = {n for p in options for n in [*p.opts, *p.secondary_opts]}
    names |= set().union(*(_envvars(p) for p in options))
    for a, b in itertools.permutations(names, 2):
        assert a + "s" != b and a + "S" != b, (a, b)
