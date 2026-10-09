"""A launcher's debug mode is the launch's: it never changes the logging of the process.

An application that embeds xeda owns its logging. A `DefaultRunner(debug=True)` used to set the
root logger to DEBUG for good, so every library of the process logged at DEBUG from then on, and
a later launcher without `debug` did not put it back.
"""

import logging
from collections.abc import Iterator

import pytest

from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth

from .test_vivado_step_tables import PART, _design
from .tool_utils import use_fake_tools

SETTINGS = {"fpga": PART, "clock_period": 5.5}
NAMES = ("", "xeda", "xeda.flow_runner", "xeda.flows.vivado", "not.xeda")


def levels() -> dict:
    return {name: logging.getLogger(name).level for name in NAMES}


@pytest.fixture(autouse=True)
def quiet_process() -> Iterator[None]:
    """An application that logs warnings and up, and puts its logging back after the test."""
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in NAMES}
    root.setLevel(logging.WARNING)
    for name in NAMES[1:]:
        logging.getLogger(name).setLevel(logging.NOTSET)
    yield
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
    except RuntimeError:
        assert fail
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
