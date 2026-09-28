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
from typing import Any, Iterator

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim, DiamondSynth, Verilator, VivadoSim, VivadoSynth
from xeda.utils import XedaException

from .settings_samples import flow_classes, minimal_settings
from .tool_utils import FAKE_TOOLS_DIR, fake_calls, use_fake_tools

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


def test_clean_empties_a_run_directory_xeda_chose(tmp_path, monkeypatch):
    """A directory under the run root is xeda's without a marker: `--clean` empties it, as
    before, and the run goes ahead."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path, cleanup_before_run=True)
    run_dir = launcher.get_flow_run_path("sqrt", "vivado_synth")
    run_dir.mkdir(parents=True)
    (run_dir / "stale.txt").write_text("an earlier run's\n")

    flow = launcher.run(
        "vivado_synth", design=Design.from_file(SQRT / "sqrt.toml"), flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded
    assert flow.run_path == run_dir
    assert not (run_dir / "stale.txt").exists()
    assert not (run_dir / MARKER).exists(), "a directory xeda chose needs no marker"


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


def test_a_vivado_xsim_dir_linked_out_of_the_run_directory_is_refused(tmp_path, monkeypatch):
    """The Vivado simulation script deletes `xsim.dir` in the run directory: one that resolves
    elsewhere (a link) is refused, naming it, before the script runs."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, launcher, "vivado_sim", "xsim")
    before = _tree(outside)
    run_dir = launcher.get_flow_run_path("sqrt", "vivado_sim")
    run_dir.mkdir(parents=True)
    (run_dir / "xsim.dir").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "tb.v"
    source.write_text("module tb; endmodule\n")
    design = Design(
        name="sqrt",
        rtl={"sources": [str(source)], "top": "tb"},
        tb={"top": "tb"},
        design_root=tmp_path,
    )

    with pytest.raises(XedaException) as refused:
        launcher.launch_flow(VivadoSim, design, {})

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert "xsim.dir" in str(refused.value)
    assert _tree(outside) == before
    assert not fake_calls(run_dir)


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


_PURGE = "empties the run directory through `Flow.purge_run_path`, which refuses one not xeda's"
_RMTREE = "the launcher's `rmtree` helper; its callers are the `rmtree(...)` sites"
_CHOSEN = "a run directory xeda chose, strictly inside the run root (`get_flow_run_path`)"
_OUTPUT = "a stale output of the run's own, by the name its setting gives; the run writes it again"

#: Every deletion site, why it deletes nothing of the user's: `(file, line, reason)`.
REVIEWED_SITES = [
    ("cocotb.py", "Path(self.results_xml).unlink(missing_ok=True)", _OUTPUT),
    ("flow/flow.py", "path.unlink()", _PURGE),
    ("flow/flow.py", "shutil.rmtree(path, ignore_errors=True)", _PURGE),
    ("flow_runner/default_runner.py", "os.remove(path)", _RMTREE),
    ("flow_runner/default_runner.py", "shutil.rmtree(path, onexc=on_rm_error)", _RMTREE),
    ("flow_runner/default_runner.py", "shutil.rmtree(path, onerror=on_rm_error)", _RMTREE),
    (
        "flow_runner/default_runner.py",
        "rmtree(p)",
        "`scrub_runs`: a flow's run directories inside a design's directory under the run root "
        "(the design name checked by `run_dir_name`), each resolving inside it, once confirmed",
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
        f"a candidate's run directory that did not improve: {_CHOSEN} of the exploration",
    ),
    (
        "flows/bsc/__init__.py",
        "path.unlink()",
        "an earlier run's packages (`.bo`/`.ba`) in `bobj_dir` and generated modules (a `.use` "
        "and its `.v`) in the output directory, both of which `Flow.removable_work_dir` keeps "
        "inside the run directory",
    ),
    ("flows/bsc/__init__.py", "old.unlink(missing_ok=True)", f"`bsc_sim`'s `vcd`: {_OUTPUT}"),
    ("flows/dc/__init__.py", "self.purge_run_path()", _PURGE),
    ("flows/vcs.py", "super().purge_run_path()", _PURGE),
    ("flows/vivado/__init__.py", "super().purge_run_path()", _PURGE),
    (
        "flows/diamond/templates/synth.tcl",
        "file delete -force ${impl_dir}",
        "`impl_folder`, which `DiamondSynth.run` keeps inside the run directory "
        "(`Flow.removable_work_dir`) before the script runs",
    ),
    ("flows/ghdl/__init__.py", "p.unlink()", f"`write_wave_opt`: {_OUTPUT}"),
    (
        "flows/ghdl/__init__.py",
        'self.ghdl.run("remove", *ss.get_flags(vhdl, "remove", backend=backend))',
        "`ghdl remove`: GHDL's own library files, which only GHDL writes",
    ),
    ("flows/ise/__init__.py", "path.unlink(missing_ok=True)", "ISE's own outputs: `outputs()`"),
    (
        "flows/openroad/templates/finalize.tcl",
        "file delete {{design.rtl.top}}.totCap",
        "a fixed name in the run directory, which OpenROAD wrote just before",
    ),
    ("flows/openxc7/__init__.py", "bin_path.unlink()", "a chip database it regenerates, forced"),
    ("flows/openxc7/__init__.py", "bba_path.unlink()", "the intermediate it wrote just before"),
    (
        "flows/verilator/__init__.py",
        "p.unlink()",
        "`*.d` in `sim_dir`, which `Flow.removable_work_dir` keeps inside the run directory",
    ),
    (
        "flows/verilator/__init__.py",
        "shutil.rmtree(sim_dir)",
        "`sim_dir`, which `Flow.removable_work_dir` keeps inside the run directory",
    ),
    (
        "flows/vivado/templates/vivado_sim.tcl",
        "file delete -force -- {{settings.saif}}",
        f"`saif`: {_OUTPUT}",
    ),
    (
        "flows/vivado/templates/vivado_sim.tcl",
        "if { [catch {file delete -force xsim.dir} error]} {",
        "`xsim.dir`, which `VivadoSim.run` keeps inside the run directory before the script runs",
    ),
    (
        "flows/vivado/vivado_synth.py",
        "(self.run_path / path).unlink(missing_ok=True)",
        f"the outputs the run registers: {_OUTPUT}",
    ),
    (
        "flows/yosys/common.py",
        "alias.unlink()",
        "a link of xeda's own under `path_aliases/` in the run directory",
    ),
    (
        "flows/yosys/yosys.py",
        "os.remove(self.artifacts.timing_report)",
        "a report of the run's own",
    ),
    (
        "flows/yosys/yosys.py",
        "os.remove(self.artifacts.utilization_report)",
        "a report of the run's own",
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
