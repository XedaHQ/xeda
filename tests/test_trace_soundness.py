"""A run's trace records what the run consumed where it consumed it, and a file's origin decides
whether it is an input or the run's own.

The failure these guard against is a stale result that looks fresh: an input edited while a long
run was still going (recorded as if the run had read the new content), a constraint file skipped
because `--cwd` made the design directory the run directory, a `$readmemh` file given as a design
parameter. Each toy flow reads one kind of input and copies it to an output, so a false fresh
result shows as an output that differs from the input it claims to be built from. `EDIT` makes a
toy flow's `run()` simulate the user saving an edit while the tool still runs -- optionally with
the file's mtime set back into the past, as a long run (or a copy that preserves times) leaves it.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import ClassVar, Optional

import pytest
from pydantic import Field

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.proc_utils import note_program

#: what the next toy run() does to simulate a concurrent edit: {kind: (path, new text, backdate)}
EDIT: dict = {}


def _edit_during_run(kind: str) -> None:
    if kind not in EDIT:
        return
    path, text, backdate = EDIT.pop(kind)
    time.sleep(0.01)
    path.write_text(text)
    if backdate:  # the tool keeps running: the edit ends up older than any 2 s racy window
        past = time.time_ns() - 10_000_000_000
        os.utime(path, ns=(past, past))


def _copy_to_output(flow: Flow, text: str) -> None:
    out = flow.run_path / "outputs" / "out.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text)
    flow.artifacts.out = out


@pytest.fixture(scope="module")
def probes():
    class ProbeSettingFile(Flow):
        """Reads a constraints file named by a setting and copies it to an output."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            constraints: Optional[Path] = Field(None, description="A constraints file.")

        def run(self) -> None:
            assert self.settings.constraints is not None
            source = self.settings.constraints
            if not source.is_absolute():
                source = self.design.root_path / source
            _copy_to_output(self, source.read_text())
            _edit_during_run("setting")

    class ProbeSource(Flow):
        """Copies the first design source to an output."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            _copy_to_output(self, self.design.rtl.sources[0].file.read_text())
            _edit_during_run("source")

    class ProbeDepfile(Flow):
        """Reads an include file it reports in a depfile, and copies it to an output."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            include = self.design.root_path / "inc.vh"
            text = include.read_text()
            (self.run_path / "deps.d").write_text(f"out.txt: {include}\n")
            self.depfiles.append(self.run_path / "deps.d")
            _copy_to_output(self, text)
            _edit_during_run("include")

    class ProbeScript(Flow):
        """Renders a script into its run directory and reports reading it, as yosys -E does."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            script = self.run_path / "script.ys"
            script.write_text("read_verilog a.v\n")
            (self.run_path / "deps.d").write_text(f"out.txt: {script.name}\n")
            self.depfiles.append(self.run_path / "deps.d")
            _copy_to_output(self, script.read_text())

    class ProbeRom(Flow):
        """Copies the file a design parameter names ($readmemh data) to an output."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            _copy_to_output(self, Path(self.design.rtl.parameters["ROM"]).read_text())

    class ProbeOutputPath(Flow):
        """Writes a netlist where a setting says, outside its run directory."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            netlist: Optional[Path] = Field(None, description="Where the netlist goes.")

        def run(self) -> None:
            assert self.settings.netlist is not None
            self.settings.netlist.parent.mkdir(parents=True, exist_ok=True)
            self.settings.netlist.write_text("netlist\n")
            self.artifacts.netlist = self.settings.netlist
            _copy_to_output(self, "done\n")

    class ProbeProgram(Flow):
        """Starts a program, which is replaced while the run goes on."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            program: Optional[Path] = Field(None, description="The program it starts.")

        def run(self) -> None:
            assert self.settings.program is not None
            note_program(str(self.settings.program))
            _copy_to_output(self, "done\n")
            _edit_during_run("program")

    class ProbeScratcher(Flow):
        """Writes files it does not declare besides its one artifact, as a tool leaves a netlist
        or a checkpoint its depender reads by path."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "scratch.txt").write_text("scratch\n")
            (self.run_path / "sub").mkdir(exist_ok=True)
            (self.run_path / "sub" / "deep.txt").write_text("deep\n")
            _copy_to_output(self, "declared\n")

    class ProbeScratchReader(Flow):
        """Reads its dependency's undeclared files by path, and copies them to an output."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ProbeScratcher, ProbeScratcher.Settings())

        def run(self) -> None:
            (scratcher,) = self.completed_dependencies
            texts = [(scratcher.run_path / n).read_text() for n in ("scratch.txt", "sub/deep.txt")]
            _copy_to_output(self, "".join(texts))

    classes = (
        ProbeSettingFile,
        ProbeSource,
        ProbeDepfile,
        ProbeScript,
        ProbeRom,
        ProbeOutputPath,
        ProbeProgram,
        ProbeScratcher,
        ProbeScratchReader,
    )
    yield {cls.__name__: cls for cls in classes}
    EDIT.clear()
    for cls in classes:
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "design"
    root.mkdir()
    (root / "a.v").write_text("module a; endmodule\n")
    (root / "c.xdc").write_text("period 10\n")
    (root / "inc.vh").write_text("`define W 4\n")
    (root / "rom.mem").write_text("00\n")
    return root


def _design(root: Path, **rtl) -> Design:
    return Design(name="p", rtl={"sources": ["a.v"], "top": "a", **rtl}, design_root=root)


def _launch(root: Path, cls, settings=None, design=None, **launcher):
    runner = DefaultRunner(root.parent / "xeda_run", display_results=False, **launcher)
    flow = runner.launch_flow(cls, design or _design(root), settings or {})
    assert flow.succeeded
    return flow, Path(flow.artifacts.out).read_text()


@pytest.mark.parametrize("backdate", [False, True], ids=["mtime now", "mtime backdated"])
def test_a_setting_file_edited_during_the_run_is_stale_next_time(probes, root, backdate):
    """The trace records the file as it was when the run started, not as the run left it."""
    cls = probes["ProbeSettingFile"]
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings)
    EDIT["setting"] = (root / "c.xdc", "period 5\n", backdate)
    _, out = _launch(root, cls, settings, rebuild_all=True)
    assert out == "period 10\n"  # what the run read
    flow, out = _launch(root, cls, settings)
    assert not flow.reused, "a result built from the old constraints looked fresh"
    assert flow.stale_reason == f"input modified during the last run: {(root / 'c.xdc').resolve()}"
    assert out == "period 5\n"
    flow, _ = _launch(root, cls, settings)
    assert flow.reused  # and once rebuilt from the edited file, it is fresh again


@pytest.mark.parametrize("same_design", [False, True], ids=["new Design", "reused Design"])
def test_a_source_edited_during_the_run_is_stale_next_time(probes, root, same_design):
    cls = probes["ProbeSource"]
    design = _design(root)
    _launch(root, cls, design=design)
    EDIT["source"] = (root / "a.v", "module b; endmodule\n", True)
    _launch(root, cls, design=design if same_design else _design(root), rebuild_all=True)
    flow, out = _launch(root, cls, design=design if same_design else _design(root))
    assert not flow.reused and out == "module b; endmodule\n"
    assert flow.stale_reason == f"input modified during the last run: {(root / 'a.v').resolve()}"


def test_an_include_known_from_the_last_run_edited_during_the_run_is_stale(probes, root):
    """A depfile names its files only after the run; the ones the previous run's depfile named
    are recorded before the run starts, like every expected input."""
    cls = probes["ProbeDepfile"]
    _launch(root, cls)
    EDIT["include"] = (root / "inc.vh", "`define W 8\n", True)
    _launch(root, cls, rebuild_all=True)
    flow, out = _launch(root, cls)
    assert not flow.reused and out == "`define W 8\n"
    assert flow.stale_reason == f"input modified during the last run: {(root / 'inc.vh').resolve()}"


def test_a_new_include_edited_during_the_run_is_stale(probes, root):
    """A file a depfile names for the first time, written while the run went on, cannot be
    vouched for: it is recorded as unknown, and the next launch runs again."""
    cls = probes["ProbeDepfile"]
    EDIT["include"] = (root / "inc.vh", "`define W 8\n", False)
    _, out = _launch(root, cls)
    assert out == "`define W 4\n"
    flow, out = _launch(root, cls)
    assert not flow.reused and out == "`define W 8\n"
    include = (root / "inc.vh").resolve()
    assert flow.stale_reason == f"input modified during the last run: {include}"
    flow, _ = _launch(root, cls)
    assert flow.reused


def test_a_file_the_run_writes_into_its_own_directory_is_not_an_input(probes, root):
    """A rendered script a depfile lists is the run's own: recorded as an output, so the next
    launch is fresh (and a hand edit of it is still noticed)."""
    cls = probes["ProbeScript"]
    first, _ = _launch(root, cls)
    trace = json.loads((first.run_path / "trace.json").read_text())
    script = str((first.run_path / "script.ys").resolve())
    assert script in trace["outputs"] and script not in trace["implicit_inputs"]
    flow, _ = _launch(root, cls)
    assert flow.reused


def test_a_file_valued_design_parameter_is_an_input(probes, root):
    cls = probes["ProbeRom"]

    def design() -> Design:
        return _design(root, parameters={"ROM": {"file": "rom.mem"}})

    _launch(root, cls, design=design())
    (root / "rom.mem").write_text("ff\n")
    flow, out = _launch(root, cls, design=design())
    assert not flow.reused and out == "ff\n"
    assert flow.stale_reason == f"input changed: {(root / 'rom.mem').resolve()}"


def test_an_output_named_by_a_setting_outside_the_run_directory_is_unknown(probes, root):
    """Until outputs are declared, a setting cannot make an external file run-owned."""
    cls = probes["ProbeOutputPath"]
    netlist = root.parent / "elsewhere" / "net.v"
    settings = {"netlist": str(netlist)}
    first, _ = _launch(root, cls, settings)
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert str(netlist.resolve()) not in trace["outputs"]
    assert str(netlist.resolve()) in trace["inputs"]
    assert trace["inputs"][str(netlist.resolve())]["sha"] == "unknown: modified during the run"
    flow, _ = _launch(root, cls, settings)
    assert not flow.reused
    flow, _ = _launch(root, cls, settings)
    assert not flow.reused


@pytest.mark.parametrize("kind", ["setting", "depfile"])
def test_external_input_edited_during_repeated_runs_stays_unknown(probes, root, kind):
    """An external input edited while each run is active must never settle as an output."""
    cls = probes["ProbeSettingFile"] if kind == "setting" else probes["ProbeDepfile"]
    settings = {"constraints": "c.xdc"} if kind == "setting" else None
    target = root / ("c.xdc" if kind == "setting" else "inc.vh")
    edit_kind = kind if kind == "setting" else "include"
    _launch(root, cls, settings)
    for index in range(2):
        EDIT[edit_kind] = (target, f"changed {index}\n", False)
        _launch(root, cls, settings, rebuild_all=True)
        flow, _ = _launch(root, cls, settings)
        assert not flow.reused
        assert "modified during the last run" in flow.stale_reason


def test_absent_setting_file_created_during_run_is_unknown(probes, root):
    class ProbeOptional(probes["ProbeSettingFile"]):
        """The file a setting names is optional until the run reads it."""

        class Settings(probes["ProbeSettingFile"].Settings):
            pass

        def run(self):
            source = self.design.root_path / "late.txt"
            _copy_to_output(self, source.read_text() if source.exists() else "absent\n")
            source.write_text("created\n")

    try:
        flow, out = _launch(root, ProbeOptional, {"constraints": "late.txt"})
        assert out == "absent\n"
        trace = json.loads((flow.run_path / "trace.json").read_text())
        target = str((root / "late.txt").resolve())
        assert trace["inputs"][target]["sha"] == "unknown: modified during the run"
        flow, _ = _launch(root, ProbeOptional, {"constraints": "late.txt"})
        assert not flow.reused
        # where the setting points changed too (it named nothing that existed): either says so
        assert "input modified during the last run" in flow.stale_reason or (
            flow.stale_reason == f"constraints now names {target} (was nothing that exists)"
        )
    finally:
        for name in (ProbeOptional.name, ProbeOptional.__name__):
            registered_flows.pop(name, None)


def test_absent_setting_file_created_then_changed_after_read_is_unknown(probes, root):
    class ProbeLate(probes["ProbeSettingFile"]):
        """A previously absent setting file is created, read, then changed by the user."""

        def run(self):
            source = self.design.root_path / "late.txt"
            source.write_text("v1\n")
            _copy_to_output(self, source.read_text())
            source.write_text("v2\n")

    try:
        settings = {"constraints": "late.txt"}
        flow, out = _launch(root, ProbeLate, settings)
        assert out == "v1\n"
        trace = json.loads((flow.run_path / "trace.json").read_text())
        target = str((root / "late.txt").resolve())
        assert trace["inputs"][target]["sha"] == "unknown: modified during the run"
        flow, out = _launch(root, ProbeLate, settings)
        assert not flow.reused and out == "v1\n"
    finally:
        for name in (ProbeLate.name, ProbeLate.__name__):
            registered_flows.pop(name, None)


def test_an_edited_results_json_is_stale(probes, root):
    cls = probes["ProbeSource"]
    first, _ = _launch(root, cls)
    results = first.run_path / "results.json"
    results.write_text(results.read_text().replace('"success": true', '"success": true '))
    flow, _ = _launch(root, cls)
    assert not flow.reused and flow.stale_reason == f"output changed: {results.resolve()}"


def test_a_program_replaced_during_the_run_is_stale(probes, root, tmp_path):
    cls = probes["ProbeProgram"]
    program = tmp_path / "tool.sh"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    settings = {"program": str(program)}
    EDIT["program"] = (program, "#!/bin/sh\necho new\n", True)
    _launch(root, cls, settings)
    flow, _ = _launch(root, cls, settings)
    assert not flow.reused and flow.stale_reason == f"{program} changed"
    flow, _ = _launch(root, cls, settings)
    assert flow.reused


def test_a_file_that_vanishes_while_the_trace_is_built_is_unknown(probes, root, monkeypatch):
    """A depfile entry removed between being listed and being recorded cannot be vouched for:
    it is recorded as unknown (the next launch runs again), not a crash after a good run."""
    from xeda.flow_runner import trace_inputs

    cls = probes["ProbeDepfile"]
    listed = trace_inputs.implicit_input_files

    def listed_then_removed(flow):
        found = listed(flow)
        (root / "inc.vh").rename(root / "inc.vh.moved")
        return found

    monkeypatch.setattr(trace_inputs, "implicit_input_files", listed_then_removed)
    _launch(root, cls)
    monkeypatch.setattr(trace_inputs, "implicit_input_files", listed)
    (root / "inc.vh.moved").rename(root / "inc.vh")
    flow, _ = _launch(root, cls)
    include = (root / "inc.vh").resolve()
    assert not flow.reused
    assert flow.stale_reason == f"input modified during the last run: {include}"


@pytest.mark.parametrize("process_offset", [30_000_000_000, -30_000_000_000])
def test_external_file_system_clock_controls_during_run_detection(
    probes, root, monkeypatch, process_offset
):
    """The run-directory filesystem clock, not process time, detects an external edit."""
    original_time_ns = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: original_time_ns() + process_offset)
    cls = probes["ProbeDepfile"]
    include = root / "inc.vh"
    _launch(root, cls)
    EDIT["include"] = (include, "`define W 8\n", True)
    _launch(root, cls, rebuild_all=True)
    flow, out = _launch(root, cls)
    assert not flow.reused and out == "`define W 8\n"
    assert "modified during the last run" in flow.stale_reason


def test_edited_external_file_with_controlled_mtime_is_unknown(probes, root):
    """A user edit that restores an old mtime is still unknown after its ctime changes."""
    cls = probes["ProbeSettingFile"]
    constraint = root / "c.xdc"
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings)
    EDIT["setting"] = (constraint, "period 5\n", True)
    forced, _ = _launch(root, cls, settings, rebuild_all=True)
    trace = json.loads((forced.run_path / "trace.json").read_text())
    assert trace["inputs"][str(constraint.resolve())]["sha"] == "unknown: modified during the run"


def test_file_valued_abc_script_is_an_input_and_changes_make_yosys_stale(root):
    """The script `init()` resolves is known before the run: an input recorded before it."""
    from xeda.flows.yosys.common import YosysBase

    class ProbeYosys(YosysBase):
        """A no-tool Yosys flow that exercises the real settings initializer."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self):
            _copy_to_output(self, "done\n")

    try:
        script = root / "map.abc"
        script.write_text("strash\n")
        settings = {"abc_script": str(script)}
        first, _ = _launch(root, ProbeYosys, settings)
        trace = json.loads((first.run_path / "trace.json").read_text())
        assert str(script.resolve()) in trace["inputs"]
        assert str(script.resolve()) not in trace["implicit_inputs"]
        script.write_text("strash\nmap\n")
        flow, _ = _launch(root, ProbeYosys, settings)
        assert not flow.reused
        assert flow.stale_reason == f"input changed: {script.resolve()}"
    finally:
        for name in (ProbeYosys.name, ProbeYosys.__name__):
            registered_flows.pop(name, None)


#: why a depender of `ProbeScratcher` runs after it ran again
SCRATCHER_AGAIN = "probe_scratcher (p/probe_scratcher) ran again"


@pytest.mark.parametrize("change", ["edit", "delete"])
@pytest.mark.parametrize("name", ["scratch.txt", "sub/deep.txt"])
def test_a_hand_changed_undeclared_file_of_a_dependency_reruns_it_and_its_depender(
    probes, root, name, change
):
    """A managed run directory's outputs are every file in it (ruling R37): a depender that
    reads a file of its dependency's by path -- as vivado_postsynth_sim reads the netlist and
    vivado_power the routed checkpoint -- no longer reuses a result built from a file edited or
    deleted by hand since. The dependency is stale, re-runs, and its depender follows."""
    cls = probes["ProbeScratchReader"]
    first, out = _launch(root, cls)
    assert out == "scratch\ndeep\n"
    changed = (first.completed_dependencies[0].run_path / name).resolve()
    if change == "edit":
        changed.write_text("hand-edited\n")
    else:
        changed.unlink()
    flow, out = _launch(root, cls)
    (dependency,) = flow.completed_dependencies
    assert not dependency.reused, "a hand-changed file of the dependency went unnoticed"
    why = "changed" if change == "edit" else "missing"
    assert dependency.stale_reason == f"output {why}: {changed}"
    assert not flow.reused and flow.stale_reason == SCRATCHER_AGAIN
    assert out == "scratch\ndeep\n"  # rebuilt from what the dependency writes
    flow, _ = _launch(root, cls)
    assert flow.reused and flow.completed_dependencies[0].reused


def test_a_managed_run_directory_s_outputs_are_every_file_in_it(probes, root):
    """Every regular file under the directory, recursively -- not only the artifacts -- but
    neither the trace itself nor xeda's temporary files."""
    first, _ = _launch(root, probes["ProbeScratchReader"])
    run_dir = first.completed_dependencies[0].run_path.resolve()
    trace = json.loads((run_dir / "trace.json").read_text())
    names = ("scratch.txt", "sub/deep.txt", "outputs/out.txt", "settings.json", "results.json")
    assert sorted(trace["outputs"]) == sorted(str(run_dir / name) for name in names)


@pytest.mark.parametrize("name", ["notes.txt", "sub/later.txt"])
def test_a_file_added_to_a_managed_run_directory_makes_it_stale(probes, root, name):
    """A managed run directory is xeda's alone: a file that appears in it after the run is a
    change -- a depender reading the directory may find it (ruling R44). The run is stale, and
    its depender follows; once the file is recorded, both are fresh again."""
    cls = probes["ProbeScratchReader"]
    first, _ = _launch(root, cls)
    added = (first.completed_dependencies[0].run_path / name).resolve()
    added.parent.mkdir(exist_ok=True)
    added.write_text("mine\n")
    flow, _ = _launch(root, cls)
    (dependency,) = flow.completed_dependencies
    assert not dependency.reused
    assert dependency.stale_reason == f"new file in the run directory: {added}"
    assert not flow.reused and flow.stale_reason == SCRATCHER_AGAIN
    flow, _ = _launch(root, cls)
    assert flow.reused and flow.completed_dependencies[0].reused


def test_pruning_still_drops_the_trace(probes, root):
    """Pruning keeps only what the run reports, so it cannot keep a trace that records every
    file of the directory: a pruned run is never reused."""
    cls = probes["ProbeScratchReader"]
    first, _ = _launch(root, cls, post_cleanup=True)
    for run_dir in (first.run_path, first.completed_dependencies[0].run_path):
        assert not (run_dir / "trace.json").exists()
        assert not (run_dir / "scratch.txt").exists()
    flow, out = _launch(root, cls, post_cleanup=True)
    assert not flow.reused and flow.stale_reason == "no successful previous run"
    assert flow.completed_dependencies[0].stale_reason == "no successful previous run"
    assert out == "scratch\ndeep\n"


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links and FIFOs")
def test_a_symbolic_link_in_a_run_directory_is_recorded_as_itself(root, tmp_path):
    """A symbolic link is an entry of its own, recorded by its target and, when that is a file,
    by the file's content (ruling R44) -- a directory it points to is not walked; a FIFO or
    socket is not a regular file and is not recorded at all (reading one would block)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lib.v").write_text("lib\n")
    (outside / "copy.v").write_text("lib\n")

    class ProbeLinks(Flow):
        """Links a file and a directory outside its run directory, and makes a FIFO."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            links = {"lib.v": outside / "lib.v", "libdir": outside, "gone": outside / "none"}
            for name, target in links.items():
                (self.run_path / name).unlink(missing_ok=True)
                (self.run_path / name).symlink_to(target)
            if not (self.run_path / "pipe").exists():
                os.mkfifo(self.run_path / "pipe")
            _copy_to_output(self, "done\n")

    try:
        first, _ = _launch(root, ProbeLinks)
        run_dir = first.run_path.resolve()
        outputs = json.loads((run_dir / "trace.json").read_text())["outputs"]
        for name in ("lib.v", "libdir", "gone"):
            assert outputs[str(run_dir / name)]["sha"].startswith("symlink:")
        assert not [path for path in outputs if run_dir / "libdir" in Path(path).parents]
        assert str(run_dir / "pipe") not in outputs
        assert _launch(root, ProbeLinks)[0].reused
        # an edit of the file it points to, outside the directory
        (outside / "lib.v").write_text("lib edited\n")
        flow, _ = _launch(root, ProbeLinks)
        assert not flow.reused and flow.stale_reason == f"output changed: {run_dir / 'lib.v'}"
        assert _launch(root, ProbeLinks)[0].reused
        # the same content through another target is another link
        (run_dir / "lib.v").unlink()
        (run_dir / "lib.v").symlink_to(outside / "copy.v")
        (outside / "copy.v").write_text("lib edited\n")
        flow, _ = _launch(root, ProbeLinks)
        assert not flow.reused and flow.stale_reason == f"output changed: {run_dir / 'lib.v'}"
    finally:
        for name in (ProbeLinks.name, ProbeLinks.__name__):
            registered_flows.pop(name, None)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a directory it cannot list"
)
def test_a_directory_the_run_left_unlistable_is_an_unknown_output(root):
    """What is in a directory that cannot be listed cannot be recorded: the directory's record is
    unknown, and the next launch runs again rather than vouching for files it never saw."""

    class ProbeLocked(Flow):
        """Leaves a directory behind that it cannot list."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            locked = self.run_path / "locked"
            if locked.exists():
                locked.chmod(0o755)
            locked.mkdir(exist_ok=True)
            (locked / "inside.txt").write_text("inside\n")
            locked.chmod(0)
            _copy_to_output(self, "done\n")

    locked = None
    try:
        first, _ = _launch(root, ProbeLocked)
        locked = first.run_path.resolve() / "locked"
        outputs = json.loads((first.run_path / "trace.json").read_text())["outputs"]
        assert outputs[str(locked)]["sha"] == "unknown: modified during the run"
        flow, _ = _launch(root, ProbeLocked)
        assert not flow.reused
        assert flow.stale_reason == f"output modified during the last run: {locked}"
    finally:
        if locked is not None and locked.exists():
            locked.chmod(0o755)
        for name in (ProbeLocked.name, ProbeLocked.__name__):
            registered_flows.pop(name, None)


@pytest.fixture
def no_racy_window(monkeypatch):
    """No racy window: a record is trusted by its metadata as soon as it was taken, so whether
    a file is read depends on the metadata compared alone."""
    from xeda import digest

    monkeypatch.setattr(digest, "RACY_NS", 0)


def _count_hashes(monkeypatch) -> list:
    """The files `record_file` reads, as they are read."""
    from xeda import digest

    hashed: list = []
    content_digest = digest.content_digest

    def counting(path):
        hashed.append(Path(path))
        return content_digest(path)

    monkeypatch.setattr(digest, "content_digest", counting)
    return hashed


def test_a_same_size_edit_with_its_recorded_mtime_restored_is_stale(probes, root):
    """An edit of the same size whose mtime is set back to the recorded one (`os.utime`,
    `touch -r`), long before the record was taken, still changes the file's inode change time:
    its metadata no longer matches the record, so it is hashed (ruling R38)."""
    cls = probes["ProbeSettingFile"]
    constraints = root / "c.xdc"
    past = time.time_ns() - 60 * 10**9
    os.utime(constraints, ns=(past, past))
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings)
    constraints.write_text("period 99\n")  # the size of "period 10\n"
    os.utime(constraints, ns=(past, past))
    flow, out = _launch(root, cls, settings)
    assert not flow.reused, "a same-size edit with its mtime restored looked unchanged"
    assert flow.stale_reason == f"input changed: {constraints.resolve()}"
    assert out == "period 99\n"


def test_a_same_size_edit_of_an_output_with_its_mtime_restored_is_stale(
    probes, root, no_racy_window
):
    first, _ = _launch(root, probes["ProbeSource"])
    out = Path(first.artifacts.out)
    before = out.stat()
    out.write_text("module X; endmodule\n")  # the size of "module a; endmodule\n"
    os.utime(out, ns=(before.st_atime_ns, before.st_mtime_ns))
    flow, _ = _launch(root, probes["ProbeSource"])
    assert not flow.reused, "a same-size edit of an output with its mtime restored went unseen"
    assert flow.stale_reason == f"output changed: {out.resolve()}"


@pytest.mark.parametrize("change", ["chmod", "copy", "move"])
def test_a_metadata_only_change_costs_a_hash_not_a_rerun(
    probes, root, no_racy_window, monkeypatch, change
):
    """A change of permissions, a copy that keeps the mtime (`cp -p`, a new inode), a move away
    and back: none changes the content, each changes what a record's metadata is compared by."""
    cls = probes["ProbeSettingFile"]
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings)
    constraints = root / "c.xdc"
    if change == "chmod":
        constraints.chmod(0o600)
    elif change == "copy":
        shutil.copy2(constraints, root / "c.copy")
        os.replace(root / "c.copy", constraints)
    else:
        constraints.rename(root / "c.moved")
        (root / "c.moved").rename(constraints)
    hashed = _count_hashes(monkeypatch)
    flow, _ = _launch(root, cls, settings)
    assert flow.reused
    assert constraints.resolve() in hashed


@pytest.mark.parametrize("spelling", ["$PWD/map.abc", "$ABC_DIR/map.abc"])
def test_a_file_a_flow_resolves_in_init_is_an_input_before_any_reuse(
    root, tmp_path, monkeypatch, spelling
):
    """yosys expands `abc_script` in `init()` -- against the start directory, or an environment
    variable -- and registers the file it names (`Flow.implicit_inputs`). Launched again with the
    same settings from elsewhere, into the same run directory, it names another file: a new
    input, known before the check, not a reuse of the script the first launch read (ruling
    R39)."""
    from xeda.flows.yosys.common import YosysBase

    class ProbeAbc(YosysBase):
        """A no-tool yosys flow that copies the ABC script it would hand yosys."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self):
            _copy_to_output(self, Path(self.settings.abc_script).read_text())

    first, second = tmp_path / "start1", tmp_path / "start2"
    for start, text in ((first, "strash\n"), (second, "strash; map\n")):
        start.mkdir()
        (start / "map.abc").write_text(text)
    settings = {"abc_script": spelling}
    try:
        for start in (first, second):
            monkeypatch.chdir(start if spelling.startswith("$PWD") else first)
            monkeypatch.setenv("ABC_DIR", str(start))
            flow, out = _launch(root, ProbeAbc, settings)
        assert not flow.reused, "the script resolved from the first start directory was reused"
        assert flow.stale_reason == f"new input: {(second / 'map.abc').resolve()}"
        assert out == "strash; map\n"
        flow, _ = _launch(root, ProbeAbc, settings)
        assert flow.reused
    finally:
        for name in (ProbeAbc.name, ProbeAbc.__name__):
            registered_flows.pop(name, None)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a directory it cannot write"
)
@pytest.mark.parametrize("touched", [False, True], ids=["unchanged", "input touched"])
def test_a_read_only_run_directory_can_still_be_checked(probes, root, touched):
    """A check reads the file-system clock (by writing a marker into the run directory) only to
    refresh the trace, before it reads a file's content. In a directory it cannot write, it
    checks all the same and refreshes nothing: a refresh only saves later hashes, and no record
    is trusted without one (ruling R40)."""
    cls = probes["ProbeSettingFile"]
    settings = {"constraints": "c.xdc"}
    first, _ = _launch(root, cls, settings)
    trace = (first.run_path / "trace.json").read_text()
    if touched:
        os.utime(root / "c.xdc")  # its record can no longer be trusted: the file is read
    first.run_path.chmod(0o555)
    try:
        flow, out = _launch(root, cls, settings)
    finally:
        first.run_path.chmod(0o755)
    assert flow.reused and out == "period 10\n"
    assert (first.run_path / "trace.json").read_text() == trace  # nothing refreshed


PROBED_TOOL = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "$$" > probed.txt  # a journal: different at every probe
    echo "probed-tool 1.2.3"
fi
"""


def test_a_version_probe_leaves_nothing_in_the_run_directory(root, tmp_path, monkeypatch):
    """A tool's version is probed when the flow creates it -- in `init()` too, which runs
    before the check -- and some tools write into their working directory even then (Vivado's
    journal and log). Every file of a managed run directory is an output, so a probe run there
    would change one at every launch: probes run in a temporary directory (ruling R42)."""
    from xeda.tool import Tool

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "probed-tool").write_text(PROBED_TOOL)
    (bin_dir / "probed-tool").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class ProbeVersioned(Flow):
        """Creates its tool in `init()`, as the Vivado flows do."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.tool = Tool("probed-tool")

        def run(self) -> None:
            _copy_to_output(self, "done\n")

    try:
        first, _ = _launch(root, ProbeVersioned)
        assert first.results.tools == [{"executable": "probed-tool", "version": "1.2.3"}]
        assert not (first.run_path / "probed.txt").exists()
        flow, _ = _launch(root, ProbeVersioned)
        assert flow.reused, flow.stale_reason
    finally:
        for name in (ProbeVersioned.name, ProbeVersioned.__name__):
            registered_flows.pop(name, None)


def test_a_record_read_after_its_racy_window_is_refreshed(probes, root, monkeypatch):
    """A file that changed just before its record was taken is read at the next check: its
    metadata cannot vouch for it yet (racy). Once that window has passed, the check that read it
    refreshes the trace, so the check after it reads nothing (ruling R43)."""
    from xeda import digest

    monkeypatch.setattr(digest, "RACY_NS", 300_000_000)  # 0.3 s rather than 2 s, not to wait
    cls = probes["ProbeSource"]
    _launch(root, cls)
    time.sleep(0.5)
    hashed = _count_hashes(monkeypatch)
    flow, _ = _launch(root, cls)
    assert flow.reused
    assert (flow.run_path / "results.json").resolve() in hashed  # written just before its record
    hashed.clear()
    flow, _ = _launch(root, cls)
    assert flow.reused and hashed == [], "a settled record was read again"
