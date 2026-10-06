"""What a setting that names no file binds a run to: where it points.

A setting may name a directory (a compiled library, an include directory) or a path that does
not exist yet. The run's identity (`flowrun_hash`) is location-free -- `$PWD/libs` and
`$DESIGN_ROOT/libs` count as written -- so two launches from different start directories, or of
two design trees with identical text, have one identity; the trace records where each such
setting pointed, and a launch where it points elsewhere is stale.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner
from pydantic import Field

from xeda import Design
from xeda.cli import cli
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner

from .tool_utils import require_ghdl


class _ProbeLibraries(Flow):
    """Reads `pkg.txt` from the directory of its library `mylib`, as a simulator reads a compiled
    library, and copies it to an output."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        lib_paths: list[tuple[str, Path]] = Field(
            [], description="Libraries: a name and the directory holding it."
        )

    def run(self) -> None:
        _name, directory = self.settings.lib_paths[0]
        out = self.run_path / "outputs" / "out.txt"
        out.parent.mkdir(exist_ok=True)
        library = self.normalize_path_to_design_root(directory)  # a relative one: under it
        out.write_text((library / "pkg.txt").read_text())
        self.artifacts.out = out


@pytest.fixture(autouse=True)
def _unregister():
    yield
    for name in (_ProbeLibraries.name, _ProbeLibraries.__name__):
        registered_flows.pop(name, None)


def _library(directory: Path, k: int) -> Path:
    (directory / "libs").mkdir(parents=True)
    (directory / "libs" / "pkg.txt").write_text(f"K = {k}\n")
    return directory


def _launch(run_root: Path, design: Design, settings: dict):
    flow = DefaultRunner(run_root, display_results=False).launch_flow(
        _ProbeLibraries, design, settings
    )
    assert flow.succeeded
    return flow, Path(flow.artifacts.out).read_text()


def test_a_library_named_from_another_start_directory_is_stale(tmp_path, monkeypatch):
    """`$PWD/libs`, launched from `A` and then from `B`, one run directory."""
    a, b = _library(tmp_path / "A", 1), _library(tmp_path / "B", 2)
    (tmp_path / "design").mkdir()
    design = Design(name="t", design_root=tmp_path / "design", rtl={"sources": [], "top": "t"})
    settings = {"lib_paths": [["mylib", "$PWD/libs"]]}
    monkeypatch.chdir(a)
    _launch(tmp_path / "run", design, settings)
    monkeypatch.chdir(b)
    flow, out = _launch(tmp_path / "run", design, settings)
    assert not flow.reused, "the library in the first start directory was taken for this one"
    was, now = (a / "libs").resolve(), (b / "libs").resolve()
    assert flow.stale_reason == f"lib_paths[0][1] now names {now} (was {was})"
    assert out == "K = 2\n"
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused


def test_a_library_of_another_design_tree_with_the_same_text_is_stale(tmp_path):
    """Two design trees whose design files read the same, each beside its own `libs/`."""
    trees = [_library(tmp_path / "v" / name, k) for name, k in (("a", 1), ("b", 2))]
    settings = {"lib_paths": [["mylib", "$DESIGN_ROOT/libs"]]}
    outs = []
    for tree in trees:
        design = Design(name="soc", design_root=tree, rtl={"sources": [], "top": "t"})
        flow, out = _launch(tmp_path / "run", design, settings)
        outs.append(out)
    assert not flow.reused, "the first tree's library was taken for the second's"
    was, now = (trees[0] / "libs").resolve(), (trees[1] / "libs").resolve()
    assert flow.stale_reason == f"lib_paths[0][1] now names {now} (was {was})"
    assert outs == ["K = 1\n", "K = 2\n"]


def test_a_relative_library_of_another_design_tree_is_stale(tmp_path, monkeypatch):
    """A relative path is looked up under the design root and the start directory: where it
    exists there is where it points -- another tree's, another place; another start directory
    without such a path, the same place."""
    trees = [_library(tmp_path / "v" / name, k) for name, k in (("a", 1), ("b", 2))]
    settings = {"lib_paths": [["mylib", "libs"]]}
    for start in ("A", "B"):
        (tmp_path / start).mkdir()
        monkeypatch.chdir(tmp_path / start)
        design = Design(name="soc", design_root=trees[0], rtl={"sources": [], "top": "t"})
        flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused, flow.stale_reason  # from B too: no `libs` there
    design = Design(name="soc", design_root=trees[1], rtl={"sources": [], "top": "t"})
    flow, out = _launch(tmp_path / "run", design, settings)
    was, now = (trees[0] / "libs").resolve(), (trees[1] / "libs").resolve()
    assert not flow.reused and flow.stale_reason == f"lib_paths[0][1] now names {now} (was {was})"
    assert out == "K = 2\n"


def test_a_setting_that_names_a_file_is_bound_by_the_file(tmp_path, monkeypatch):
    """Where a setting names an existing file, the file's record binds it (an input): the start
    directory changing alone, with the same file named, is no change."""
    lib = _library(tmp_path / "L", 1)
    (tmp_path / "design").mkdir()
    design = Design(name="t", design_root=tmp_path / "design", rtl={"sources": [], "top": "t"})
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    for start in ("A", "B"):
        (tmp_path / start).mkdir()
        monkeypatch.chdir(tmp_path / start)
        flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused, flow.stale_reason


# --- The probes, with GHDL and a compiled VHDL library ------------------------------------------

TB = """library mylib; use mylib.pkg.all;
entity tb is end entity;
architecture a of tb is begin
  process begin
    assert K = 1 report "K is not 1" severity failure;
    std.env.finish;
    wait;
  end process;
end architecture;
"""


def _compiled(directory: Path, k: int) -> Path:
    """`mylib`, with `pkg.K = k`, compiled into `<directory>/libs`."""
    (directory / "libs").mkdir(parents=True)
    (directory / "pkg.vhd").write_text(
        f"package pkg is constant K : integer := {k}; end package;\n"
    )
    subprocess.run(
        ["ghdl", "-a", "--std=08", "--work=mylib", "--workdir=libs", "pkg.vhd"],
        cwd=directory,
        check=True,
    )
    return directory


def test_ghdl_sim_with_a_library_named_from_another_start_directory_is_stale(tmp_path, monkeypatch):
    """As found: from `B`, whose `pkg.K` is 2, GHDL was "up to date and passing"."""
    require_ghdl()
    from xeda.flows import GhdlSim

    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "tb.vhd").write_text(TB)
    design = Design(
        name="t",
        design_root=tmp_path / "design",
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    settings = {"lib_paths": [["mylib", "$PWD/libs"]]}
    results = []
    for start, k in (("A", 1), ("B", 2)):
        monkeypatch.chdir(_compiled(tmp_path / start, k))
        runner = DefaultRunner(tmp_path / "run", display_results=False)
        flow = runner.launch_flow(GhdlSim, design, settings)
        results.append((flow.reused, flow.succeeded))
    assert results == [(False, True), (False, False)], "B's library was never simulated"
    assert flow.stale_reason.startswith("lib_paths[0][1] now names ")


def test_ghdl_sim_of_another_design_tree_with_the_same_text_is_stale(tmp_path, monkeypatch):
    """As found, through the command line: `v/b/soc.toml` was "up to date"."""
    require_ghdl()
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "tb.vhd").write_text(TB)
    toml = """name = "soc"
language.vhdl.standard = "2008"
[rtl]
sources = []
top = "tb"
[tb]
sources = ["../../common/tb.vhd"]
top = "tb"
[flows.ghdl_sim]
lib_paths = [["mylib", "$DESIGN_ROOT/libs"]]
"""
    for name, k in (("a", 1), ("b", 2)):
        (_compiled(tmp_path / "v" / name, k) / "soc.toml").write_text(toml)
    monkeypatch.chdir(tmp_path)
    outcomes = []
    for name in ("a", "b"):
        result = CliRunner().invoke(
            cli, ["run", "ghdl_sim", f"v/{name}/soc.toml", "--run-root", "run", "--json"]
        )
        outcomes.append(result.output)
    assert '"success": true' in outcomes[0]
    assert '"success": false' in outcomes[1], "the second tree's library was never simulated"
    assert "now names" in outcomes[1] and os.fspath(tmp_path) in outcomes[1]


# --- What is in a directory a setting names ----------------------------------------


def _probe_design(tmp_path: Path) -> Design:
    (tmp_path / "design").mkdir(exist_ok=True)
    return Design(name="t", design_root=tmp_path / "design", rtl={"sources": [], "top": "t"})


def _change(libs: Path, change: str) -> str:
    """Change the library directory `libs` in place, as recompiling a library does; the file
    the change is about."""
    if change == "edit":
        (libs / "pkg.txt").write_text("K = 2\n")
        return str((libs / "pkg.txt").resolve())
    if change == "edit deep":
        (libs / "sub" / "deep.txt").write_text("edited\n")
        return str((libs / "sub" / "deep.txt").resolve())
    if change == "add":
        (libs / "sub" / "new.txt").write_text("new\n")
        return str((libs / "sub" / "new.txt").resolve())
    assert change == "remove"
    (libs / "other.txt").unlink()
    return str((libs / "other.txt").resolve())


@pytest.mark.parametrize("change", ["edit", "edit deep", "add", "remove"])
def test_a_change_inside_a_library_directory_makes_the_run_stale(tmp_path, change):
    """In place: the setting still names the same directory, but a file in it -- at any depth
    -- was edited, added or removed. Unchanged, the run is reused."""
    lib = _library(tmp_path / "L", 1)
    (lib / "libs" / "other.txt").write_text("other\n")
    (lib / "libs" / "sub").mkdir()
    (lib / "libs" / "sub" / "deep.txt").write_text("deep\n")
    design = _probe_design(tmp_path)
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    _launch(tmp_path / "run", design, settings)
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused, flow.stale_reason
    changed = _change(lib / "libs", change)
    flow, out = _launch(tmp_path / "run", design, settings)
    assert not flow.reused, f"a file {change} in the library went unnoticed"
    reason = {
        "add": "new input",
        "remove": "input no longer used",
    }.get(change, "input changed")
    assert flow.stale_reason == f"{reason}: {changed}"
    assert out == ("K = 2\n" if change == "edit" else "K = 1\n")
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused, flow.stale_reason


@pytest.mark.skipif(os.name == "nt", reason="symbolic links and FIFOs")
def test_a_link_in_a_library_directory_is_recorded_as_itself(tmp_path):
    """As in a run directory: a link to a file by its target and the file's content, a
    link to a directory by its target -- and then followed, each directory once, so a
    loop is no trouble; a FIFO is listed, never read."""
    lib = _library(tmp_path / "L", 1)
    outside = tmp_path / "outside"
    (outside / "a").mkdir(parents=True)
    (outside / "b").mkdir()
    (outside / "defs.txt").write_text("defs\n")
    (lib / "libs" / "defs.txt").symlink_to(outside / "defs.txt")
    (lib / "libs" / "vendor").symlink_to(outside / "a", target_is_directory=True)
    (lib / "libs" / "loop").symlink_to(lib / "libs", target_is_directory=True)
    os.mkfifo(lib / "libs" / "pipe")
    design = _probe_design(tmp_path)
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    flow, _ = _launch(tmp_path / "run", design, settings)
    trace = json.loads((flow.run_path / "trace.json").read_text())
    libs = (lib / "libs").resolve()
    assert sorted(p for p in trace["inputs"] if p.startswith(str(libs))) == [
        str(libs / name) for name in ("defs.txt", "loop", "pipe", "pkg.txt", "vendor")
    ]
    assert _launch(tmp_path / "run", design, settings)[0].reused
    (outside / "defs.txt").write_text("edited\n")
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.stale_reason == f"input changed: {libs / 'defs.txt'}"
    (lib / "libs" / "vendor").unlink()
    (lib / "libs" / "vendor").symlink_to(outside / "b", target_is_directory=True)
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.stale_reason == f"input changed: {libs / 'vendor'}"


# --- Directory entries, special files and directory links in a listing -----------------

LIBS = {"lib_paths": [["mylib", "$DESIGN_ROOT/libs"]]}


def _tree(tmp_path) -> tuple[Path, Design]:
    tree = _library(tmp_path / "t", 1)
    return tree, Design(name="t", design_root=tree, rtl={"sources": [], "top": "t"})


def test_an_empty_directory_added_to_a_library_is_a_new_input(tmp_path):
    """An empty directory is an entry of its own."""
    tree, design = _tree(tmp_path)
    _launch(tmp_path / "run", design, LIBS)
    (tree / "libs" / "work").mkdir()
    flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
        _ProbeLibraries, design, LIBS
    )
    assert not flow.reused
    assert flow.stale_reason == f"new input: {(tree / 'libs' / 'work').resolve()}"


def test_an_empty_directory_added_to_a_run_directory_is_a_change(tmp_path):
    _tree_path, design = _tree(tmp_path)
    first, _ = _launch(tmp_path / "run", design, LIBS)
    (first.run_path / "extra").mkdir()
    flow, _ = _launch(tmp_path / "run", design, LIBS)
    assert flow.stale_reason == f"new file in the run directory: {first.run_path / 'extra'}"


@pytest.mark.skipif(os.name == "nt", reason="FIFOs")
def test_a_fifo_in_a_library_is_listed_and_never_read(tmp_path):
    tree, design = _tree(tmp_path)
    os.mkfifo(tree / "libs" / "pipe")  # reading it would block
    _launch(tmp_path / "run", design, LIBS)
    flow, _ = _launch(tmp_path / "run", design, LIBS)
    assert flow.reused, flow.stale_reason


@pytest.mark.skipif(os.name == "nt", reason="symbolic links")
def test_a_library_behind_a_directory_link_is_listed_through_it(tmp_path):
    """An edit beneath a linked directory's target is noticed; a link cycle ends."""
    real = _library(tmp_path / "real", 1) / "libs"
    tree = tmp_path / "t"
    (tree / "libs").mkdir(parents=True)
    (tree / "libs" / "pkg.txt").write_text("K = 0\n")
    (tree / "libs" / "vendor").symlink_to(real, target_is_directory=True)
    (tree / "libs" / "loop").symlink_to(tree / "libs", target_is_directory=True)
    design = Design(name="t", design_root=tree, rtl={"sources": [], "top": "t"})
    _launch(tmp_path / "run", design, LIBS)
    (real / "pkg.txt").write_text("K = 2\n")
    flow, _ = _launch(tmp_path / "run", design, LIBS)
    listed = (tree / "libs").resolve() / "vendor" / "pkg.txt"
    assert not flow.reused and flow.stale_reason == f"input changed: {listed}"


def test_the_run_s_own_directories_are_not_listed(tmp_path, monkeypatch):
    """A setting naming a directory that holds the run root (`$DESIGN_ROOT`, the run root under
    it) does not list the run root: the flow's own `reports`/`outputs` directories, and every
    other run's, are not its inputs, so a second launch is reused."""
    lib = _library(tmp_path / "L", 1)
    design = Design(name="t", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    monkeypatch.chdir(tmp_path)
    settings = {"lib_paths": [["mylib", str(lib / "libs")], ["root", "$DESIGN_ROOT"]]}
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flows = [runner.launch_flow(_ProbeLibraries, design, settings) for _ in range(2)]
    assert flows[-1].reused, flows[-1].stale_reason
    trace = json.loads((flows[-1].run_path / "trace.json").read_text())
    assert not [p for p in trace["inputs"] if "xeda_run" in p]


def test_a_large_library_directory_is_reported(tmp_path, monkeypatch, caplog):
    """No cap on what is listed -- a cap would let a change past it go unnoticed -- but a
    listing past `LARGE_LISTING_FILES` is logged, with the setting and the count."""
    from xeda.flow_runner import trace_inputs

    monkeypatch.setattr(trace_inputs, "LARGE_LISTING_FILES", 3)
    lib = _library(tmp_path / "L", 1)
    for i in range(4):
        (lib / "libs" / f"unit{i}.txt").write_text(f"{i}\n")
    with caplog.at_level(logging.WARNING, logger="xeda.flow_runner.trace_inputs"):
        _launch(
            tmp_path / "run", _probe_design(tmp_path), {"lib_paths": [["mylib", str(lib / "libs")]]}
        )
    assert f"lib_paths[0][1] names {(lib / 'libs').resolve()}: its 5 files" in caplog.text


def test_ghdl_sim_with_a_library_recompiled_in_place_is_stale(tmp_path, monkeypatch):
    """The in-place variant of the two above: the same `libs` recompiled with `pkg.K` = 2 -- the
    setting names the same directory -- was "up to date and passing"."""
    require_ghdl()
    from xeda.flows import GhdlSim

    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "tb.vhd").write_text(TB)
    design = Design(
        name="t",
        design_root=tmp_path / "design",
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    lib = _compiled(tmp_path / "L", 1)
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(GhdlSim, design, settings)
    assert first.succeeded
    assert runner.launch_flow(GhdlSim, design, settings).reused
    (lib / "pkg.vhd").write_text("package pkg is constant K : integer := 2; end package;\n")
    subprocess.run(
        ["ghdl", "-a", "--std=08", "--work=mylib", "--workdir=libs", "pkg.vhd"],
        cwd=lib,
        check=True,
    )
    flow = runner.launch_flow(GhdlSim, design, settings)
    assert not flow.reused, "the recompiled library was never simulated"
    assert flow.stale_reason.startswith(f"input changed: {(lib / 'libs').resolve()}/")
    assert not flow.succeeded


def test_verilator_s_include_directories_are_listed(tmp_path):
    """`include_dirs` names directories (a path setting, so the trace sees it): every entry
    under one -- a subdirectory too -- is a candidate input; the run directory's own directories
    are not."""
    from xeda.flow_runner.trace_inputs import setting_directory_files
    from xeda.flows import Verilator

    (tmp_path / "inc" / "sub").mkdir(parents=True)
    (tmp_path / "inc" / "defs.vh").write_text("`define W 8\n")
    (tmp_path / "inc" / "sub" / "more.vh").write_text("`define D 2\n")
    (tmp_path / "run" / "obj").mkdir(parents=True)
    settings = Verilator.Settings.from_input(
        {"include_dirs": ["inc", "$DESIGN_ROOT/run/obj"]},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    inc = (tmp_path / "inc").resolve()
    assert setting_directory_files(settings, tmp_path / "run") == [
        inc / "defs.vh",
        inc / "sub",
        inc / "sub" / "more.vh",
    ]


def test_an_unchanged_library_directory_is_not_read_again(tmp_path, monkeypatch):
    """Each run records its inputs just before it starts; a file whose metadata the previous
    run's record vouches for (`FileRecord.trusted`) is not read again -- a large library costs a
    `stat` per file per run, not a hash."""
    from xeda import digest

    monkeypatch.setattr(digest, "RACY_NS", 0)
    lib = _library(tmp_path / "L", 1)
    for i in range(20):
        (lib / "libs" / f"unit{i}.txt").write_text(f"{i}\n")
    design = _probe_design(tmp_path)
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    read: list[Path] = []
    content_digest = digest.content_digest

    def counting(path: Path) -> str:
        read.append(Path(path))
        return content_digest(path)

    monkeypatch.setattr(digest, "content_digest", counting)
    DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
        _ProbeLibraries, design, settings
    )
    libs = (lib / "libs").resolve()
    assert len([p for p in read if p.is_relative_to(libs)]) == 21
    read.clear()
    flow = DefaultRunner(tmp_path / "run", display_results=False, rebuild_all=True).launch_flow(
        _ProbeLibraries, design, settings
    )
    assert flow.succeeded and not flow.reused
    assert [p for p in read if p.is_relative_to(libs)] == [], "the library was read again"


# --- VCS metadata in a library directory --------------------------------------------------


@pytest.mark.parametrize("vcs", [".git", ".hg", ".svn"])
def test_version_control_metadata_in_a_library_directory_is_not_an_input(tmp_path, vcs):
    """A tool never reads `.git`, `.hg` or `.svn`: a commit or a fetch in a library that is a
    repository is no change to the run."""
    lib = _library(tmp_path / "L", 1)
    (lib / "libs" / vcs / "objects").mkdir(parents=True)
    (lib / "libs" / vcs / "HEAD").write_text("ref: main\n")
    design = _probe_design(tmp_path)
    settings = {"lib_paths": [["mylib", str(lib / "libs")]]}
    flow, _ = _launch(tmp_path / "run", design, settings)
    trace = json.loads((flow.run_path / "trace.json").read_text())
    assert not [p for p in trace["inputs"] if f"/{vcs}/" in p]
    (lib / "libs" / vcs / "HEAD").write_text("ref: other\n")
    (lib / "libs" / vcs / "objects" / "ab").write_text("new object\n")
    flow, _ = _launch(tmp_path / "run", design, settings)
    assert flow.reused, flow.stale_reason
