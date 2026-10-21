"""A yosys process keeps its temporary files in the run directory of its flow.

Yosys and the programs it starts (`abc`, `abc_new`) make directories and files in the directory
that `TMPDIR` names. `abc_new` never removes its directory `yosys-abc-XXXXXX`, and the cache of
the Liberty reader stays there too. A launch writes nothing outside the run root, and a leftover
file that `yosys -E` lists as an input makes every launch run again. So the flow gives yosys
the environment variable `TMPDIR`, set to a relative name of a directory in the run directory,
and removes that directory when yosys ends. `XDG_STATE_HOME` names the same directory: there
yosys keeps the history of its commands, which it writes in the home directory of the user
otherwise. Xeda's own environment does not change.
"""

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest

from xeda.flow import FlowException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.flows.yosys.common import YOSYS_TEMPORARY_DIRECTORY

from .test_yosys_asic import _counter_design, _design, _tiny
from .tool_utils import (
    launch_until_fresh,
    require_yosys,
    require_yosys_tcl,
    run_outputs_state,
)

ABC_NEW = {"abc": {"mapper": "abc_new"}}
WRAPPED = {**ABC_NEW, "arithmetic": {"wrap_operators": True}}


def test_the_directory_is_a_relative_name_without_a_space():
    assert not Path(YOSYS_TEMPORARY_DIRECTORY).is_absolute()
    assert Path(YOSYS_TEMPORARY_DIRECTORY).parts == (YOSYS_TEMPORARY_DIRECTORY,)
    assert " " not in YOSYS_TEMPORARY_DIRECTORY


# ----------------------------------------------------------------------- the recorded calls


def _recorded_launch(
    tmp_path: Path, monkeypatch, fail: bool = False, settings: Any = None, **given: Any
):
    """Launch `Yosys` with every tool call recorded instead of run. Return the calls, each as
    (executable, environment given to the process, whether the temporary directory existed and
    was empty at the call), and the run directory."""
    parent = tmp_path / "parent-tmp"
    parent.mkdir(exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(parent))
    calls: list[tuple[str, Any, bool]] = []

    def record(executable, args=None, **kwargs):
        run_dir = Path(kwargs["cwd"])
        directory = run_dir / YOSYS_TEMPORARY_DIRECTORY
        calls.append(
            (
                str(executable),
                kwargs.get("env"),
                directory.is_dir() and not any(directory.iterdir()),
            )
        )
        if fail and kwargs.get("env"):
            raise RuntimeError("yosys failed")
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    with contextlib.suppress(Exception if fail else FlowException):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            Yosys, _design(tmp_path / "design"), settings or _tiny(tmp_path, **given)
        )
    (run_dir,) = (tmp_path / "xeda_run").glob("mac/yosys")
    return calls, run_dir, parent


def test_the_yosys_call_gets_the_variable_and_no_other_call_does(tmp_path, monkeypatch):
    calls, run_dir, parent = _recorded_launch(tmp_path, monkeypatch)
    yosys_calls = [call for call in calls if call[1] is not None]
    assert len(yosys_calls) == 1
    executable, env, existed = yosys_calls[0]
    assert executable == "yosys"
    assert env["TMPDIR"] == YOSYS_TEMPORARY_DIRECTORY
    assert env["XDG_STATE_HOME"] == YOSYS_TEMPORARY_DIRECTORY
    assert existed, "the directory exists, and is empty, when yosys starts"
    assert not (run_dir / YOSYS_TEMPORARY_DIRECTORY).exists()
    # the other calls (the version query) get no environment of their own
    assert [call for call in calls if call[1] is None], "the version query is a call too"


def test_xedas_own_environment_is_not_changed(tmp_path, monkeypatch):
    _recorded_launch(tmp_path, monkeypatch)
    assert os.environ["TMPDIR"] == str(tmp_path / "parent-tmp")


def test_the_directory_is_removed_when_yosys_fails(tmp_path, monkeypatch):
    calls, run_dir, _ = _recorded_launch(tmp_path, monkeypatch, fail=True)
    assert any(call[1] is not None for call in calls)
    assert not (run_dir / YOSYS_TEMPORARY_DIRECTORY).exists()


def test_a_link_at_the_name_of_the_directory_is_replaced_and_not_followed(tmp_path, monkeypatch):
    """A tool or a killed run may have left a link at the name: the flow starts with an empty
    directory of its own, and what the link led to is not touched."""
    settings = _tiny(tmp_path)
    _, run_dir, _ = _recorded_launch(tmp_path, monkeypatch, settings=settings)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep").write_text("the file of the user\n")
    (run_dir / YOSYS_TEMPORARY_DIRECTORY).symlink_to(outside, target_is_directory=True)
    calls, _, _ = _recorded_launch(tmp_path, monkeypatch, settings=settings)
    assert [call for call in calls if call[1] is not None][0][2]
    assert (outside / "keep").read_text() == "the file of the user\n"
    assert list(outside.iterdir()) == [outside / "keep"]


# --------------------------------------------------------------------------- real yosys


def _launch(tmp_path: Path, monkeypatch, run_root: str = "xeda_run", **given: Any):
    """Launch the counter with real yosys, with the parent's `TMPDIR` an empty directory."""
    parent = tmp_path / "parent-tmp"
    parent.mkdir()
    monkeypatch.setenv("TMPDIR", str(parent))
    flow = DefaultRunner(tmp_path / run_root, display_results=False).launch_flow(
        Yosys, _counter_design(tmp_path / "design"), _tiny(tmp_path, **given)
    )
    assert flow.succeeded
    return flow, parent


def test_real_yosys_leaves_nothing_in_the_temporary_directory_of_xeda(tmp_path, monkeypatch):
    require_yosys()
    flow, parent = _launch(tmp_path, monkeypatch)
    assert list(parent.iterdir()) == []
    assert not (flow.run_path / YOSYS_TEMPORARY_DIRECTORY).exists()


def test_real_yosys_writes_no_history_in_the_home_directory(tmp_path, monkeypatch):
    """Yosys keeps the history of its commands in `$XDG_STATE_HOME/yosys`, else in
    `~/.local/state/yosys`. The run keeps it in the run directory, which it removes."""
    require_yosys()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    flow, _ = _launch(tmp_path, monkeypatch)
    assert list(home.iterdir()) == []
    assert not (flow.run_path / YOSYS_TEMPORARY_DIRECTORY).exists()


@pytest.mark.parametrize("given", [ABC_NEW, WRAPPED], ids=["abc_new", "wrapped"])
def test_real_yosys_with_abc_new_leaves_nothing_behind(tmp_path, monkeypatch, given):
    """`abc_new` makes its directory in `TMPDIR` and leaves it there."""
    require_yosys_tcl()
    flow, parent = _launch(tmp_path, monkeypatch, **given)
    assert list(parent.iterdir()) == []
    assert not (flow.run_path / YOSYS_TEMPORARY_DIRECTORY).exists()
    assert not list(flow.run_path.glob("yosys-abc-*"))


def test_a_run_directory_with_a_space_in_its_path_still_maps(tmp_path, monkeypatch):
    require_yosys()
    flow, parent = _launch(tmp_path, monkeypatch, run_root="xeda run")
    assert " " in str(flow.run_path)
    assert list(parent.iterdir()) == []
    assert "_X1 " in (flow.run_path / "netlist.v").read_text()


@pytest.mark.parametrize("given", [ABC_NEW, WRAPPED], ids=["abc_new", "wrapped"])
def test_a_second_launch_with_abc_new_is_fresh(tmp_path, monkeypatch, given):
    require_yosys_tcl()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    settings = _tiny(tmp_path, **given)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = _counter_design(tmp_path / "design")
    first = runner.launch_flow(Yosys, design, settings)
    assert first.succeeded and not first.reused
    again = launch_until_fresh(runner, lambda: runner.launch_flow(Yosys, design, settings))
    before = run_outputs_state(again.run_path)
    last = len(runner.launched)
    fresh = runner.launch_flow(Yosys, design, settings)
    assert fresh.reused and all(flow.reused for flow in runner.launched[last:])
    assert run_outputs_state(again.run_path) == before
