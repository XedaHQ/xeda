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
import time
from pathlib import Path
from typing import ClassVar, Optional

import pytest
from pydantic import Field

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow.run_dir import mark_run_dir
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

    classes = (
        ProbeSettingFile,
        ProbeSource,
        ProbeDepfile,
        ProbeScript,
        ProbeRom,
        ProbeOutputPath,
        ProbeProgram,
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


def _launch(root: Path, cls, settings=None, design=None, run_path=None, **launcher):
    runner = DefaultRunner(root.parent / "xeda_run", display_results=False, **launcher)
    flow = runner.launch_flow(cls, design or _design(root), settings or {}, run_path=run_path)
    assert flow.succeeded
    return flow, Path(flow.artifacts.out).read_text()


@pytest.mark.parametrize("backdate", [False, True], ids=["mtime now", "mtime backdated"])
def test_a_setting_file_edited_during_the_run_is_stale_next_time(probes, root, backdate):
    """The trace records the file as it was when the run started, not as the run left it."""
    cls = probes["ProbeSettingFile"]
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings)
    EDIT["setting"] = (root / "c.xdc", "period 5\n", backdate)
    _, out = _launch(root, cls, settings, rebuild="all")
    assert out == "period 10\n"  # what the run read
    flow, out = _launch(root, cls, settings)
    assert not flow.reused, "a result built from the old constraints looked fresh"
    assert flow.stale_reason == f"input changed: {(root / 'c.xdc').resolve()}"
    assert out == "period 5\n"
    flow, _ = _launch(root, cls, settings)
    assert flow.reused  # and once rebuilt from the edited file, it is fresh again


@pytest.mark.parametrize("same_design", [False, True], ids=["new Design", "reused Design"])
def test_a_source_edited_during_the_run_is_stale_next_time(probes, root, same_design):
    cls = probes["ProbeSource"]
    design = _design(root)
    _launch(root, cls, design=design)
    EDIT["source"] = (root / "a.v", "module b; endmodule\n", True)
    _launch(root, cls, design=design if same_design else _design(root), rebuild="all")
    flow, out = _launch(root, cls, design=design if same_design else _design(root))
    assert not flow.reused and out == "module b; endmodule\n"
    assert flow.stale_reason == f"input changed: {(root / 'a.v').resolve()}"


def test_an_include_known_from_the_last_run_edited_during_the_run_is_stale(probes, root):
    """A depfile names its files only after the run; the ones the previous run's depfile named
    are recorded before the run starts, like every expected input."""
    cls = probes["ProbeDepfile"]
    _launch(root, cls)
    EDIT["include"] = (root / "inc.vh", "`define W 8\n", True)
    _launch(root, cls, rebuild="all")
    flow, out = _launch(root, cls)
    assert not flow.reused and out == "`define W 8\n"
    assert flow.stale_reason == f"input changed: {(root / 'inc.vh').resolve()}"


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


def test_cwd_from_the_design_directory_still_tracks_the_design_s_files(probes, root, monkeypatch):
    """`xeda run --cwd` from the design directory makes it the run directory: its constraints
    are still inputs, since the run did not write them."""
    cls = probes["ProbeSettingFile"]
    monkeypatch.chdir(root)
    mark_run_dir(root)  # handed to xeda as a run directory
    settings = {"constraints": "c.xdc"}
    _launch(root, cls, settings, run_path=root)
    (root / "c.xdc").write_text("period 5\n")
    flow, out = _launch(root, cls, settings, run_path=root)
    assert not flow.reused and out == "period 5\n"
    assert flow.stale_reason == f"input changed: {(root / 'c.xdc').resolve()}"
    flow, _ = _launch(root, cls, settings, run_path=root)
    assert flow.reused


def test_cwd_takes_its_lock_inside_the_directory(probes, root, monkeypatch):
    monkeypatch.chdir(root)
    mark_run_dir(root)  # handed to xeda as a run directory
    _launch(root, probes["ProbeSettingFile"], {"constraints": "c.xdc"}, run_path=root)
    assert (root / ".xeda.lock").is_file()
    assert not (root.parent / f"{root.name}.lock").exists()


def test_a_file_valued_design_parameter_is_an_input(probes, root):
    cls = probes["ProbeRom"]

    def design() -> Design:
        return _design(root, parameters={"ROM": {"file": "rom.mem"}})

    _launch(root, cls, design=design())
    (root / "rom.mem").write_text("ff\n")
    flow, out = _launch(root, cls, design=design())
    assert not flow.reused and out == "ff\n"
    assert flow.stale_reason == f"input changed: {(root / 'rom.mem').resolve()}"


def test_an_output_named_by_a_setting_outside_the_run_directory_is_fresh_next_time(probes, root):
    """The run wrote it: it is the run's own output, not a new input of the next launch."""
    cls = probes["ProbeOutputPath"]
    netlist = root.parent / "elsewhere" / "net.v"
    settings = {"netlist": str(netlist)}
    first, _ = _launch(root, cls, settings)
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert str(netlist.resolve()) in trace["outputs"]
    assert str(netlist.resolve()) not in trace["inputs"]
    flow, _ = _launch(root, cls, settings)
    assert flow.reused
    netlist.write_text("edited by hand\n")
    flow, _ = _launch(root, cls, settings)
    assert not flow.reused and flow.stale_reason == f"output changed: {netlist.resolve()}"


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
