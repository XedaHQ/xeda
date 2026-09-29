"""xeda runs only in a directory that is xeda's.

`xeda run vivado_synth sqrt.toml --cwd`, started in the design's own directory, deleted every
file there: `--cwd` makes the current directory the run directory, and Vivado's `clean` (on by
default) empties the run directory before the flow runs. An API `run_path` did the same, and a
design named `..` put its run directory outside the run root, where `--clean` emptied a
directory of the user's.

So a directory given explicitly (`--cwd`, the launcher's `run_path`) is used only if it does not
exist (xeda creates it), is empty, or carries xeda's marker (`.xeda-run-dir`, which xeda writes
into every such directory it runs in); anything else is refused before anything is created,
written or deleted. A directory xeda chooses lies strictly inside the run root by construction:
a design or flow name that would put it elsewhere is refused. Emptying a run directory
(`Flow.purge_run_path`) refuses one that is neither inside a run root nor marked, and a work
directory a flow removes files from by name (Verilator's `sim_dir`, bsc's `bobj_dir`,
Diamond's `impl_folder`, Vivado's `xsim.dir`) must lie inside the run directory. The oracle at
the end lists every place `src/xeda` deletes by name, each reviewed.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar, Iterator

import pytest

from xeda import Design
from xeda.cocotb import Cocotb
from xeda.console import console
from xeda.flow import Flow, FlowFatalError, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import scrub_runs
from xeda.flow.run_dir import claim_run_dir
from xeda.flows import Bsc, BscSim, DiamondSynth, Verilator, VivadoSim, VivadoSynth
from xeda.utils import XedaException

from .settings_samples import flow_classes, minimal_settings
from .tool_utils import FAKE_TOOLS_DIR, fake_calls, fake_returns, use_fake_tools

MARKER = ".xeda-run-dir"
SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"

#: The command of the report, as given: run in the design's own directory.
CWD_RUN = ["run", "vivado_synth", "sqrt.toml", "--cwd"]
XILINX = ["-s", "fpga.part=xc7a12tcsg325-1", "clock.period=10"]
XILINX_SETTINGS = {"fpga": {"part": "xc7a12tcsg325-1"}, "clock": {"period": 10.0}}

REFUSAL = "xeda runs only in an empty directory or one it created"


def _users_directory(root: Path) -> Path:
    """The user's own directory: the sqrt design, a file and a directory of theirs, and
    directories of theirs named like a flow's work directory."""
    shutil.copytree(SQRT, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "notes.txt").write_text("the user's own notes\n")
    (root / "mine").mkdir()
    (root / "mine" / "data.txt").write_text("the user's own data\n")
    for directory, name in (
        ("sim_build", "mine.d"),
        ("bobjs", "Mine.bo"),
        ("diamond_impl", "keep.txt"),
        ("xsim.dir", "keep.txt"),
    ):
        (root / directory).mkdir()
        (root / directory / name).write_text("the user's own file\n")
    return root


def _tree(root: Path) -> dict[str, Any]:
    """Every entry under `root` by its relative path: a file's content, a link's target, or
    `None` for a directory -- so a new, changed or removed entry of any kind shows."""
    tree: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            tree[name] = ("link", os.readlink(path))
        elif path.is_dir():
            tree[name] = None
        else:
            tree[name] = path.read_bytes()
    return tree


def _xeda(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """`xeda *args` started in `cwd`, with the fake EDA tools first on PATH."""
    env = dict(os.environ, COLUMNS="500")
    env["PATH"] = str(FAKE_TOOLS_DIR) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _error(run: subprocess.CompletedProcess) -> dict[str, str]:
    """The error of a failed `--json` run's document."""
    assert run.returncode == 1, run.stdout + run.stderr
    document = json.loads(run.stdout)
    assert document["success"] is False, document
    return document["error"]


def _no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the fake tools (and this Python) on PATH: no real tool can run."""
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(FAKE_TOOLS_DIR), str(Path(sys.executable).parent)])
    )


# ---------------------------------------------------------------------------------------------
# `--cwd` from the command line
# ---------------------------------------------------------------------------------------------

#: How the reported command may be given: as reported, asked to clean, or with `clean = true`
#: in the design file.
CWD_INVOCATIONS: dict[str, list[str]] = {
    "--cwd": [*CWD_RUN, *XILINX],
    "--cwd --clean": [*CWD_RUN, "--clean", *XILINX],
    "--cwd -s clean=true": [*CWD_RUN, *XILINX, "clean=true"],
    "--cwd, clean in the design": [*CWD_RUN, *XILINX],
}


@pytest.mark.parametrize("invocation", CWD_INVOCATIONS)
@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_the_reported_command_is_refused_and_changes_nothing(tmp_path, invocation, json_flag):
    """The reported command, started in the design's directory, is refused with one message
    naming the directory and what to do instead -- and nothing there, or beside it, is created,
    changed or deleted."""
    root = _users_directory(tmp_path / "sqrt")
    if invocation == "--cwd, clean in the design":
        design_file = root / "sqrt.toml"
        text = design_file.read_text()
        assert "[flows.vivado_synth]\n" in text
        design_file.write_text(
            text.replace("[flows.vivado_synth]\n", "[flows.vivado_synth]\nclean = true\n")
        )
    before = _tree(tmp_path)

    args = CWD_INVOCATIONS[invocation]
    run = _xeda(*args[:4], *(["--json"] if json_flag else []), *args[4:], cwd=root)

    assert _tree(tmp_path) == before, run.stdout + run.stderr
    assert not fake_calls(root), "Vivado never ran"
    if json_flag:
        error = _error(run)
        assert error["type"] == "RunDirectoryError", error
        message = error["message"]
    else:
        assert run.returncode == 1, run.stdout + run.stderr
        message = " ".join((run.stdout + run.stderr).split())
    assert f"{root.resolve()} holds files xeda did not put there" in message, message
    assert REFUSAL in message, message
    assert "Run without --cwd" in message, message


def test_cwd_in_an_empty_directory_runs_there_and_again(tmp_path):
    """`--cwd` in an empty directory runs there, and marks it as xeda's; a second run there --
    whose Vivado `clean` empties the directory first -- works as well, and keeps the marker."""
    design_dir = tmp_path / "sqrt"
    shutil.copytree(SQRT, design_dir, ignore=shutil.ignore_patterns("__pycache__"))
    design_before = _tree(design_dir)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    for attempt in (1, 2):
        run = _xeda(
            "run",
            "vivado_synth",
            str(design_dir / "sqrt.toml"),
            "--cwd",
            "--json",
            *XILINX,
            cwd=run_dir,
        )
        assert run.returncode == 0, f"run {attempt}: {run.stdout}{run.stderr}"
        document = json.loads(run.stdout)
        assert document["success"] is True
        assert Path(document["run_path"]).resolve() == run_dir.resolve()
        assert (run_dir / MARKER).is_file(), f"run {attempt} left no marker"
        assert fake_calls(run_dir), f"run {attempt}: Vivado ran in the directory"
        assert (run_dir / "results.json").is_file()
    assert _tree(design_dir) == design_before


# ---------------------------------------------------------------------------------------------
# The launcher's `run_path`
# ---------------------------------------------------------------------------------------------


def _launcher(tmp_path: Path, **settings: Any) -> DefaultRunner:
    """A launcher as `xeda run` makes one: run directories `<run root>/<design>/<flow>`."""
    settings = dict(cached_dependencies=False, incremental=True) | settings
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)


def test_a_given_run_path_holding_files_is_refused(tmp_path, monkeypatch):
    """The API's `run_path` follows the same rule as `--cwd`: a directory holding files xeda did
    not put there is refused, naming it, and left exactly as it was."""
    use_fake_tools(monkeypatch)
    root = _users_directory(tmp_path / "sqrt")
    before = _tree(root)
    launcher = _launcher(tmp_path, run_path=root)

    with pytest.raises(XedaException) as refused:
        launcher.run("vivado_synth", design=root / "sqrt.toml", flow_settings=XILINX_SETTINGS)

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert f"{root} holds files xeda did not put there" in str(refused.value)
    assert _tree(root) == before
    assert not fake_calls(root)


@pytest.mark.parametrize("state", ["absent", "empty", "marked, holding files"])
def test_a_given_run_path_xeda_may_use_is_marked_and_reused(tmp_path, monkeypatch, state):
    """A `run_path` that does not exist is created, an empty one adopted -- each marked as
    xeda's -- and a marked one used as it is, however many runs go there."""
    use_fake_tools(monkeypatch)
    run_dir = tmp_path / "given" / "run"
    if state != "absent":
        run_dir.mkdir(parents=True)
    if state == "marked, holding files":
        (run_dir / MARKER).write_text("format = 1\n")
        (run_dir / "vivado_synth.tcl").write_text("# an earlier run's\n")
    design = Design.from_file(SQRT / "sqrt.toml")

    for attempt in (1, 2):
        flow = _launcher(tmp_path, run_path=run_dir).run(
            "vivado_synth", design=design, flow_settings=XILINX_SETTINGS
        )
        assert flow is not None and flow.succeeded, f"run {attempt}"
        assert flow.run_path == run_dir
        assert (run_dir / MARKER).is_file(), f"run {attempt}"
        assert fake_calls(run_dir), f"run {attempt}"


FLOWS = [cls for cls, _ in flow_classes()]


@pytest.mark.parametrize("flow_class", FLOWS, ids=[cls.name for cls in FLOWS])
def test_every_flow_refuses_a_given_directory_holding_files(tmp_path, monkeypatch, flow_class):
    """Whatever the flow, a given run directory holding files xeda did not put there is refused
    before anything is created, written or deleted there."""
    _no_tools(monkeypatch)
    root = _users_directory(tmp_path / "sqrt")
    before = _tree(root)
    monkeypatch.chdir(root)

    with pytest.raises(Exception) as refused:
        _launcher(tmp_path).launch_flow(
            flow_class,
            Design.from_file(root / "sqrt.toml"),
            minimal_settings(flow_class),
            run_path=root,
        )

    assert type(refused.value).__name__ == "RunDirectoryError", refused.value
    assert _tree(root) == before


# ---------------------------------------------------------------------------------------------
# A directory xeda chooses lies inside the run root
# ---------------------------------------------------------------------------------------------


def _dotdot_design(start: Path) -> Path:
    """A design named `..`, whose run directory `<start>/xeda_run/../<flow>` is `<start>/<flow>`."""
    shutil.copy(SQRT / "sqrt.vhdl", start / "sqrt.vhdl")
    design_file = start / "d.toml"
    design_file.write_text(
        'name = ".."\n'
        'language.vhdl.standard = "2008"\n'
        "[rtl]\n"
        'sources = ["sqrt.vhdl"]\n'
        'top = "sqrt"\n'
        'clock.port = "clk"\n'
    )
    return design_file


@pytest.mark.parametrize("option", ["--clean", "--no-incremental"])
def test_a_design_named_dotdot_is_refused_and_the_users_directory_survives(tmp_path, option):
    """A design named `..` would run in `<start>/vivado_synth`, a directory of the user's, which
    `--clean` (Vivado's `clean`) or `--no-incremental` (the launcher's) emptied. It is refused,
    naming the name, and the user's directory is untouched."""
    start = tmp_path / "start"
    start.mkdir()
    _dotdot_design(start)
    canary = start / "vivado_synth" / "keep.txt"
    canary.parent.mkdir()
    canary.write_text("the user's own file\n")

    run = _xeda("run", "vivado_synth", "d.toml", option, "--json", *XILINX, cwd=start)

    assert canary.read_text() == "the user's own file\n", run.stdout + run.stderr
    assert sorted(p.name for p in canary.parent.iterdir()) == ["keep.txt"]
    error = _error(run)
    assert error["type"] == "RunDirectoryError", error
    assert "design name '..'" in error["message"], error


def test_scrub_refuses_a_design_named_dotdot(tmp_path):
    """`xeda scrub vivado_synth ..` would offer to remove `<start>/vivado_synth`, a directory of
    the user's: the design name is refused, naming it, and nothing is removed."""
    start = tmp_path / "start"
    canary = start / "vivado_synth" / "keep.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("the user's own file\n")

    run = _xeda("scrub", "vivado_synth", "..", "--json", cwd=start)

    assert canary.read_text() == "the user's own file\n"
    assert run.returncode != 0, run.stdout + run.stderr
    document = json.loads(run.stdout)
    assert document["success"] is False, document
    assert "design name '..'" in document["error"]["message"], document


ESCAPING_NAMES = {
    "..": "..",
    ".": ".",
    "a separator": "a/b",
    "a backslash": "a\\b",
    "an absolute path": "/tmp/elsewhere",
    "nothing once sanitized": "...",
}


@pytest.mark.parametrize("name", ESCAPING_NAMES.values(), ids=list(ESCAPING_NAMES))
@pytest.mark.parametrize("what", ["design", "flow"])
def test_a_name_that_would_leave_the_run_root_is_refused(tmp_path, name, what):
    """A design or flow name that does not name one directory inside the run root is refused,
    naming it."""
    launcher = _launcher(tmp_path)
    names = {"design": "sqrt", "flow": "vivado_synth"} | {what: name}

    with pytest.raises(XedaException) as refused:
        launcher.get_flow_run_path(names["design"], names["flow"])

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert f"{what} name {name!r}" in str(refused.value)


def test_a_run_directory_linked_out_of_the_run_root_is_refused(tmp_path):
    """A run directory must resolve inside the run root: one reached through a link to a
    directory elsewhere is refused."""
    launcher = _launcher(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (launcher.xeda_run_dir / "sqrt").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(XedaException) as refused:
        launcher.get_flow_run_path("sqrt", "vivado_synth")

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(elsewhere.resolve() / "vivado_synth") in str(refused.value)


def test_a_run_directory_inside_the_run_root_is_the_usual_one(tmp_path):
    """The names of ordinary designs and flows map to `<run root>/<design>/<flow>`, as before."""
    launcher = _launcher(tmp_path)
    root = launcher.xeda_run_dir
    assert launcher.get_flow_run_path("sqrt", "vivado_synth") == root / "sqrt" / "vivado_synth"
    assert launcher.get_flow_run_path("my design", "ghdl_sim") == root / "my design" / "ghdl_sim"


# ---------------------------------------------------------------------------------------------
# Emptying a run directory
# ---------------------------------------------------------------------------------------------


def _vivado_synth(run_dir: Path) -> VivadoSynth:
    """A flow built directly -- no launcher -- to run in `run_dir`."""
    return VivadoSynth(
        VivadoSynth.Settings(**XILINX_SETTINGS), Design.from_file(SQRT / "sqrt.toml"), run_dir
    )


def test_purge_run_path_refuses_a_directory_that_is_not_xedas(tmp_path):
    """A flow's `clean` does not empty a directory that is neither inside a run root nor marked
    as xeda's -- whoever built the flow."""
    root = _users_directory(tmp_path / "sqrt")
    before = _tree(root)
    flow = _vivado_synth(root)

    with pytest.raises(XedaException) as refused:
        flow.purge_run_path()

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(root) in str(refused.value)
    assert _tree(root) == before


def test_purge_run_path_empties_a_marked_directory_but_keeps_its_marker(tmp_path):
    """A marked directory is xeda's: `clean` empties it, all but the marker, so it stays
    xeda's for the next run."""
    run_dir = tmp_path / "run"
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / MARKER).write_text("format = 1\n")
    (run_dir / "vivado_synth.tcl").write_text("# an earlier run's\n")
    (run_dir / "reports" / "timing.rpt").write_text("an earlier run's\n")

    _vivado_synth(run_dir).purge_run_path()

    assert sorted(p.name for p in run_dir.iterdir()) == [MARKER]


def test_clean_empties_a_run_directory_xeda_made(tmp_path, monkeypatch):
    """A run directory xeda made carries its marker: the next run's `--clean` empties it, all but
    the marker, and the run goes ahead."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(SQRT / "sqrt.toml")
    first = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=XILINX_SETTINGS)
    assert first is not None and first.succeeded
    run_dir = first.run_path
    assert (run_dir / MARKER).is_file(), "xeda marks every run directory it makes"
    (run_dir / "stale.txt").write_text("an earlier run's\n")

    flow = _launcher(tmp_path, cleanup_before_run=True).run(
        "vivado_synth", design=design, flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded
    assert flow.run_path == run_dir
    assert not (run_dir / "stale.txt").exists()
    assert (run_dir / MARKER).is_file()


# ---------------------------------------------------------------------------------------------
# Work directories a flow removes files from by name
# ---------------------------------------------------------------------------------------------


def _outside(tmp_path: Path, launcher: DefaultRunner, flow: str, name: str) -> Path:
    """A directory of the user's at `<run dir>/../<name>` of `flow`'s run directory, holding a
    file of every kind a work directory holds."""
    outside = launcher.get_flow_run_path("sqrt", flow).parent / name
    outside.mkdir(parents=True)
    for file in ("keep.txt", "mine.d", "Mine.bo"):
        (outside / file).write_text("the user's own file\n")
    return outside


@pytest.mark.parametrize("clean", [False, True], ids=["default", "clean"])
def test_a_verilator_sim_dir_outside_the_run_directory_is_refused(tmp_path, monkeypatch, clean):
    """Verilator removes `*.d` files from its `sim_dir`, and with `clean` the whole `sim_dir`: a
    `sim_dir` of `../x` is refused, naming it, and the directory there is untouched."""
    _no_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "verilator", "x")
    before = _tree(outside)
    source = tmp_path / "top.v"
    source.write_text("module top; endmodule\n")
    design = Design(name="sqrt", rtl={"sources": [str(source)], "top": "top"}, design_root=tmp_path)

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(Verilator, design, {"sim_dir": "../x", "clean": clean})

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert "sim_dir" in str(refused.value) and str(outside) in str(refused.value)
    assert _tree(outside) == before


@pytest.mark.parametrize(
    "flow_class, setting",
    [(Bsc, "bobj_dir"), (Bsc, "verilog_out_dir"), (BscSim, "bobj_dir"), (BscSim, "sim_dir")],
    ids=["bsc-bobj_dir", "bsc-verilog_out_dir", "bsc_sim-bobj_dir", "bsc_sim-sim_dir"],
)
def test_a_bsc_work_directory_outside_the_run_directory_is_refused(
    tmp_path, monkeypatch, flow_class, setting
):
    """With `cleanup_bobjs`, bsc removes an earlier run's packages (`.bo`/`.ba`) from `bobj_dir`
    and its generated modules (a `.use` and the `.v` beside it) from the output directory: either
    directory outside the run directory is refused, naming it."""
    _no_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, flow_class.name, "bsc_work")
    for name in ("Mine.ba", "mkMine.use", "mkMine.v"):
        (outside / name).write_text("the user's own file\n")
    before = _tree(outside)
    source = tmp_path / "Top.bsv"
    source.write_text("package Top;\nendpackage\n")
    design = Design(
        name="sqrt",
        rtl={"sources": [str(source)], "top": "mkTop"},
        tb={"sources": [str(source)], "top": "mkTop"},
        design_root=tmp_path,
    )

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(flow_class, design, {setting: "../bsc_work"})

    assert type(refused.value).__name__ == "RunDirectoryError", refused.value
    assert setting in str(refused.value) and str(outside) in str(refused.value)
    assert _tree(outside) == before


def test_a_diamond_impl_folder_outside_the_run_directory_is_refused(tmp_path, monkeypatch):
    """Diamond's script deletes its `impl_folder` before creating the project: an `impl_folder`
    outside the run directory is refused, naming it, before the script runs."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "diamond_synth", "impl")
    before = _tree(outside)
    settings = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": 10.0}}

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(
            DiamondSynth,
            Design.from_file(SQRT / "sqrt.toml"),
            settings | {"impl_folder": "../impl"},
        )

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert "impl_folder" in str(refused.value) and str(outside) in str(refused.value)
    assert _tree(outside) == before
    assert not fake_calls(launcher.get_flow_run_path("sqrt", "diamond_synth"))


@pytest.mark.parametrize(
    "impl_folder",
    ["$::env(XEDA_TEST_TARGET)", "[set ::env(XEDA_TEST_TARGET)]", "${::env(XEDA_TEST_TARGET)}"],
    ids=["variable", "command", "braced variable"],
)
def test_a_diamond_impl_folder_is_deleted_exactly_as_checked(tmp_path, monkeypatch, impl_folder):
    """The reviewer's reproduction: an `impl_folder` that Tcl would substitute -- here to a
    directory of the user's outside the run directory -- was checked as the literal path it is,
    then rendered inside double quotes, where Tcl substituted it before `file delete -force`.
    The script is handed exactly the path the check approved, as one literal word."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "diamond_synth", "users")
    before = _tree(outside)
    monkeypatch.setenv("XEDA_TEST_TARGET", str(outside))
    settings = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": 10.0}}

    try:
        launcher.launch_flow(
            DiamondSynth,
            Design.from_file(SQRT / "sqrt.toml"),
            settings | {"impl_folder": impl_folder},
        )
    except Exception:  # pylint: disable=broad-except
        pass  # the fake writes empty reports, which diamond's parser cannot read; not the point

    assert _tree(outside) == before
    run_dir = launcher.get_flow_run_path("sqrt", "diamond_synth")
    (new,) = [call for call in fake_calls(run_dir) if call[:2] == ["prj_project", "new"]]
    assert new[new.index("-impl_dir") + 1] == str(run_dir.resolve() / impl_folder)


def test_a_work_directory_a_tool_left_as_a_link_is_removed_as_a_link(tmp_path, monkeypatch):
    """A tool may turn a work directory into a symbolic link to anywhere (an install tree, say).
    The Vivado simulation script deletes `xsim.dir` in the run directory: the next run removes
    the link itself and proceeds -- it neither fails nor touches what the link leads to."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "vivado_sim", "xsim")
    before = _tree(outside)
    run_dir = launcher.get_flow_run_path("sqrt", "vivado_sim")
    run_dir.mkdir(parents=True)
    (run_dir / MARKER).write_text("format = 1\n")  # xeda's, where a tool made a link
    (run_dir / "xsim.dir").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "tb.v"
    source.write_text("module tb; endmodule\n")
    design = Design(
        name="sqrt",
        rtl={"sources": [str(source)], "top": "tb"},
        tb={"top": "tb"},
        design_root=tmp_path,
    )

    launcher.launch_flow(VivadoSim, design, {})

    assert not (run_dir / "xsim.dir").is_symlink()
    assert _tree(outside) == before
    assert fake_calls(run_dir)


def test_a_work_directory_behind_a_link_out_of_the_run_directory_is_refused(tmp_path, monkeypatch):
    """Only the work directory's own name may be a link: when an earlier component of its path is
    one that leads out (`tools/sim` with `tools` -> elsewhere), the directory really lies
    outside the run directory, and is refused, naming it, with nothing removed."""
    _no_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "verilator", "x")
    before = _tree(outside)
    run_dir = launcher.get_flow_run_path("sqrt", "verilator")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / MARKER).write_text("format = 1\n")
    (run_dir / "tools").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "top.v"
    source.write_text("module top; endmodule\n")
    design = Design(name="sqrt", rtl={"sources": [str(source)], "top": "top"}, design_root=tmp_path)

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(Verilator, design, {"sim_dir": "tools/sim", "clean": True})

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert "sim_dir" in str(refused.value)
    assert (run_dir / "tools").is_symlink()
    assert _tree(outside) == before


# ---------------------------------------------------------------------------------------------
# A run directory xeda chooses is used only if it is xeda's
# ---------------------------------------------------------------------------------------------


def _users_run_root(tmp_path: Path, subdir: str = "vivado_synth") -> tuple[Path, Path]:
    """The sqrt design, and a directory of the user's, `myrundir`, that holds a file of theirs at
    the run directory xeda derives there: `myrundir/sqrt/<subdir>/my_data.txt`."""
    design_dir = tmp_path / "sqrt"
    shutil.copytree(SQRT, design_dir, ignore=shutil.ignore_patterns("__pycache__"))
    canary = tmp_path / "myrundir" / "sqrt" / subdir / "my_data.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("the user's own data\n")
    return design_dir, canary


@pytest.mark.parametrize("option", [[], ["--no-incremental"], ["--clean"], ["--scrub"]])
def test_a_users_directory_at_the_derived_run_path_is_refused(tmp_path, option):
    """`--xeda-run-dir myrundir`, where the user keeps `myrundir/sqrt/vivado_synth/my_data.txt`:
    vivado_synth's default `clean`, `--clean` and `--no-incremental` deleted it. A run directory
    xeda chooses is used only if xeda made it, marked it, or it holds an earlier xeda run of the
    same flow; this one is refused, naming it, before anything runs."""
    design_dir, canary = _users_run_root(tmp_path)
    before = _tree(tmp_path / "myrundir")

    run = _xeda(
        "run",
        "vivado_synth",
        str(design_dir / "sqrt.toml"),
        "--xeda-run-dir",
        "myrundir",
        "--json",
        *option,
        *XILINX,
        cwd=tmp_path,
    )

    assert _tree(tmp_path / "myrundir") == before, run.stdout + run.stderr
    error = _error(run)
    assert error["type"] == "RunDirectoryError", error
    assert str(canary.parent) in error["message"], error
    assert "--xeda-run-dir" in error["message"], error


def test_scrub_refuses_a_users_directory_it_would_remove(tmp_path, monkeypatch):
    """`scrub` removes a flow's earlier run directories: one that is not xeda's is refused,
    naming it, before anything is removed or confirmed; xeda's own go as before."""
    base = tmp_path / "myrundir" / "sqrt"
    users = base / "vivado_synth_0123456789abcdef"
    users.mkdir(parents=True)
    (users / "my_data.txt").write_text("the user's own data\n")
    ours = base / "vivado_synth"
    ours.mkdir()
    (ours / MARKER).write_text("format = 1\n")
    asked = []
    monkeypatch.setattr(console, "input", lambda *a, **kw: asked.append(a) or "yes")

    with pytest.raises(XedaException) as refused:
        scrub_runs("vivado_synth", base)

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(users) in str(refused.value)
    assert (users / "my_data.txt").is_file() and ours.is_dir()
    assert not asked, "refused before asking"

    shutil.rmtree(users)
    assert scrub_runs("vivado_synth", base)
    assert not ours.exists()


def test_xeda_scrub_refuses_a_users_directory(tmp_path):
    """`xeda scrub vivado_synth sqrt --xeda-run-dir myrundir` refuses the user's directory at the
    derived run path, naming it, with a JSON failure document."""
    _, canary = _users_run_root(tmp_path)

    run = _xeda(
        "scrub", "vivado_synth", "sqrt", "--xeda-run-dir", "myrundir", "--json", cwd=tmp_path
    )

    assert canary.is_file()
    error = _error(run)
    assert error["type"] == "RunDirectoryError", error
    assert str(canary.parent) in error["message"], error


def _earlier_run(tmp_path: Path, monkeypatch) -> Path:
    """The run directory of an earlier `vivado_synth` run as a 0.4.2 xeda left it: no marker,
    its `settings.json` naming the flow, and a file of the run's own."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(SQRT / "sqrt.toml")
    flow = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=XILINX_SETTINGS)
    assert flow is not None and flow.succeeded
    (flow.run_path / MARKER).unlink()
    (flow.run_path / "stale.txt").write_text("an earlier run's\n")
    return flow.run_path


@pytest.mark.parametrize("option", [{}, {"incremental": False}], ids=["default", "no-incremental"])
def test_an_earlier_xeda_runs_directory_is_adopted(tmp_path, monkeypatch, option):
    """An existing `xeda_run` tree keeps working: a directory holding an earlier xeda run of the
    same flow (its `settings.json` says so) is adopted, marked, and cleaned as before."""
    run_dir = _earlier_run(tmp_path, monkeypatch)

    flow = _launcher(tmp_path, **option).run(
        "vivado_synth", design=Design.from_file(SQRT / "sqrt.toml"), flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded and flow.run_path == run_dir
    assert (run_dir / MARKER).is_file()
    assert not (run_dir / "stale.txt").exists(), "vivado_synth's clean emptied it"


@pytest.mark.parametrize(
    "record",
    [
        {"flow_name": "yosys_fpga", "flow_settings": {}, "xeda_version": "0.4.2"},
        {"flow_name": "vivado_synth"},
        ["vivado_synth"],
        "not JSON",
    ],
    ids=["another flow's", "no run record", "a list", "not JSON"],
)
def test_a_directory_with_no_earlier_run_of_the_flow_is_refused(tmp_path, monkeypatch, record):
    """A `settings.json` that is not xeda's run record of this flow does not make a directory
    xeda's."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    run_dir = launcher.get_flow_run_path("sqrt", "vivado_synth")
    run_dir.mkdir(parents=True)
    text = record if isinstance(record, str) else json.dumps(record)
    (run_dir / "settings.json").write_text(text)
    before = _tree(run_dir)

    with pytest.raises(XedaException) as refused:
        launcher.run(
            "vivado_synth",
            design=Design.from_file(SQRT / "sqrt.toml"),
            flow_settings=XILINX_SETTINGS,
        )

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(run_dir) in str(refused.value)
    assert _tree(run_dir) == before
    assert not fake_calls(run_dir)


@pytest.fixture
def toy_flows():
    """A flow with one dependency, each writing a file into its run directory. Registered while
    the test runs only, so the sweeps over every flow never see them."""

    class ToyDep(Flow):
        """A dependency that writes one file, and whose `clean` empties its run directory."""

        results_description: ClassVar[dict[str, str]] = {}

        def clean(self) -> None:
            self.purge_run_path()

        def run(self) -> None:
            (self.run_path / "dep.txt").write_text("dep\n")

    class ToyTop(Flow):
        """A flow with one dependency."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyDep, ToyDep.Settings())

        def run(self) -> None:
            (self.run_path / "top.txt").write_text("top\n")

    yield ToyDep, ToyTop
    for cls in (ToyDep, ToyTop):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


def test_every_run_directory_xeda_makes_is_marked(tmp_path, toy_flows):
    """A flow's run directory and its dependency's (nested in it) are each marked when xeda makes
    them -- or finds them empty -- so the next run knows them for xeda's."""
    _, top = toy_flows
    design = Design.from_file(SQRT / "sqrt.toml")
    launcher = _launcher(tmp_path)
    empty = launcher.get_flow_run_path("sqrt", top.name)
    empty.mkdir(parents=True)  # empty: nothing of anyone's to lose

    flow = launcher.launch_flow(top, design, {})

    assert flow.succeeded
    assert flow.run_path == empty and (empty / MARKER).is_file()
    (dep,) = flow.completed_dependencies
    assert dep.run_path.parent == empty and (dep.run_path / MARKER).is_file()

    again = _launcher(tmp_path, incremental=False).launch_flow(top, design, {})
    assert again.succeeded and (again.run_path / MARKER).is_file()


def test_a_dependency_directory_linked_out_of_its_depender_is_refused(tmp_path, toy_flows):
    """A dependency runs in `<depender's run directory>/<its name>`: a link there to a directory
    elsewhere -- marked as xeda's, even -- is refused before it is claimed or cleaned, like any run
    directory xeda derives that does not resolve inside the directory it derives it in."""
    dep, top = toy_flows
    design = Design.from_file(SQRT / "sqrt.toml")
    first = _launcher(tmp_path).launch_flow(top, design, {})
    dep_dir = first.run_path / dep.name
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / MARKER).write_text("format = 1\n")
    (outside / "keep.txt").write_text(PRECIOUS)
    shutil.rmtree(dep_dir)
    dep_dir.symlink_to(outside, target_is_directory=True)
    before = _tree(outside)

    with pytest.raises(XedaException) as refused:
        _launcher(tmp_path, cleanup_before_run=True).launch_flow(top, design, {})

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(dep_dir) in str(refused.value)
    assert _tree(outside) == before


# ---------------------------------------------------------------------------------------------
# A run directory is a directory, never a link to one
# ---------------------------------------------------------------------------------------------


def _marked_elsewhere(directory: Path) -> Path:
    """A directory marked as xeda's -- whatever it was, the marker says it is xeda's -- holding a
    file of the user's."""
    directory.mkdir(parents=True)
    (directory / MARKER).write_text("format = 1\n")
    (directory / "keep.txt").write_text(PRECIOUS)
    return directory


def test_a_given_run_path_that_is_a_link_is_refused(tmp_path, toy_flows):
    """A run directory given explicitly (`--cwd`, the launcher's `run_path`) that is itself a
    link is refused, naming it, before it is claimed or cleaned -- even one leading to a
    directory marked as xeda's: its marker was read through the link, and the run cleaned and
    wrote there, in whatever directory the link leads to."""
    dep, _ = toy_flows
    elsewhere = _marked_elsewhere(tmp_path / "elsewhere")
    link = tmp_path / "run"
    link.symlink_to(elsewhere, target_is_directory=True)
    before = _tree(elsewhere)

    with pytest.raises(XedaException) as refused:
        _launcher(tmp_path, cleanup_before_run=True).launch_flow(
            dep, Design.from_file(SQRT / "sqrt.toml"), {}, run_path=link
        )

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert f"{link} is a symbolic link" in str(refused.value)
    assert _tree(elsewhere) == before
    assert link.is_symlink()


def test_a_derived_run_directory_that_is_a_link_is_refused(tmp_path, toy_flows):
    """The same for a run directory xeda derives, `<run root>/<design>/<flow>`: a link there is
    refused, naming it, even when it leads to another directory inside the run root marked as
    xeda's -- it is not the flow's run directory, whatever it is."""
    dep, _ = toy_flows
    launcher = _launcher(tmp_path, cleanup_before_run=True)
    run_dir = launcher.get_flow_run_path("sqrt", dep.name)
    other = _marked_elsewhere(run_dir.parent / "other")
    run_dir.symlink_to(other, target_is_directory=True)
    before = _tree(other)

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(dep, Design.from_file(SQRT / "sqrt.toml"), {})

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert f"{run_dir} is a symbolic link" in str(refused.value)
    assert _tree(other) == before


def test_purge_run_path_refuses_a_link(tmp_path):
    """A flow's `clean` does not empty its run directory through a link, even one to a marked
    directory -- whoever built the flow."""
    elsewhere = _marked_elsewhere(tmp_path / "elsewhere")
    link = tmp_path / "run"
    link.symlink_to(elsewhere, target_is_directory=True)
    before = _tree(elsewhere)

    with pytest.raises(XedaException) as refused:
        _vivado_synth(link).purge_run_path()

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert f"{link} is a symbolic link" in str(refused.value)
    assert _tree(elsewhere) == before


def test_scrub_refuses_a_link_among_a_flows_run_directories(tmp_path, monkeypatch):
    """Scrubbing a flow's run directories refuses one that is a link, naming it, before anything
    is asked or removed: it would remove, through the link, whatever it leads to."""
    design_dir = tmp_path / "xeda_run" / "sqrt"
    other = _marked_elsewhere(design_dir / "other")
    link = design_dir / "vivado_synth"
    link.symlink_to(other, target_is_directory=True)
    before = _tree(other)
    asked = []
    monkeypatch.setattr(console, "input", lambda prompt: asked.append(prompt) or "yes")

    with pytest.raises(XedaException) as refused:
        scrub_runs("vivado_synth", design_dir)

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(link) in str(refused.value) and "symbolic link" in str(refused.value)
    assert not asked
    assert _tree(other) == before and link.is_symlink()


# ---------------------------------------------------------------------------------------------
# Nothing outside the run directory is deleted
# ---------------------------------------------------------------------------------------------

PRECIOUS = "the user's own file, at the path they named\n"


def test_a_named_bitstream_outside_the_run_directory_survives_the_pre_run_stage(
    tmp_path, monkeypatch
):
    """An earlier file at an output path named outside the run directory is left for the tool to
    overwrite (as 0.4.2 did), never deleted by xeda: here Vivado's `write_bitstream` fails, and
    the file is exactly as it was -- and is not reported as the failed run's bitstream."""
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "write_bitstream")
    canary = tmp_path / "external" / "my.bit"
    canary.parent.mkdir()
    canary.write_text(PRECIOUS)

    flow = _launcher(tmp_path).run(
        "vivado_synth",
        design=Design.from_file(SQRT / "sqrt.toml"),
        flow_settings={**XILINX_SETTINGS, "bitstream": str(canary)},
    )

    assert canary.read_text() == PRECIOUS
    assert flow is not None and not flow.succeeded
    assert "bitstream" not in flow.results.artifacts


def test_a_bitstream_outside_the_run_directory_from_before_the_run_is_not_this_runs(
    tmp_path, monkeypatch
):
    """A run that reports `write_bitstream` complete, although it wrote no bitstream, fails
    naming the path -- also when a file from before the run is there, which is kept."""
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "write_bitstream")
    fake_returns(
        monkeypatch,
        {
            ("get_property", "STATUS", "impl_1"): "write_bitstream Complete!",
            ("get_property", "PROGRESS", "impl_1"): "100%",
        },
    )
    canary = tmp_path / "external" / "my.bit"
    canary.parent.mkdir()
    canary.write_text(PRECIOUS)

    with pytest.raises(FlowFatalError, match="from before the run") as raised:
        _launcher(tmp_path).run(
            "vivado_synth",
            design=Design.from_file(SQRT / "sqrt.toml"),
            flow_settings={**XILINX_SETTINGS, "bitstream": str(canary)},
        )

    assert str(canary) in str(raised.value)
    assert canary.read_text() == PRECIOUS


def test_a_named_saif_outside_the_run_directory_survives(tmp_path, monkeypatch):
    """The Vivado simulation script deleted an existing SAIF file before `open_saif`: one named
    outside the run directory is now left for Vivado to overwrite."""
    use_fake_tools(monkeypatch)
    canary = tmp_path / "external" / "my.saif"
    canary.parent.mkdir()
    canary.write_text(PRECIOUS)
    source = tmp_path / "tb.v"
    source.write_text("module tb; endmodule\n")
    design = Design(
        name="sqrt",
        rtl={"sources": [str(source)], "top": "tb"},
        tb={"top": "tb"},
        design_root=tmp_path,
    )

    flow = _launcher(tmp_path).launch_flow(VivadoSim, design, {"saif": str(canary)})

    assert canary.read_text() == PRECIOUS
    assert ["open_saif", str(canary)] in fake_calls(flow.run_path)


def test_a_cocotb_results_file_outside_the_run_directory_is_refused(tmp_path, monkeypatch):
    """cocotb's results file is removed before each simulation, so that an earlier one cannot
    pass for this run's: one named outside the run directory is refused, not deleted."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    canary = tmp_path / "results.xml"
    canary.write_text(PRECIOUS)
    monkeypatch.chdir(run_dir)  # a simulator runs in its run directory

    with pytest.raises(XedaException) as refused:
        Cocotb(sim_name="verilator", results_xml="../results.xml").discard_results()

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert "results_xml" in str(refused.value)
    assert canary.read_text() == PRECIOUS


def test_a_stale_output_is_removed_only_inside_the_run_directory(tmp_path):
    """`Flow.remove_stale_output`: an earlier copy inside the run directory is removed; one
    outside it is kept, and until the run writes it anew it is not the run's own."""
    run_dir = tmp_path / "run"
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / MARKER).write_text("format = 1\n")
    inside = run_dir / "outputs" / "x.bit"
    inside.write_text("an earlier run's\n")
    outside = tmp_path / "x.bit"
    outside.write_text(PRECIOUS)
    flow = _vivado_synth(run_dir)

    flow.remove_stale_output("outputs/x.bit")
    flow.remove_stale_output(outside)
    flow.remove_stale_output("../x.bit")

    assert not inside.exists()
    assert outside.read_text() == PRECIOUS
    assert not flow.wrote_output("outputs/x.bit") and not flow.wrote_output(outside)
    os.utime(outside, ns=(1, 1))  # rewritten, as the tool would
    assert flow.wrote_output(outside)


# ---------------------------------------------------------------------------------------------
# xeda never writes through a link
# ---------------------------------------------------------------------------------------------


def _linked_canary(tmp_path: Path, link: Path, name: str) -> Path:
    """A file of the user's outside the run directory, and a symbolic link to it at `link`."""
    canary = tmp_path / "users" / name
    canary.parent.mkdir(exist_ok=True)
    canary.write_text(PRECIOUS)
    link.symlink_to(canary)
    return canary


def test_the_marker_is_never_written_through_a_link(tmp_path):
    """Adopting an earlier run whose `.xeda-run-dir` is a link to a file of the user's refuses,
    naming it, rather than writing the marker through the link."""
    run_dir = tmp_path / "xeda_run" / "sqrt" / "vivado_synth"
    run_dir.mkdir(parents=True)
    (run_dir / "settings.json").write_text(
        json.dumps({"flow_name": "vivado_synth", "flow_settings": {}, "xeda_version": "0.4.2"})
    )
    canary = _linked_canary(tmp_path, run_dir / MARKER, "notes.txt")

    with pytest.raises(XedaException) as refused:
        claim_run_dir(run_dir, "vivado_synth")

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert str(run_dir / MARKER) in str(refused.value)
    assert canary.read_text() == PRECIOUS
    assert (run_dir / MARKER).is_symlink()


#: The files xeda itself generates in `vivado_synth`'s run directory (the rest are the tool's).
GENERATED = [
    "vivado_synth.tcl",
    "clock.xdc",
    "post_synth_design_hook.tcl",
    "post_route_design_hook.tcl",
    "settings.json",
    "results.json",
]


def test_generated_files_replace_a_link_rather_than_write_through_it(tmp_path, monkeypatch):
    """A link at the name of a file xeda generates in its run directory -- a script, constraints,
    `settings.json`, `results.json` -- is replaced by the new file; the file it pointed to, the
    user's, is untouched."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(SQRT / "sqrt.toml")
    settings = {**XILINX_SETTINGS, "clean": False}  # the links must survive to the next run
    first = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=settings)
    assert first is not None and first.succeeded
    canaries = {}
    for name in GENERATED:
        assert (first.run_path / name).is_file(), name
        (first.run_path / name).unlink()
        canaries[name] = _linked_canary(tmp_path, first.run_path / name, name)

    again = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=settings)

    assert again is not None and again.succeeded
    for name, canary in canaries.items():
        assert canary.read_text() == PRECIOUS, name
        generated = again.run_path / name
        assert generated.is_file() and not generated.is_symlink(), name


def test_replacing_file_and_copy_replace_a_link(tmp_path):
    """`utils.replacing_file` and `utils.replacing_copy` write like `open(path, "w")` and
    `shutil.copy`, but replace a link at the destination instead of writing through it; the new
    file gets the permissions `open` would give it."""
    from xeda.utils import replacing_copy, replacing_file

    work = tmp_path / "run"
    work.mkdir()
    written = _linked_canary(tmp_path, work / "script.tcl", "a.txt")
    copied = _linked_canary(tmp_path, work / "copy.v", "b.txt")
    source = tmp_path / "source.v"
    source.write_text("module m; endmodule\n")

    with replacing_file(work / "script.tcl") as f:
        f.write("puts hello\n")
    replacing_copy(source, work / "copy.v")

    assert written.read_text() == PRECIOUS and copied.read_text() == PRECIOUS
    assert (work / "script.tcl").read_text() == "puts hello\n"
    assert (work / "copy.v").read_text() == source.read_text()
    assert not (work / "script.tcl").is_symlink() and not (work / "copy.v").is_symlink()
    umask = os.umask(0)
    os.umask(umask)
    assert (work / "script.tcl").stat().st_mode & 0o777 == 0o666 & ~umask
    assert sorted(p.name for p in work.iterdir()) == ["copy.v", "script.tcl"], "no temporary left"


def test_replacing_copy_leaves_the_destination_untouched_if_copymode_fails(tmp_path, monkeypatch):
    """If `shutil.copymode` fails, the destination must not already have been replaced by the
    new content with the wrong permission bits: `replacing_copy` sets the temporary file's mode
    before it is committed, so a failed `copymode` leaves the original destination -- content and
    mode -- exactly as it was, and no temporary file behind."""
    from xeda import utils

    source = tmp_path / "source.v"
    source.write_text("module m; endmodule\n")
    os.chmod(source, 0o600)

    target = tmp_path / "dest.v"
    target.write_text(PRECIOUS)
    os.chmod(target, 0o644)

    def _failing_copymode(_src: Any, _dst: Any, *, follow_symlinks: bool = True) -> None:
        raise OSError("copymode failed")

    monkeypatch.setattr(utils.shutil, "copymode", _failing_copymode)

    with pytest.raises(OSError):
        utils.replacing_copy(source, target)

    assert target.read_text() == PRECIOUS
    assert target.stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest.v", "source.v"], "no temporary left"


def test_replacing_file_never_commits_a_partly_written_file(tmp_path):
    """A body that writes part of the new content and fails leaves the file it would have
    replaced as it was, and no temporary file behind."""
    from xeda.utils import replacing_file

    target = tmp_path / "settings.json"
    target.write_text(PRECIOUS)

    with pytest.raises(RuntimeError):
        with replacing_file(target) as f:
            f.write("{ a prefix of the new")
            raise RuntimeError("failed half-way")

    assert target.read_text() == PRECIOUS
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


def test_replacing_file_keeps_the_original_error_if_the_diagnostic_cannot_be_saved(
    tmp_path, monkeypatch
):
    """`keep_on_error` commits the temporary as a best-effort diagnostic when the body raises;
    if that commit itself fails (its `os.replace`), the body's own exception must still be what
    propagates, not the secondary failure -- and the target is left untouched, with no temporary
    file behind."""
    from xeda import utils

    class ToolFailed(Exception):
        pass

    target = tmp_path / "tool.log"

    def _failing_replace(_src: Any, _dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(utils.os, "replace", _failing_replace)

    with pytest.raises(ToolFailed):
        with utils.replacing_file(target, keep_on_error=True) as f:
            f.write("why it failed\n")
            raise ToolFailed("the tool itself failed")

    assert not target.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [], "no temporary left"


def test_dump_json_never_commits_a_partly_serialized_document(tmp_path):
    """`settings.json` is written a second time, without a backup, after a run: a value that
    fails to serialize half-way leaves the first document whole."""
    from xeda.utils import dump_json

    target = tmp_path / "settings.json"
    dump_json({"first": 1}, target, backup=False)
    first = target.read_text()

    class Unserializable:
        def as_json_value(self):
            raise TypeError("cannot be written")

    with pytest.raises(TypeError):
        dump_json({"a": list(range(100)), "b": Unserializable()}, target, backup=False)

    assert target.read_text() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


def test_a_failed_tool_keeps_its_redirected_output(tmp_path):
    """A tool whose output a flow redirects to a file, and which fails, still leaves that
    output: it is the diagnostic of the failure."""
    from xeda.proc_utils import run_process
    from xeda.utils import NonZeroExitCode

    log = tmp_path / "tool.log"
    with pytest.raises(NonZeroExitCode):
        run_process("/bin/sh", ["-c", "echo why it failed; exit 3"], stdout=log)

    assert log.read_text() == "why it failed\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tool.log"]


# ---------------------------------------------------------------------------------------------
# The oracle: every place xeda deletes by name
# ---------------------------------------------------------------------------------------------

SRC_DIR = Path(__file__).parent.parent / "src" / "xeda"

#: Files that hold data, not code or tool scripts: PDK libraries, board and platform tables.
DATA_SUFFIXES = {".lef", ".lib", ".gz", ".gds", ".v", ".lyt", ".lyp", ".json", ".toml", ".md"}
DATA_SUFFIXES |= {".typed", ".tech", ".pyc"}
#: Module functions that delete, by module (`import os as o`, `from os import remove as r`
#: resolved).
MODULE_DELETIONS = {("os", f) for f in ("remove", "unlink", "rmdir", "removedirs")}
MODULE_DELETIONS |= {("shutil", "rmtree")}
#: Methods that delete, whatever the receiver: `Path.unlink`, `Path.rmdir`, xeda's own.
DELETING_METHODS = {"unlink", "rmdir", "rmtree", "purge_run_path"}
#: A tool's own subcommand that removes files (`ghdl remove`).
TOOL_REMOVAL = re.compile(r"^-{0,2}(remove|clean|rm|delete)$")
#: In a tool script: `rm`, Tcl's `file delete`.
SCRIPT_DELETION = re.compile(r"\brm\b|\bfile\s+delete\b")


def _imports(tree: ast.AST) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """The module aliases (`import os as o`: `o` -> `os`) and the imported names (`from os
    import remove as r`: `r` -> (`os`, `remove`)) of a module."""
    modules: dict[str, str] = {}
    names: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module.split(".")[0], alias.name)
    return modules, names


def python_sites(text: str) -> Iterator[str]:
    """The stripped source line of each place Python `text` deletes a file or directory, or
    makes a tool do so."""
    tree = ast.parse(text)
    lines = text.splitlines()
    modules, names = _imports(tree)
    for node in ast.walk(tree):
        deletes = False
        if isinstance(node, ast.Call):
            func = node.func
            first = node.args[0] if node.args else None
            if isinstance(func, ast.Name):
                module, name = names.get(func.id, ("", func.id))
                deletes = (module, name) in MODULE_DELETIONS or name == "rmtree"
            elif isinstance(func, ast.Attribute):
                receiver = func.value.id if isinstance(func.value, ast.Name) else ""
                module = modules.get(receiver, receiver)
                deletes = (module, func.attr) in MODULE_DELETIONS or func.attr in DELETING_METHODS
                deletes |= (
                    func.attr in ("run", "run_get_stdout")
                    and isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and bool(TOOL_REMOVAL.match(first.value))
                )
        elif isinstance(node, ast.Constant) and node.value == "rm":
            deletes = True  # a command line `["rm", ...]`
        if deletes:
            yield lines[node.lineno - 1].strip()


def script_sites(text: str) -> Iterator[str]:
    """Each line of a tool script (a template, a platform's Tcl) that deletes files."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and SCRIPT_DELETION.search(stripped):
            yield stripped


def deletion_sites() -> Counter:
    """Every deletion site in `src/xeda`, counted: `(file, line)` -> how many times it occurs."""
    found: Counter = Counter()
    for path in sorted(SRC_DIR.rglob("*")):
        if not path.is_file() or path.suffix in DATA_SUFFIXES or "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="replace")
        for line in python_sites(text) if path.suffix == ".py" else script_sites(text):
            found[(path.relative_to(SRC_DIR).as_posix(), line)] += 1
    return found


_PURGE = "empties the run directory through `Flow.purge_run_path`, which refuses one not marked"
_RMTREE = "the launcher's `rmtree` helper; its callers are the `rmtree(...)` sites"
_CHOSEN = (
    "a run directory xeda chose strictly inside the run root (`get_flow_run_path`) and claimed "
    "(`claim_run_dir`: made, empty, marked or an earlier run of the flow) before anything runs"
)
_INSIDE = "`Flow.removable_work_dir` keeps it inside the run directory, else refuses"

#: Every deletion site, why it deletes nothing of the user's: `(file, line, reason)`.
REVIEWED_SITES = [
    (
        "cocotb.py",
        "results_xml.unlink(missing_ok=True)",
        "an earlier cocotb results file, inside the run directory (`resolved_inside`; refused "
        "outside it)",
    ),
    ("flow/flow.py", "path.unlink()", _PURGE),
    ("flow/flow.py", "shutil.rmtree(path, ignore_errors=True)", _PURGE),
    (
        "flow/flow.py",
        "inside.unlink(missing_ok=True)",
        "`Flow.remove_stale_output`: an earlier copy of an output, only inside the run directory "
        "(one named outside it is left for the tool to overwrite)",
    ),
    ("flow_runner/default_runner.py", "os.remove(path)", _RMTREE),
    ("flow_runner/default_runner.py", "shutil.rmtree(path, onexc=on_rm_error)", _RMTREE),
    ("flow_runner/default_runner.py", "shutil.rmtree(path, onerror=on_rm_error)", _RMTREE),
    (
        "flow_runner/default_runner.py",
        "rmtree(p)",
        "`scrub_runs`: a flow's run directories inside a design's directory under the run root "
        "(the design name checked by `run_dir_name`), each marked or an earlier run of the flow "
        "(else refused), once confirmed",
    ),
    ("flow_runner/default_runner.py", "rmtree(run_path)", f"`--no-incremental`: {_CHOSEN}"),
    (
        "flow_runner/default_runner.py",
        "rmtree(flow.run_path)",
        f"`--post-cleanup-purge`: {_CHOSEN}",
    ),
    (
        "flow_runner/default_runner.py",
        "rmtree(path)",
        f"`--post-cleanup`: inside {_CHOSEN}, whose resolved path it checks",
    ),
    ("flow_runner/default_runner.py", "path.unlink()", "`--post-cleanup`: as `rmtree(path)`"),
    (
        "flow_runner/dse/dse_runner.py",
        "shutil.rmtree(p, ignore_errors=True)",
        f"a candidate's run directory that did not improve: {_CHOSEN}",
    ),
    (
        "flows/bsc/__init__.py",
        "path.unlink()",
        "an earlier run's packages (`.bo`/`.ba`) in `bobj_dir` and generated modules (a `.use` "
        f"and its `.v`) in the output directory: {_INSIDE}",
    ),
    ("flows/dc/__init__.py", "self.purge_run_path()", _PURGE),
    ("flows/vcs.py", "super().purge_run_path()", _PURGE),
    ("flows/vivado/__init__.py", "super().purge_run_path()", _PURGE),
    (
        "flows/diamond/templates/synth.tcl",
        "file delete -force -- $impl_dir",
        "`impl_folder`, checked by `DiamondSynth.run` (`Flow.removable_work_dir`) and rendered as "
        "exactly the path checked, one literal Tcl word",
    ),
    (
        "flows/ghdl/__init__.py",
        'self.ghdl.run("remove", *ss.get_flags(vhdl, "remove", backend=backend))',
        "`ghdl remove`: GHDL's own library files, in the run directory (no `--workdir`)",
    ),
    (
        "flows/openroad/templates/finalize.tcl",
        'file delete -- [file tail {{(design.rtl.top ~ ".totCap")|tcl_word}}]',
        "`<top>.totCap`, which OpenROAD wrote just before, in the run directory: the top as one "
        "literal word, and `file tail` keeps it there",
    ),
    (
        "flows/openxc7/__init__.py",
        "bba_path.unlink()",
        "the chip database's intermediate, which it wrote into the run directory just before",
    ),
    (
        "flow/flow.py",
        "named.unlink()",
        "`Flow.removable_work_dir`: the work directory's own name, a link a tool made, removed "
        "as a link (never what it leads to); its parent lies inside the run directory",
    ),
    ("flows/verilator/__init__.py", "p.unlink()", f"`*.d` in `sim_dir`: {_INSIDE}"),
    ("flows/verilator/__init__.py", "shutil.rmtree(sim_dir)", f"`sim_dir`: {_INSIDE}"),
    (
        "flows/vivado/templates/vivado_sim.tcl",
        "if { [catch {file delete -force xsim.dir} error]} {",
        f"`xsim.dir`, checked by `VivadoSim.run` before the script is rendered: {_INSIDE}",
    ),
    (
        "flows/yosys/common.py",
        "alias.unlink()",
        "a link of xeda's own under `path_aliases/` in the run directory",
    ),
    (
        "utils.py",
        "Path(temporary).unlink(missing_ok=True)  # the temporary file, never committed",
        "`replacing_file`: the temporary file it created, when the body failed or the rename did",
    ),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lef", "PDK script: its own"),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lib", "PDK script: its own"),
]


def test_every_deletion_by_name_is_reviewed():
    """A mechanical oracle: every place in `src/xeda` -- xeda's own code, the flows, their tool
    scripts, the platforms' Tcl -- that deletes a file or a directory by name, or makes a tool do
    so, is one reviewed in `REVIEWED_SITES`, as many times as it occurs. A new one fails here
    until it is shown to delete only inside a directory of xeda's, or only the run's own
    outputs."""
    found = deletion_sites()
    reviewed = Counter((file, line) for file, line, _ in REVIEWED_SITES)
    assert sorted((found - reviewed).elements()) == [], "not reviewed"
    assert sorted((reviewed - found).elements()) == [], "reviewed, but no longer there"


#: Module functions and methods that write, create, copy onto or rename onto a path.
MODULE_WRITES = {("shutil", f) for f in ("copy", "copy2", "copyfile", "copytree", "move")}
MODULE_WRITES |= {("os", f) for f in ("rename", "renames", "replace", "open", "symlink", "link")}
WRITING_METHODS = {"write_text", "write_bytes", "symlink_to", "hardlink_to", "touch", "rename"}
#: Modules whose `open(path, mode)` takes the mode second (`Path.open` takes it first).
OPENING_MODULES = {"gzip", "bz2", "lzma", "io", "codecs", "os"}


def _is_mode(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(node.value)
        and set(node.value) <= set("rwaxbt+")
    )


def _write_mode(node: ast.Call, position: int) -> bool:
    """Whether the mode of an `open` call writes -- or is not a constant, and so may. The mode
    is its `mode=`, else the argument at `position`, else any other argument that spells a mode
    (`sftp.open(path, "r")`)."""
    mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
    if mode is None and len(node.args) > position:
        mode = node.args[position]
    if mode is not None and not _is_mode(mode):
        mode = next((arg for arg in node.args if _is_mode(arg)), mode)
    if mode is None:
        return False
    choices = [mode.body, mode.orelse] if isinstance(mode, ast.IfExp) else [mode]
    return any(
        not _is_mode(choice) or bool(set(choice.value) & set("wax+"))  # type: ignore[attr-defined]
        for choice in choices
    )


def write_sites(text: str) -> Iterator[str]:
    """The stripped source line of each place Python `text` writes a file by name with a raw
    primitive -- `open` for writing, `write_text`, a copy, a rename, a link -- rather than
    through `utils.replacing_file`/`replacing_copy`, which never write through a link."""
    tree = ast.parse(text)
    lines = text.splitlines()
    modules, names = _imports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        writes = False
        if isinstance(func, ast.Name):
            module, name = names.get(func.id, ("", func.id))
            writes = (module, name) in MODULE_WRITES or (name == "open" and _write_mode(node, 1))
        elif isinstance(func, ast.Attribute):
            receiver = func.value.id if isinstance(func.value, ast.Name) else ""
            module = modules.get(receiver, receiver)
            if (module, func.attr) in MODULE_WRITES or func.attr in WRITING_METHODS:
                writes = True
            elif func.attr in ("open", "fdopen"):
                writes = _write_mode(node, 1 if module in OPENING_MODULES else 0)
            elif func.attr == "replace":  # `Path.replace(target)`, never `str.replace(a, b)`
                writes = (len(node.args) == 1 and not node.keywords) or any(
                    k.arg == "target" for k in node.keywords
                )
        if writes:
            yield lines[node.lineno - 1].strip()


_OUTSIDE_RUNS = "not a run directory's"

#: Every raw write by name, why it cannot write through a link into a file of the user's:
#: `(file, line, reason)`. Everything else xeda writes goes through `replacing_file`.
REVIEWED_WRITES = [
    ("utils.py", "os.replace(temporary, target)", "`replacing_file`: its complete temporary"),
    (
        "utils.py",
        "os.replace(temporary, target)",
        "`replacing_file(keep_on_error=True)`: a failed tool's redirected output, kept",
    ),
    (
        "utils.py",
        'with os.fdopen(fd, mode, encoding=None if "b" in mode else encoding) as f:',
        "`replacing_file`: the temporary file `mkstemp` just created",
    ),
    ("utils.py", "return path.rename(backup_path)", "`backup_existing`: a rename, not a write"),
    (
        "flow/run_dir.py",
        "fd = os.open(marker, flags, 0o666)",
        "the marker, created with O_CREAT | O_EXCL | O_NOFOLLOW: an existing entry is refused",
    ),
    ("flow/run_dir.py", 'with os.fdopen(fd, "w") as f:', "the marker it just created"),
    (
        "flow_runner/remote.py",
        'with open(design_file, "w") as f:',
        f"`send_design`: the archive's design file, in a `TemporaryDirectory` it made: {_OUTSIDE_RUNS}",
    ),
    (
        "flows/openfpgaloader.py",
        "packed.replace(bitstream)",
        "a rename of its own packed file onto the bitstream: replaces a link, never follows it",
    ),
    (
        "flows/yosys/common.py",
        "alias.symlink_to(target, target_is_directory=target.is_dir())",
        "creates a link of its own under `path_aliases/`, where no entry is left at the name",
    ),
    (
        "agent_skill.py",
        'flows_md.write_text(generate_flows_reference(), encoding="utf-8")',
        f"`xeda skill install` into the directory the user names: {_OUTSIDE_RUNS}",
    ),
    (
        "agent_skill.py",
        'shutil.copyfile(reference, target / "references" / reference.name)',
        f"`xeda skill install`: {_OUTSIDE_RUNS}",
    ),
    (
        "agent_skill.py",
        'shutil.copyfile(source / "SKILL.md", target / "SKILL.md")',
        f"`xeda skill install`: {_OUTSIDE_RUNS}",
    ),
    (
        "platforms/asap7/openroad/post_mergeLib.py",
        'fo = open(mergedFile, "w")',
        "a PDK script the tool runs, not xeda",
    ),
    (
        "platforms/mk_to_toml.py",
        'with open(Path(args.config_mk).parent / "config.toml", "w") as f:',
        f"a developer's script converting a PDK's config: {_OUTSIDE_RUNS}",
    ),
]


def test_every_raw_write_by_name_is_reviewed():
    """A mechanical oracle: every place in `src/xeda` that writes a file by name with a raw
    primitive, rather than through `replacing_file`/`replacing_copy` (which replace a link at the
    name instead of writing through it), is one reviewed in `REVIEWED_WRITES`, as many times as it
    occurs."""
    found: Counter = Counter()
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" not in path.parts:
            for line in write_sites(path.read_text()):
                found[(path.relative_to(SRC_DIR).as_posix(), line)] += 1
    reviewed = Counter((file, line) for file, line, _ in REVIEWED_WRITES)
    assert sorted((found - reviewed).elements()) == [], "not reviewed"
    assert sorted((reviewed - found).elements()) == [], "reviewed, but no longer there"


WRITE_MUTATIONS = [
    'def f(p):\n    open(p, "w")\n',
    'def f(p):\n    open(p, mode="a")\n',
    "def f(p, m):\n    open(p, m)\n",
    'def f(p):\n    p.open("w")\n',
    'def f(p):\n    p.write_text("x")\n',
    "import shutil\n\ndef f(a, b):\n    shutil.copy(a, b)\n",
    "from shutil import copyfile as c\n\ndef f(a, b):\n    c(a, b)\n",
    "import os as o\n\ndef f(a, b):\n    o.replace(a, b)\n",
    "def f(p, t):\n    p.replace(t)\n",
]


@pytest.mark.parametrize("added", WRITE_MUTATIONS)
def test_the_write_oracle_notices_every_spelling_of_a_write(added):
    """Each raw write appended to a scratch copy of a flow's text is a site the oracle notices;
    a read and `str.replace` are not."""
    text = (SRC_DIR / "flows" / "ghdl" / "__init__.py").read_text()
    assert Counter(write_sites(text + "\n" + added)) - Counter(write_sites(text)), added
    assert not list(write_sites('open("a")\nopen("a", "rb")\n"x".replace("x", "y")\n'))


#: Deletions the oracle must notice, however they are spelled: `(file, the text appended)`.
MUTATIONS = [
    ("flows/verilator/__init__.py", "def f(p):\n    p.unlink()\n"),  # a reviewed line, again
    ("flows/dc/__init__.py", "def f(self):\n    self.purge_run_path()\n"),
    ("flows/ghdl/__init__.py", "import os as fs\n\ndef f(p):\n    fs.remove(p)\n"),
    ("flows/ghdl/__init__.py", "from os import remove\n\ndef f(p):\n    remove(p)\n"),
    ("flows/ghdl/__init__.py", "from shutil import rmtree as r\n\ndef f(p):\n    r(p)\n"),
    ("flows/ghdl/__init__.py", "def f(p):\n    p.rmdir()\n"),
    ("flows/ghdl/__init__.py", 'def f(self):\n    self.ghdl.run("--remove")\n'),
    ("flows/ghdl/__init__.py", 'def f(run):\n    run(["rm", "-rf", "build"])\n'),
    ("flows/vivado/templates/vivado_sim.tcl", "exec rm -rf xsim.dir\n"),
    ("flows/vivado/templates/vivado_sim.tcl", "file delete -force build\n"),
]


@pytest.mark.parametrize("file, added", MUTATIONS, ids=[f for f, _ in MUTATIONS])
def test_the_oracle_notices_every_spelling_of_a_deletion(file, added):
    """Each deletion appended to a scratch copy of a file's text is a site the oracle has not
    seen reviewed -- occurrences are counted, not deduplicated."""
    path = SRC_DIR / file
    text = path.read_text()
    sites = python_sites if path.suffix == ".py" else script_sites
    assert Counter(sites(text + "\n" + added)) - Counter(sites(text)), f"{added!r} went unnoticed"
