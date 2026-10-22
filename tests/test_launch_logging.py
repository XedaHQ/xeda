"""A launch never changes the logging of the process.

An application that embeds xeda owns its logging. A `DefaultRunner(debug=True)` used to set the
root logger to DEBUG for good, so every library of the process logged at DEBUG from then on, and
a later launcher without `debug` did not put it back. Later it set the level of the `xeda` logger
for the length of a launch. The level is the application's to set, and the command line sets it.
"""

import logging
from pathlib import Path
from collections.abc import Iterator

import pytest

from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth

from .remote_fixtures import fake_remote, fake_ssh, remote_host
from .test_vivado_step_tables import PART, _design
from .tool_utils import use_fake_tools

FIXTURES = (fake_ssh, fake_remote, remote_host)
SETTINGS = {"fpga": PART, "clock": {"period": "5.5 ns"}}
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


@pytest.mark.parametrize("debug", [True, False], ids=["debug", "plain"])
def test_a_launch_leaves_the_level_of_the_process_whether_or_not_it_is_debug(
    tmp_path, monkeypatch, debug
) -> None:
    seen = _launch_recording(tmp_path, monkeypatch, debug=debug)
    assert seen == {"xeda": False, "other": False}


@pytest.mark.parametrize("fail", [False, True], ids=["finished", "raised"])
def test_the_levels_are_the_same_after_a_debug_launch(tmp_path, monkeypatch, fail) -> None:
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


def test_a_direct_search_launch_leaves_the_level_alone_when_asked_for_debug(
    tmp_path, monkeypatch
) -> None:
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
        flow_settings={"fpga": "xc7a12tcsg325-1", "clock": {"period": "5 ns"}},
    )
    assert seen == [False]
    assert logging.getLogger("xeda").level == logging.NOTSET


def test_a_remote_launch_changes_no_logger(tmp_path, remote_host) -> None:
    """`run_remote` is a way in too: it takes the log level as an argument, and it neither sets
    a logger of the process nor leaves one changed."""
    from xeda.remote.client import run_remote

    design = tmp_path / "d.yaml"
    design.write_text("name: d\nrtl:\n  sources: []\n  top: t\n")
    before = levels()
    outcome = run_remote(
        "nextpnr",
        design=design,
        destination_text="somewhere",
        run_root=tmp_path / "run",
        flow_settings=["no_such_setting=1"],  # refused at planning, before any connection
        xedaproject=None,
        design_overrides=[],
        design_allow_extra=False,
        target=None,
        select_design_in_project=None,
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        fetch_all=False,
        outputs_to=None,
        overwrite_outputs=False,
        confirm_overwrite=None,
        log_level=logging.DEBUG,
        detailed_logs=False,
    )
    assert not outcome.success and outcome.error is not None
    assert levels() == before


class _RecordingLauncher:
    """Stands in for a launcher the command line builds: it records the settings it is given."""

    given: list = []
    target = None
    design_name = None

    def __init__(self, *args, **settings):
        self.given.append(settings)

    def run(self, *args, **kwargs):
        return None


@pytest.mark.parametrize("where", ["command", "group"])
def test_the_command_line_hands_its_debug_option_to_the_remote_run(
    where, tmp_path, monkeypatch
) -> None:
    from click.testing import CliRunner

    from xeda.cli import cli
    from xeda.remote import client

    given: list = []

    def recorded(chain, **arguments):
        given.append(arguments)
        return client.RemoteOutcome({"success": True, "run_path": "/mirror/run"}, True)

    sqrt = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(client, "run_remote", recorded)
    args = ["run", "vivado_synth", str(sqrt), "--remote", "host", "--json"]
    args = ["--debug", *args] if where == "group" else [*args, "--debug"]
    CliRunner().invoke(cli, args, catch_exceptions=False)
    assert [arguments["log_level"] for arguments in given] == [logging.DEBUG]


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
