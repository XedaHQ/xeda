"""A launcher's debug mode is the launch's: it never changes the logging of the process.

An application that embeds xeda owns its logging. A `DefaultRunner(debug=True)` used to set the
root logger to DEBUG for good, so every library of the process logged at DEBUG from then on, and
a later launcher without `debug` did not put it back.
"""

import logging
from pathlib import Path
from collections.abc import Iterator

import pytest

from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth

from .test_vivado_step_tables import PART, _design
from .tool_utils import use_fake_tools

SETTINGS = {"fpga": PART, "clock": {"period": 5.5}}
NAMES = ("", "xeda", "xeda.flow_runner", "xeda.flows.vivado", "not.xeda")


def levels() -> dict:
    return {name: logging.getLogger(name).level for name in NAMES}


@pytest.fixture(autouse=True)
def quiet_process() -> Iterator[None]:
    """An application that logs warnings and up, and puts its logging (levels and the root
    logger's handlers) back after the test."""
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in NAMES}
    handlers = list(root.handlers)
    root.setLevel(logging.WARNING)
    for name in NAMES[1:]:
        logging.getLogger(name).setLevel(logging.NOTSET)
    yield
    # the command line installs a handler on the root logger (`setup_logger`)
    root.handlers[:] = handlers
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_making_a_debug_launcher_changes_no_logger(tmp_path) -> None:
    before = levels()
    DefaultRunner(tmp_path / "run", debug=True)
    assert levels() == before


def _launch_recording(tmp_path, monkeypatch, debug: bool, fail: bool = False) -> dict:
    """Launch `vivado_synth`; what the loggers say while its `run` goes on."""
    use_fake_tools(monkeypatch)
    seen: dict = {}
    original = VivadoSynth.run

    def run(self):
        seen["xeda"] = logging.getLogger("xeda.flows.vivado").isEnabledFor(logging.DEBUG)
        seen["other"] = logging.getLogger("not.xeda").isEnabledFor(logging.DEBUG)
        if fail:
            raise RuntimeError("the flow stops here")
        return original(self)

    monkeypatch.setattr(VivadoSynth, "run", run)
    runner = DefaultRunner(tmp_path / "run", display_results=False, debug=debug)
    try:
        runner.launch_flow(VivadoSynth, _design(), dict(SETTINGS))
    except RuntimeError as error:
        # the one the patched `run()` raises: any other is a failure of the launch itself
        assert fail and str(error) == "the flow stops here", error
    return seen


def test_a_debug_launch_logs_at_debug_for_xeda_alone(tmp_path, monkeypatch) -> None:
    seen = _launch_recording(tmp_path, monkeypatch, debug=True)
    assert seen == {"xeda": True, "other": False}


def test_a_launch_without_debug_leaves_xeda_at_the_level_of_the_process(
    tmp_path, monkeypatch
) -> None:
    seen = _launch_recording(tmp_path, monkeypatch, debug=False)
    assert seen == {"xeda": False, "other": False}


@pytest.mark.parametrize("fail", [False, True], ids=["finished", "raised"])
def test_the_levels_are_put_back_after_a_debug_launch(tmp_path, monkeypatch, fail) -> None:
    before = levels()
    _launch_recording(tmp_path, monkeypatch, debug=True, fail=fail)
    assert levels() == before


def test_a_launch_without_debug_leaves_a_level_the_application_sets_meanwhile(
    tmp_path, monkeypatch
) -> None:
    """The application owns the level of `xeda`; a launch that asked for nothing puts nothing
    back over it."""
    use_fake_tools(monkeypatch)
    original = VivadoSynth.run

    def run(self):
        logging.getLogger("xeda").setLevel(logging.ERROR)  # a handler of the application, say
        return original(self)

    monkeypatch.setattr(VivadoSynth, "run", run)
    DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
        VivadoSynth, _design(), dict(SETTINGS)
    )
    assert logging.getLogger("xeda").level == logging.ERROR


def test_a_debug_launch_leaves_a_level_the_application_sets_meanwhile(
    tmp_path, monkeypatch
) -> None:
    use_fake_tools(monkeypatch)
    original = VivadoSynth.run

    def run(self):
        logging.getLogger("xeda").setLevel(logging.ERROR)
        return original(self)

    monkeypatch.setattr(VivadoSynth, "run", run)
    DefaultRunner(tmp_path / "run", display_results=False, debug=True).launch_flow(
        VivadoSynth, _design(), dict(SETTINGS)
    )
    assert logging.getLogger("xeda").level == logging.ERROR


@pytest.mark.parametrize(
    "order",
    [("a", "b", "a", "b"), ("a", "b", "b", "a")],
    ids=["first-in-first-out", "nested"],
)
@pytest.mark.parametrize("second_debug", [True, False], ids=["both-debug", "second-plain"])
def test_overlapping_launches_put_the_level_back_when_the_last_debug_one_ends(
    order, second_debug
) -> None:
    """Two launchers of one process (threads) enter and leave in any order. The level is
    DEBUG while a debug launch is on, and the process's own after the last one."""
    from xeda.flow_runner.default_runner import xeda_debug_logging

    package = logging.getLogger("xeda")
    scopes = {"a": xeda_debug_logging(True), "b": xeda_debug_logging(second_debug)}
    entered: set = set()
    for name in order:
        if name in entered:
            scopes[name].__exit__(None, None, None)
            entered.discard(name)
            debug_on = "a" in entered or ("b" in entered and second_debug)
            assert (package.level == logging.DEBUG) == debug_on
        else:
            scopes[name].__enter__()
            entered.add(name)
            assert package.level == logging.DEBUG
    assert package.level == logging.NOTSET


def test_a_direct_search_launch_logs_at_debug_when_asked(tmp_path, monkeypatch) -> None:
    """`Dse.run_flow` is a way in too: a caller may use it without `run`."""
    from xeda.flow_runner import Dse
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    seen: list = []

    class Watching(FmaxOptimizer):
        def next_batch(self):
            seen.append(logging.getLogger("xeda.flows.vivado").isEnabledFor(logging.DEBUG))
            return None

    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.v").write_text("module a(input clk); endmodule\n")
    from xeda.design import Design

    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["a.v"], "top": "a", "clock": "clk"}
    )
    runner = Dse(
        Watching,
        optimizer_settings={"init_freq_low": 100.0, "init_freq_high": 200.0},
        run_root=tmp_path / "run",
        max_workers=1,
        debug=True,
    )
    runner.run_flow(
        "vivado_alt_synth",
        design,
        flow_settings={"fpga": "xc7a12tcsg325-1", "clock": {"period": 5.0}},
    )
    assert seen == [True]
    assert logging.getLogger("xeda").level == logging.NOTSET


def test_a_remote_launch_logs_at_debug_when_asked(tmp_path, monkeypatch) -> None:
    """`RemoteRunner.run_remote` is a way in too."""
    from xeda.flow_runner.remote import RemoteRunner

    seen: list = []

    class Stop(Exception):
        pass

    def refuse(self, *args, **kwargs):
        seen.append(logging.getLogger("xeda.flow_runner").isEnabledFor(logging.DEBUG))
        raise Stop

    monkeypatch.setattr(RemoteRunner, "_refuse_unaccepted_request", refuse)
    runner = RemoteRunner(tmp_path / "run", debug=True)
    with pytest.raises(Stop):
        runner.run_remote(_design(), "vivado_synth", "nowhere")
    assert seen == [True]
    assert logging.getLogger("xeda").level == logging.NOTSET


class _RecordingLauncher:
    """Stands in for a launcher the command line builds: it records the settings it is given."""

    given: list = []
    target = None
    design_name = None

    def __init__(self, *args, **settings):
        self.given.append(settings)

    def run_remote(self, *args, **kwargs):
        return {"success": True, "run_path": "/remote/run"}

    def run(self, *args, **kwargs):
        return None


@pytest.mark.parametrize("where", ["command", "group"])
def test_the_command_line_hands_its_debug_option_to_the_remote_runner(
    where, tmp_path, monkeypatch
) -> None:
    from click.testing import CliRunner

    from xeda.cli import cli
    from xeda.flow_runner import remote

    sqrt = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingLauncher)
    _RecordingLauncher.given = []
    args = ["run", "vivado_synth", str(sqrt), "--remote", "host", "--json"]
    args = ["--debug", *args] if where == "group" else [*args, "--debug"]
    CliRunner().invoke(cli, args, catch_exceptions=False)
    assert [given.get("debug") for given in _RecordingLauncher.given] == [True]


@pytest.mark.parametrize("where", ["command", "group"])
def test_the_command_line_hands_its_debug_option_to_the_search(
    where, tmp_path, monkeypatch
) -> None:
    from click.testing import CliRunner

    from xeda import cli as cli_module

    sqrt = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "Dse", _RecordingLauncher)
    _RecordingLauncher.given = []
    args = ["dse", "vivado_synth", "--design", str(sqrt), "--json"]
    args = ["--debug", *args] if where == "group" else [*args, "--debug"]
    CliRunner().invoke(cli_module.cli, args, catch_exceptions=False)
    assert [given.get("debug") for given in _RecordingLauncher.given] == [True]
