"""xeda writes and deletes only what is its own: the user's files survive a launch.

`xeda run vivado_synth sqrt.yaml --cwd`, started in the design's own directory, once deleted every
file there, and a design named `..` put its run directory outside the run root, where a clean
emptied a directory of the user's. Every run directory now lies under a run root that xeda created
and marked (`.xeda-run-root`), and this file proves the parts of that which no other suite owns:

* a directory named as the run root that holds files and no marker is refused before anything is
  written (`RunRootError`), from the API and the command line, `xeda scrub` included; the default
  `./xeda_run` an earlier xeda made is adopted;
* a design name that would put a run directory elsewhere is refused, and so is a run directory
  (a design's or a dependency's) that is a link leading out of the run root; one that is a link
  resolving inside the run root is used, and scrubbed as what it leads to;
* `clean` empties xeda's run directory, and a flow built without a launcher deletes nothing;
* a working setting given as a location (`../x`, an absolute path) is refused at launch before any
  tool runs, one whose name Tcl would substitute is removed literally, a work directory a tool
  left as a link is removed as a link, and one behind a link out of the run directory is refused;
* xeda never writes through a link: generated files replace it, and the run-root marker is created
  exclusively;
* the write primitives (`replacing_file`, `replacing_copy`, `dump_json`) never commit a partly
  written file, and a failed tool keeps its redirected output.

That a launch never changes a file outside the run root is `test_isolation.py`; that a delivery
never replaces a file unconfirmed is `test_delivery.py`; `RunDirectory`'s containment is
`test_run_directory.py`.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from xeda import Design
from xeda.console import console
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsError, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import scrub_runs
from xeda.flows import Bsc, BscSim, DiamondSynth, Verilator, VivadoSim, VivadoSynth
from xeda.run_dir import RunDirectoryError
from xeda.run_root import RUN_ROOT_MARKER, RunRootError, ensure_run_root
from xeda.utils import XedaException

from .tool_utils import FAKE_TOOLS_DIR, fake_calls, producers_of, use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"

XILINX = ["-s", "fpga.part=xc7a12tcsg325-1", "clock.period=10"]
XILINX_SETTINGS = {"fpga": {"part": "xc7a12tcsg325-1"}, "clock": {"period": 10.0}}
PRECIOUS = "the user's own file, at the path they named\n"


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


def _launcher(tmp_path: Path, **settings: Any) -> DefaultRunner:
    """A launcher as `xeda run` makes one: run directories `<run root>/<design>/<flow>`."""
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)


def _sqrt(tmp_path: Path) -> Path:
    """A copy of the sqrt design in `tmp_path/sqrt`: its design file."""
    design_dir = tmp_path / "sqrt"
    shutil.copytree(SQRT, design_dir, ignore=shutil.ignore_patterns("__pycache__"))
    return design_dir / "sqrt.yaml"


@pytest.fixture
def toy_flows():
    """A flow with one producer, each writing a file into its run directory. Registered while
    the test runs only, so the sweeps over every flow never see them."""

    class ToyDep(Flow):
        """A producer that writes one file."""

        results_description: ClassVar[dict[str, str]] = {}

        class Outputs(Flow.Outputs):
            dep: Path = Out(SourceType.Data, description="The file it writes.")

        def run(self) -> None:
            (self.run_path / "dep.txt").write_text("dep\n")
            self.outputs.dep = self.run_path / "dep.txt"

    class ToyTop(Flow):
        """A flow with one producer."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            dep: Path = In(
                SourceType.Data,
                producer="toy_dep",
                output="dep",
                description="The producer's file.",
            )

        def run(self) -> None:
            (self.run_path / "top.txt").write_text("top\n")

    yield ToyDep, ToyTop
    for cls in (ToyDep, ToyTop):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


# ---------------------------------------------------------------------------------------------
# A run root that holds the user's files is not xeda's
# ---------------------------------------------------------------------------------------------


def _users_run_root(tmp_path: Path) -> tuple[Path, Path]:
    """The sqrt design's file, and a directory of the user's, `myrundir`, that holds a file of
    theirs where the run directory xeda derives there would be:
    `myrundir/sqrt/vivado_synth/my_data.txt`, and no marker."""
    design_file = _sqrt(tmp_path)
    canary = tmp_path / "myrundir" / "sqrt" / "vivado_synth" / "my_data.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("the user's own data\n")
    return design_file, canary


@pytest.mark.parametrize("option", [[], ["--rebuild-all"], ["--clean"]])
def test_a_users_directory_as_the_run_root_is_refused(tmp_path, option):
    """`--run-root myrundir`, where the user keeps `myrundir/sqrt/vivado_synth/my_data.txt`: a
    clean once deleted it. A run root holding files and no marker is not xeda's: it is refused,
    naming it and the marker, before anything is written -- the user's tree exactly as it was."""
    design_file, canary = _users_run_root(tmp_path)
    before = _tree(tmp_path / "myrundir")

    run = _xeda(
        "run", "vivado_synth", str(design_file), "--run-root", "myrundir", "--json", *option,
        *XILINX, cwd=tmp_path,
    )  # fmt: skip

    assert _tree(tmp_path / "myrundir") == before, run.stdout + run.stderr
    error = _error(run)
    assert error["type"] == "RunRootError", error
    assert str(tmp_path / "myrundir") in error["message"], error
    assert RUN_ROOT_MARKER in error["message"], error
    assert canary.read_text() == "the user's own data\n"


@pytest.mark.parametrize(
    "record",
    [
        {"flow_name": "vivado_synth", "flow_settings": {}, "xeda_version": "0.4.2"},
        {"flow_name": "yosys_fpga", "flow_settings": {}, "xeda_version": "0.4.2"},
        ["vivado_synth"],
        "not JSON",
    ],
    ids=["an earlier run's", "another flow's", "a list", "not JSON"],
)
def test_a_run_root_holding_files_is_refused_whatever_they_say(tmp_path, monkeypatch, record):
    """What the files in an unmarked directory say does not make it xeda's -- not even a
    `settings.json` that looks like xeda's own record of this very flow: the API refuses the run
    root when the launcher is made, before any flow is launched, and nothing there is written."""
    use_fake_tools(monkeypatch)
    root = tmp_path / "myrundir"
    run_dir = root / "sqrt" / "vivado_synth"
    run_dir.mkdir(parents=True)
    (run_dir / "settings.json").write_text(
        record if isinstance(record, str) else json.dumps(record)
    )
    before = _tree(root)

    with pytest.raises(RunRootError) as refused:
        DefaultRunner(root, display_results=False).run(
            "vivado_synth",
            design=Design.from_file(SQRT / "sqrt.yaml"),
            flow_settings=XILINX_SETTINGS,
        )

    assert str(root) in str(refused.value) and RUN_ROOT_MARKER in str(refused.value)
    assert _tree(root) == before
    assert not fake_calls(run_dir)


@pytest.mark.parametrize("option", [{}, {"clean": True}], ids=["default", "clean"])
def test_an_earlier_xeda_runs_directory_is_adopted(tmp_path, monkeypatch, option):
    """An existing `./xeda_run` tree keeps working: the default run root, directly in the start
    directory, holds an earlier xeda's runs and no marker; it is adopted -- marked -- and the run
    goes ahead there, `clean` emptying the flow's run directory as before."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = Design.from_file(SQRT / "sqrt.yaml")
    first = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=XILINX_SETTINGS)
    assert first is not None and first.succeeded
    root = tmp_path / "xeda_run"
    for name in (RUN_ROOT_MARKER, ".gitignore", "CACHEDIR.TAG"):  # as 0.4.2 left it
        (root / name).unlink()
    (first.run_path / "stale.txt").write_text("an earlier run's\n")

    flow = DefaultRunner(root, display_results=False, **option).run(
        "vivado_synth", design=design, flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded and flow.run_path == first.run_path
    assert (root / RUN_ROOT_MARKER).is_file()
    assert (first.run_path / "stale.txt").exists() != bool(option), "only a clean emptied it"


def test_xeda_scrub_refuses_a_users_directory(tmp_path):
    """`xeda scrub vivado_synth sqrt --run-root myrundir` against a directory of the user's
    refuses it with a JSON failure document and a nonzero exit, naming it -- nothing removed."""
    _, canary = _users_run_root(tmp_path)
    before = _tree(tmp_path / "myrundir")

    run = _xeda(
        "scrub", "vivado_synth", "sqrt", "--run-root", "myrundir", "--json", cwd=tmp_path
    )  # fmt: skip

    assert run.returncode != 0, run.stdout + run.stderr
    error = _error(run)
    assert error["type"] == "RunRootError", error
    assert str(tmp_path / "myrundir") in error["message"], error
    assert _tree(tmp_path / "myrundir") == before and canary.is_file()


def test_the_run_root_marker_is_never_written_through_a_link(tmp_path):
    """The marker is created exclusively: a directory holding only a link named `.xeda-run-root`
    -- to a file of the user's -- is not a run root, so it is refused rather than marked, and the
    link's target is unchanged."""
    root = tmp_path / "runs"
    root.mkdir()
    canary = tmp_path / "users" / "notes.txt"
    canary.parent.mkdir()
    canary.write_text(PRECIOUS)
    (root / RUN_ROOT_MARKER).symlink_to(canary)

    with pytest.raises(RunRootError):
        ensure_run_root(root, start=tmp_path)

    assert canary.read_text() == PRECIOUS
    assert (root / RUN_ROOT_MARKER).is_symlink()
    assert not (root / ".gitignore").exists()


# ---------------------------------------------------------------------------------------------
# A directory xeda chooses lies inside the run root
# ---------------------------------------------------------------------------------------------


def _dotdot_design(start: Path) -> Path:
    """A design named `..`, whose run directory `<start>/xeda_run/../<flow>` was `<start>/<flow>`."""
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


@pytest.mark.parametrize("option", ["--clean", "--rebuild-all"])
def test_a_design_named_dotdot_is_refused_and_the_users_directory_survives(tmp_path, option):
    """A design named `..` would run in `<start>/vivado_synth`, a directory of the user's, which
    a clean emptied. The design is refused, naming its name, and the user's directory is
    untouched."""
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
    assert "design name" in error["message"], error


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
    assert "'..' is not a design name" in document["error"]["message"], document


ESCAPING_NAMES = {
    "..": "..",
    ".": ".",
    "a separator": "a/b",
    "a backslash": "a\\b",
    "an absolute path": "/tmp/elsewhere",
    "nothing once sanitized": "...",
}


@pytest.mark.parametrize("name", ESCAPING_NAMES.values(), ids=list(ESCAPING_NAMES))
def test_a_name_that_would_leave_the_run_root_is_refused(tmp_path, name):
    """A design name that does not name one directory inside the run root is refused, naming it,
    both when the design is made and when a launcher is asked for its run directory. (A flow's
    name comes from the registry, so there is no flow half to refuse.)"""
    from xeda.design import DesignValidationError

    with pytest.raises((ValueError, DesignValidationError), match="design name"):
        Design(name=name, design_root=tmp_path, rtl={"sources": []})
    with pytest.raises(RunDirectoryError) as refused:
        _launcher(tmp_path).get_flow_run_path(name, "vivado_synth")

    assert repr(name) in str(refused.value)
    assert not (tmp_path / "elsewhere").exists()


# ---------------------------------------------------------------------------------------------
# A run directory that is a link
# ---------------------------------------------------------------------------------------------


def _elsewhere(tmp_path: Path) -> Path:
    """A directory of the user's outside any run root, holding a file of theirs."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text(PRECIOUS)
    return elsewhere


@pytest.mark.parametrize("level", ["design", "flow"])
def test_a_run_directory_linked_out_of_the_run_root_is_refused(tmp_path, monkeypatch, level):
    """A run directory must resolve inside the run root: one reached through a link (at the
    design's directory, or at the flow's own) to a directory elsewhere is refused, before
    anything -- the lock beside it included -- is written there, even with `clean`."""
    use_fake_tools(monkeypatch)
    elsewhere = _elsewhere(tmp_path)
    before = _tree(elsewhere)
    launcher = _launcher(tmp_path, clean=True)
    link = launcher.run_root / "sqrt"
    if level == "flow":
        link.mkdir()
        link = link / "vivado_synth"
    link.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RunDirectoryError, match="leads out of the run root"):
        launcher.get_flow_run_path("sqrt", "vivado_synth")
    with pytest.raises(XedaException) as refused:
        launcher.run(
            "vivado_synth",
            design=Design.from_file(SQRT / "sqrt.yaml"),
            flow_settings=XILINX_SETTINGS,
        )

    assert type(refused.value).__name__ == "RunDirectoryError"
    assert _tree(elsewhere) == before
    assert not fake_calls(elsewhere)


def test_a_run_directory_inside_the_run_root_is_the_usual_one(tmp_path):
    """The names of ordinary designs and flows map to `<run root>/<design>/<flow>`; a link there
    that resolves inside the run root is not refused."""
    launcher = _launcher(tmp_path)
    root = launcher.run_root
    assert launcher.get_flow_run_path("sqrt", "vivado_synth") == root / "sqrt" / "vivado_synth"
    assert launcher.get_flow_run_path("my-design", "ghdl_sim") == root / "my-design" / "ghdl_sim"

    target = root / "sqrt" / "elsewhere_in_the_root"
    target.mkdir(parents=True)
    (root / "sqrt" / "vivado_synth").symlink_to(target, target_is_directory=True)

    assert launcher.get_flow_run_path("sqrt", "vivado_synth") == root / "sqrt" / "vivado_synth"


def test_a_run_directory_that_is_a_link_inside_the_run_root_is_used(tmp_path, monkeypatch):
    """A run directory that is a link resolving inside the run root is accepted: the run's files
    land where it leads."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    root = launcher.run_root
    target = root / "sqrt" / "elsewhere_in_the_root"
    target.mkdir(parents=True)
    (root / "sqrt" / "vivado_synth").symlink_to(target, target_is_directory=True)

    flow = launcher.run(
        "vivado_synth", design=Design.from_file(SQRT / "sqrt.yaml"), flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded
    assert (target / "results.json").is_file() and fake_calls(target)


def test_a_dependency_directory_linked_out_of_the_run_root_is_refused(tmp_path, toy_flows):
    """A dependency runs in a sibling of its depender's directory, `<run root>/<design>/<flow>`:
    a link there to a directory elsewhere is refused before it is cleaned or written, and what
    the link leads to is untouched."""
    dep, top = toy_flows
    design = Design.from_file(SQRT / "sqrt.yaml")
    first = _launcher(tmp_path).launch_flow(top, design, {})
    dep_dir = first.run_path.parent / dep.name
    assert dep_dir.is_dir() and dep_dir != first.run_path
    elsewhere = _elsewhere(tmp_path)
    shutil.rmtree(dep_dir)
    dep_dir.symlink_to(elsewhere, target_is_directory=True)
    before = _tree(elsewhere)

    with pytest.raises(RunDirectoryError) as refused:
        _launcher(tmp_path, clean=True).launch_flow(top, design, {})

    assert "leads out of the run root" in str(refused.value)
    assert _tree(elsewhere) == before
    assert dep_dir.is_symlink()


def test_scrub_never_removes_what_a_link_out_of_the_run_root_leads_to(tmp_path, monkeypatch):
    """Scrubbing a flow's run directories skips one that is a link leading out of the run root,
    without asking: it would remove, through the link, whatever it leads to."""
    run_root = ensure_run_root(tmp_path / "xeda_run")
    design_dir = run_root / "sqrt"
    design_dir.mkdir()
    elsewhere = _elsewhere(tmp_path)
    link = design_dir / "vivado_synth"
    link.symlink_to(elsewhere, target_is_directory=True)
    before = _tree(elsewhere)
    asked = []
    monkeypatch.setattr(console, "input", lambda prompt: asked.append(prompt) or "yes")

    assert not scrub_runs("vivado_synth", design_dir, run_root=run_root)

    assert not asked
    assert _tree(elsewhere) == before and link.is_symlink()


def test_scrub_removes_a_link_into_the_run_root_and_what_it_leads_to(tmp_path, monkeypatch):
    """A run directory that is a link to another directory of the run root is xeda's: scrubbing
    it removes the directory it leads to and the link."""
    run_root = ensure_run_root(tmp_path / "xeda_run")
    design_dir = run_root / "sqrt"
    target = design_dir / "other"
    target.mkdir(parents=True)
    (target / "results.json").write_text("{}\n")
    link = design_dir / "vivado_synth"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(console, "input", lambda prompt: "yes")

    assert scrub_runs("vivado_synth", design_dir, run_root=run_root)

    assert not target.exists() and not os.path.lexists(link)
    assert design_dir.is_dir()


# ---------------------------------------------------------------------------------------------
# Emptying a run directory
# ---------------------------------------------------------------------------------------------


def _vivado_synth(run_dir: Path) -> VivadoSynth:
    """A flow built directly -- no launcher -- to run in `run_dir`."""
    return VivadoSynth(
        VivadoSynth.Settings(**XILINX_SETTINGS), Design.from_file(SQRT / "sqrt.yaml"), run_dir
    )


@pytest.mark.parametrize("what", ["a directory of the user's", "a link to one"])
def test_purge_run_path_deletes_nothing_in_a_flow_built_without_a_launcher(tmp_path, what):
    """A flow's `purge_run_path` empties a run directory only when a launcher chose it: for one
    built directly -- on a directory of the user's, or on a link to one -- it logs and deletes
    nothing."""
    users = tmp_path / "sqrt"
    shutil.copytree(SQRT, users, ignore=shutil.ignore_patterns("__pycache__"))
    (users / "mine").mkdir()
    (users / "mine" / "data.txt").write_text("the user's own data\n")
    run_dir = users
    if what == "a link to one":
        run_dir = tmp_path / "run"
        run_dir.symlink_to(users, target_is_directory=True)
    before = _tree(users)

    _vivado_synth(run_dir).purge_run_path()

    assert _tree(users) == before


def test_clean_empties_a_run_directory_xeda_made(tmp_path, monkeypatch):
    """`clean` empties a run directory xeda made -- whatever an earlier run left in it, nested
    directories included -- before the next run; the run goes ahead and the run root stays
    marked."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(SQRT / "sqrt.yaml")
    first = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=XILINX_SETTINGS)
    assert first is not None and first.succeeded
    run_dir = first.run_path
    (run_dir / "reports").mkdir(exist_ok=True)
    (run_dir / "reports" / "old.rpt").write_text("an earlier run's\n")
    (run_dir / "stale.txt").write_text("an earlier run's\n")
    assert (tmp_path / "xeda_run" / RUN_ROOT_MARKER).is_file()

    flow = _launcher(tmp_path, clean=True).run(
        "vivado_synth", design=design, flow_settings=XILINX_SETTINGS
    )

    assert flow is not None and flow.succeeded and not flow.reused
    assert flow.run_path == run_dir
    assert not (run_dir / "stale.txt").exists()
    assert not (run_dir / "reports" / "old.rpt").exists()
    assert (tmp_path / "xeda_run" / RUN_ROOT_MARKER).is_file()


def test_every_run_directory_xeda_makes_lies_under_a_marked_run_root(tmp_path, toy_flows):
    """A flow's run directory and its dependency's (its sibling) are made under the run root,
    which is created and marked, with its ignore files, by the first launch."""
    dep, top = toy_flows
    launcher = _launcher(tmp_path)
    root = tmp_path / "xeda_run"
    assert not root.exists(), "the run root is made when first used"

    flow = launcher.launch_flow(top, Design.from_file(SQRT / "sqrt.yaml"), {})

    assert flow.succeeded
    for name in (RUN_ROOT_MARKER, ".gitignore", "CACHEDIR.TAG"):
        assert (root / name).is_file(), name
    (done,) = producers_of(launcher, flow)
    assert flow.run_path == root.resolve() / "sqrt" / top.name
    assert done.run_path == root.resolve() / "sqrt" / dep.name
    for directory in (flow.run_path, done.run_path):
        assert directory.is_dir() and not (directory / RUN_ROOT_MARKER).exists()


# ---------------------------------------------------------------------------------------------
# Working locations a flow removes files from by name
# ---------------------------------------------------------------------------------------------


def _outside(tmp_path: Path, name: str) -> Path:
    """A directory of the user's outside any run root, holding a file of every kind a work
    directory holds."""
    outside = tmp_path / name
    outside.mkdir(parents=True)
    for file in ("keep.txt", "mine.d", "Mine.bo", "Mine.ba", "mkMine.use", "mkMine.v"):
        (outside / file).write_text("the user's own file\n")
    return outside


def _verilator_design(tmp_path: Path) -> Design:
    source = tmp_path / "top.v"
    source.write_text("module top; endmodule\n")
    return Design(name="sqrt", rtl={"sources": [str(source)], "top": "top"}, design_root=tmp_path)


@pytest.mark.parametrize("where", ["../x", "absolute"])
def test_a_verilator_sim_dir_outside_the_run_directory_is_refused(tmp_path, monkeypatch, where):
    """Verilator removes `*.d` files from its `sim_dir`: a `sim_dir` given as a location -- `../x`
    or an absolute path -- is refused at launch, naming the setting, before any tool runs, and
    the directory there is untouched."""
    _no_tools(monkeypatch)
    outside = _outside(tmp_path, "x")
    before = _tree(outside)
    sim_dir = "../x" if where == "../x" else str(outside)

    with pytest.raises(FlowSettingsError) as refused:
        _launcher(tmp_path).launch_flow(
            Verilator, _verilator_design(tmp_path), {"sim_dir": sim_dir}
        )

    assert "sim_dir" in str(refused.value)
    assert _tree(outside) == before


@pytest.mark.parametrize(
    "flow_class, setting",
    [(Bsc, "bobj_dir"), (Bsc, "verilog_out_dir"), (BscSim, "bobj_dir"), (BscSim, "sim_dir")],
    ids=["bsc-bobj_dir", "bsc-verilog_out_dir", "bsc_sim-bobj_dir", "bsc_sim-sim_dir"],
)
def test_a_bsc_work_directory_outside_the_run_directory_is_refused(
    tmp_path, monkeypatch, flow_class, setting
):
    """bsc removes an earlier run's packages (`.bo`/`.ba`) from `bobj_dir` and its generated
    modules (a `.use` and the `.v` beside it) from the output directory: either given as a
    location is refused at launch, naming the setting, with the directory there untouched."""
    _no_tools(monkeypatch)
    outside = _outside(tmp_path, "bsc_work")
    before = _tree(outside)
    source = tmp_path / "Top.bsv"
    source.write_text("package Top;\nendpackage\n")
    design = Design(
        name="sqrt",
        rtl={"sources": [str(source)], "top": "mkTop"},
        tb={"sources": [str(source)], "top": "mkTop"},
        design_root=tmp_path,
    )

    with pytest.raises(FlowSettingsError) as refused:
        _launcher(tmp_path).launch_flow(flow_class, design, {setting: "../bsc_work"})

    assert setting in str(refused.value)
    assert _tree(outside) == before


@pytest.mark.parametrize("where", ["../impl", "absolute"])
def test_a_diamond_impl_folder_outside_the_run_directory_is_refused(tmp_path, monkeypatch, where):
    """Diamond's script deletes its `impl_folder` before creating the project: an `impl_folder`
    given as a location is refused at launch, naming the setting, before the script runs."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, "impl")
    before = _tree(outside)
    settings = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": 10.0}}

    with pytest.raises(FlowSettingsError) as refused:
        launcher.launch_flow(
            DiamondSynth,
            Design.from_file(SQRT / "sqrt.yaml"),
            settings | {"impl_folder": "../impl" if where == "../impl" else str(outside)},
        )

    assert "impl_folder" in str(refused.value)
    assert _tree(outside) == before
    assert not list((launcher.run_root).rglob("fake_*.calls")), "no tool ran"


@pytest.mark.parametrize(
    "impl_folder",
    ["impl[pwd]", "impl[set x 1]", "my impl", "{brace}"],
    ids=["command", "command with a space", "space", "braces"],
)
def test_a_diamond_impl_folder_is_deleted_exactly_as_checked(tmp_path, monkeypatch, impl_folder):
    """An `impl_folder` that Tcl would substitute (command brackets) or split (a space) names a
    directory of that literal name inside the run directory: it is the one removed, and the tool
    is handed exactly `<run directory>/<name>` as one word."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    run_dir = launcher.get_flow_run_path("sqrt", "diamond_synth")
    (run_dir / impl_folder).mkdir(parents=True)
    (run_dir / impl_folder / "old.txt").write_text("an earlier run's\n")
    settings = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": 10.0}}

    try:
        launcher.launch_flow(
            DiamondSynth,
            Design.from_file(SQRT / "sqrt.yaml"),
            settings | {"impl_folder": impl_folder},
        )
    except Exception:  # pylint: disable=broad-except
        pass  # the fake writes empty reports, which diamond's parser cannot read; not the point

    assert not (run_dir / impl_folder / "old.txt").exists(), "the literal name was removed"
    (new,) = [call for call in fake_calls(run_dir) if call[:2] == ["prj_project", "new"]]
    assert new[new.index("-impl_dir") + 1] == str(run_dir.resolve() / impl_folder)


def test_a_work_directory_a_tool_left_as_a_link_is_removed_as_a_link(tmp_path, monkeypatch):
    """A tool may turn a work directory into a symbolic link to anywhere (an install tree, say).
    The Vivado simulation script deletes `xsim.dir` in the run directory: the next run removes
    the link itself and proceeds -- it neither fails nor touches what the link leads to."""
    use_fake_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, "xsim")
    before = _tree(outside)
    run_dir = launcher.get_flow_run_path("sqrt", "vivado_sim")
    run_dir.mkdir(parents=True)  # under the marked run root: xeda's, where a tool made a link
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
    outside the run directory, and is refused, naming the link, with nothing removed."""
    _no_tools(monkeypatch)
    launcher = _launcher(tmp_path)
    outside = _outside(tmp_path, "x")
    before = _tree(outside)
    run_dir = launcher.get_flow_run_path("sqrt", "verilator")
    run_dir.mkdir(parents=True)
    (run_dir / "tools").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunDirectoryError) as refused:
        launcher.launch_flow(Verilator, _verilator_design(tmp_path), {"sim_dir": "tools/sim"})

    assert str(run_dir / "tools") in str(refused.value)
    assert (run_dir / "tools").is_symlink()
    assert _tree(outside) == before


def test_a_cocotb_results_file_outside_the_run_directory_is_refused(tmp_path, monkeypatch):
    """cocotb's results file is removed before each simulation, so that an earlier one cannot
    pass for this run's: `cocotb.results_xml` given as `../results.xml` is refused at launch,
    naming the setting, and the file there is not deleted."""
    _no_tools(monkeypatch)
    canary = tmp_path / "results.xml"
    canary.write_text(PRECIOUS)

    with pytest.raises(FlowSettingsError) as refused:
        _launcher(tmp_path).launch_flow(
            Verilator, _verilator_design(tmp_path), {"cocotb": {"results_xml": "../results.xml"}}
        )

    assert "results_xml" in str(refused.value)
    assert canary.read_text() == PRECIOUS


def test_a_cocotb_results_file_given_as_a_location_is_delivered_never_deleted(
    tmp_path, monkeypatch
):
    """`cocotb.results_xml` given as a location is a deliverable, not a place the simulation
    works in: the run writes `results.xml` in its own directory, and the file already at the
    location is neither deleted nor replaced unless the user says so -- the launch is refused
    before any tool runs."""
    from xeda.deliver import OutputExistsError

    _no_tools(monkeypatch)
    canary = tmp_path / "results.xml"
    canary.write_text(PRECIOUS)

    with pytest.raises(OutputExistsError):
        _launcher(tmp_path).launch_flow(
            Verilator, _verilator_design(tmp_path), {"cocotb": {"results_xml": str(canary)}}
        )

    assert canary.read_text() == PRECIOUS


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
    design = Design.from_file(SQRT / "sqrt.yaml")
    first = _launcher(tmp_path).run("vivado_synth", design=design, flow_settings=XILINX_SETTINGS)
    assert first is not None and first.succeeded
    canaries = {}
    for name in GENERATED:
        assert (first.run_path / name).is_file(), name
        (first.run_path / name).unlink()
        canaries[name] = _linked_canary(tmp_path, first.run_path / name, name)

    # not cleaned first: the links must survive to the write, and be replaced there
    again = _launcher(tmp_path, rebuild_all=True).run(
        "vivado_synth", design=design, flow_settings=XILINX_SETTINGS
    )

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symbolic links")
def test_removing_a_read_only_directory_never_changes_a_linked_file(tmp_path):
    """The retry that makes a read-only entry writable acts on the entry, never through a
    tool-made link to a user's file."""
    from xeda.run_dir import rmtree

    outside = tmp_path / "user.txt"
    outside.write_text("mine")
    outside.chmod(0o640)
    before = outside.stat().st_mode
    run = tmp_path / "run"
    locked = run / "locked"
    locked.mkdir(parents=True)
    (locked / "link").symlink_to(outside)
    locked.chmod(0o555)
    try:
        rmtree(run)
    finally:
        if locked.exists():
            locked.chmod(0o755)
    assert not run.exists()
    assert outside.read_text() == "mine"
    assert outside.stat().st_mode == before
