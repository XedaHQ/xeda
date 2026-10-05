"""Preparation happens under input leases, before freshness, including reused launches."""

import json
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner, default_runner
from xeda.dataclass import Field
from xeda.proc_utils import run_process
from xeda.flows import xilinx

from . import tool_utils
from .io_flows import _Taker, _Wrapper
from .test_read_locks import _probe
from .test_xilinx_chipdb import _binary, generation as chipdb_generation, prefix  # noqa: F401

# Expose the shared fixture under its dependency name without shadowing an import.
generation = chipdb_generation


class _ChipdbTaker(_Taker):
    """Exercise shared cache preparation without activating the Xilinx nextpnr target."""

    class Settings(_Taker.Settings):
        nextpnr: Path | None = Field(None, description="The installed nextpnr binary.")
        chipdb: Path | None = Field(None, description="An explicit read-only chip database.")

    def prepare_inputs(self):
        assert self.settings.nextpnr is not None
        assert self.inputs.made.read_text() == "made\n"
        assert _probe(self.inputs.made.parent) == "blocked"
        layout = xilinx.find_xilinx_layout(self.settings.nextpnr, chipdb=self.settings.chipdb)
        selection = xilinx.select_xilinx("xc7a100tcsg324-1", layout)
        self.chipdb = xilinx.prepare_chipdb(layout, selection, self.run_directory)
        self.implicit_inputs.append(self.chipdb)

    def run(self):
        super().run()
        self.results["chipdb"] = str(self.chipdb)
        run_process(sys.executable, ["-c", "pass"])


def _launch_cache(tmp_path, generation, *, name="d", settings=None):
    design = Design(name=name, design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(generation[1].run_root, display_results=False)
    return runner.launch_flow(
        _ChipdbTaker,
        design,
        {"nextpnr": generation[0] / "bin/nextpnr-himbaechel", **(settings or {})},
    )


def test_cache_preparation_is_snapshotted_and_second_launch_is_fresh(
    tmp_path, generation, monkeypatch
):
    original = default_runner.snapshot_inputs
    snapshots = []

    def snapshot(expected, *args):
        result = original(expected, *args)
        if expected.flow == _ChipdbTaker.name:
            chipdb = next(p for p in result.records if p.endswith("xc7a100t.bin"))
            assert Path(chipdb).is_file() and not result.records[chipdb].unknown
            snapshots.append(chipdb)
        return result

    monkeypatch.setattr(default_runner, "snapshot_inputs", snapshot)
    first = _launch_cache(tmp_path, generation)
    # A freshness check can refresh trace metadata once input timestamps settle.
    # Preparation must leave the actual outputs and result/settings documents alone.
    before = tool_utils.run_outputs_state(first.run_path)
    tool_utils.check_after_the_racy_window(monkeypatch)  # the check refreshes the trace
    again = _launch_cache(tmp_path, generation)
    assert again.reused
    assert len(snapshots) == 1
    assert tool_utils.run_outputs_state(first.run_path) == before
    other = _launch_cache(tmp_path, generation, name="other")
    assert other.results["chipdb"] == first.results["chipdb"]
    assert len(generation[3].read_text().splitlines()) == 1
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert list(trace["programs"]) == [sys.executable, str(generation[0] / "bin/bbasm")]
    assert all(not p["file"]["sha"].startswith("unknown:") for p in trace["programs"].values())
    hit_trace = json.loads((other.run_path / "trace.json").read_text())
    assert list(hit_trace["programs"]) == [sys.executable]  # execution only; no generator on hit


def test_content_change_selects_new_cache_and_invalidates_reuse(tmp_path, generation):
    first = _launch_cache(tmp_path, generation)
    (generation[0] / "share/nextpnr/himbaechel/uarch/xilinx/constids.inc").write_text("new content")
    second = _launch_cache(tmp_path, generation)
    assert not second.reused
    assert second.results["chipdb"] != first.results["chipdb"]
    assert Path(first.results["chipdb"]).is_file()
    assert len(generation[3].read_text().splitlines()) == 2


def test_explicit_chipdb_content_change_invalidates_consumer(tmp_path, generation):
    chipdb = tmp_path / "explicit.bin"
    chipdb.write_bytes(_binary())
    first = _launch_cache(tmp_path, generation, settings={"chipdb": chipdb})
    assert _launch_cache(tmp_path, generation, settings={"chipdb": chipdb}).reused
    content = bytearray(chipdb.read_bytes())
    content[8] ^= 1
    chipdb.write_bytes(content)
    assert not _launch_cache(tmp_path, generation, settings={"chipdb": chipdb}).reused
    assert not generation[3].exists()
    assert first.results.success


def test_cache_failure_after_success_records_preparation_cause(tmp_path, generation):
    first = _launch_cache(tmp_path, generation)
    (generation[0] / "share/nextpnr/himbaechel/uarch/xilinx/constids.inc").write_text("new content")
    generation[2].write_text("partial")
    with pytest.raises(FlowFatalError, match="generator"):
        _launch_cache(tmp_path, generation)
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False and recorded["error"]["type"] == "FlowFatalError"
    assert (
        "generator" in recorded["error"]["message"]
        and "nextpnr failure" not in recorded["error"]["message"]
    )
    for key in ("design", "flow", "design_hash", "flow_hash", "run_path", "timestamp"):
        assert recorded[key]
    assert not (first.run_path / "trace.json").exists()


@pytest.mark.parametrize("damage", ["header", "digest"])
def test_corrupt_cache_never_starts_consumer(tmp_path, generation, monkeypatch, damage):
    first = _launch_cache(tmp_path, generation)
    chipdb = Path(first.results["chipdb"])
    content = bytearray(chipdb.read_bytes())
    if damage == "header":
        content = bytearray(b"bad header")
    else:
        content[8] ^= 1
    chipdb.write_bytes(content)

    def refuse_run(self):
        pytest.fail("consumer ran with an invalid chip database")

    monkeypatch.setattr(_ChipdbTaker, "run", refuse_run)
    with pytest.raises(FlowFatalError, match="Corrupt Xilinx chipdb cache"):
        _launch_cache(tmp_path, generation)
    assert not (first.run_path / "trace.json").exists()
    assert len(generation[3].read_text().splitlines()) == 1


def test_preparation_program_changed_after_start_is_unknown(tmp_path, generation, monkeypatch):
    original = _ChipdbTaker.run

    def mutate(self):
        assembler = generation[0] / "bin/bbasm"
        assembler.write_text(assembler.read_text() + "\n# changed after preparation\n")
        original(self)

    monkeypatch.setattr(_ChipdbTaker, "run", mutate)
    first = _launch_cache(tmp_path, generation)
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert trace["programs"][str(generation[0] / "bin/bbasm")]["file"]["sha"].startswith("unknown:")
    assert not trace["programs"][sys.executable]["file"]["sha"].startswith("unknown:")


def test_cache_planning_has_no_preparation_or_tools(tmp_path, generation, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("planning prepared the chip database")

    monkeypatch.setattr(xilinx, "prepare_chipdb", fail, raising=False)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    DefaultRunner(generation[1].run_root, display_results=False).plan(
        _ChipdbTaker, design, flow_settings={"nextpnr": generation[0] / "bin/nextpnr-himbaechel"}
    )
    assert not (generation[1].run_root / ".cache").exists()


def test_nextpnr_prepares_chipdb_without_losing_typed_pin_inputs(tmp_path, generation, monkeypatch):
    import xeda.flows.nextpnr as nextpnr_module
    from xeda.flows.nextpnr import Nextpnr

    pins = tmp_path / "pins.xdc"
    pins.write_text("set_property PACKAGE_PIN E3 [get_ports clk]\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [pins.name], "top": "t"})
    flow = Nextpnr(
        Nextpnr.Settings(fpga={"part": "xc7a100tcsg324-1"}),
        design,
        generation[1].path,
        run_directory=generation[1],
    )
    flow.inputs.constraints = [pins]
    flow.inputs.sdc = []
    monkeypatch.setattr(
        nextpnr_module, "which", lambda name: str(generation[0] / "bin" / name), raising=False
    )
    flow.prepare_inputs()
    assert flow._pin_inputs == [pins]
    assert flow._chipdb.is_file() and flow._chipdb in flow.implicit_inputs


@pytest.mark.parametrize("path_style", ["absolute", "relative", "relative_database"])
def test_nextpnr_accepts_validated_explicit_chipdb_without_generation(
    tmp_path, generation, monkeypatch, path_style
):
    import xeda.flows.nextpnr as nextpnr_module
    from xeda.flows.nextpnr import Nextpnr

    chipdb = tmp_path / "explicit.bin"
    chipdb.write_bytes(_binary())
    pins = tmp_path / "pins.xdc"
    pins.write_text("# pins supplied\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [pins.name], "top": "t"})
    flow = Nextpnr(
        Nextpnr.Settings(
            fpga={"part": "xc7a100tcsg324-1"},
            chipdb=chipdb if path_style == "absolute" else Path(chipdb.name),
            prjxray_db=(
                (generation[0] / "share/nextpnr/prjxray-db").relative_to(tmp_path)
                if path_style == "relative_database"
                else None
            ),
        ),
        design,
        tmp_path / "direct",
    )
    flow.inputs.constraints = [pins]
    monkeypatch.setattr(
        nextpnr_module, "which", lambda name: str(generation[0] / "bin" / name), raising=False
    )
    flow.prepare_inputs()
    assert flow._chipdb == chipdb and chipdb in flow.implicit_inputs
    assert not generation[3].exists()


def test_scrubbing_flow_directories_preserves_shared_chipdb(tmp_path, generation, monkeypatch):
    from xeda.console import console

    first = _launch_cache(tmp_path, generation)
    chipdb = Path(first.results["chipdb"])
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in chipdb.parent.iterdir()}
    monkeypatch.setattr(console, "input", lambda *args, **kwargs: "yes")
    assert default_runner.scrub_runs(
        first.name, first.run_path.parent, run_root=generation[1].run_root
    )
    assert not first.run_path.exists()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


def test_program_merge_keeps_first_preparation_state(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.write_text(f"#!{sys.executable}\n# first content\n")
    tool.chmod(0o755)

    def prepare(self):
        run_process(str(tool))
        tool.write_text(f"#!{sys.executable}\n# different content\n")

    def run(self):
        run_process(str(tool))

    monkeypatch.setattr(_Taker, "prepare_inputs", prepare)
    monkeypatch.setattr(_Taker, "run", run)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    first = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(_Taker, design, {})
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert list(trace["programs"]) == [str(tool)]
    assert trace["programs"][str(tool)]["file"]["sha"].startswith("unknown:")


@pytest.mark.parametrize("consumer", [_Taker, _Wrapper])
def test_preparation_sees_handed_over_inputs_before_freshness(tmp_path, monkeypatch, consumer):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    seen = []
    original = default_runner.expectation

    def prepare(self):
        producer = self.completed_dependencies[0]
        assert _probe(producer.run_path) == "blocked"
        if isinstance(self, _Taker):
            assert self.inputs.made.read_text() == "made\n"
        seen.append("prepare")

    def expectation(flow, *args):
        if isinstance(flow, consumer):
            seen.append("expectation")
        return original(flow, *args)

    monkeypatch.setattr(consumer, "prepare_inputs", prepare, raising=False)
    monkeypatch.setattr(default_runner, "expectation", expectation)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(consumer, design, {})
    before = tool_utils.run_outputs_state(first.run_path)
    tool_utils.check_after_the_racy_window(monkeypatch)  # the check refreshes the trace
    again = runner.launch_flow(consumer, design, {})
    assert again.reused
    assert seen == ["prepare", "expectation", "prepare", "expectation"]
    assert tool_utils.run_outputs_state(first.run_path) == before


def test_preparation_failure_invalidates_success_and_releases_lease(tmp_path, monkeypatch):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(_Taker, design, {})

    def fail(self):
        assert _probe(self.inputs.made.parent) == "blocked"
        raise FlowFatalError("broken preparation")

    monkeypatch.setattr(_Taker, "prepare_inputs", fail, raising=False)
    with pytest.raises(FlowFatalError, match="broken preparation"):
        runner.launch_flow(_Taker, design, {})
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False
    assert recorded["error"]["message"] == "broken preparation"
    for key in ("design", "flow", "design_hash", "flow_hash", "run_path", "timestamp"):
        assert recorded[key]
    assert not (first.run_path / "trace.json").exists()
    assert _probe(first.inputs.made.parent) == "free"


def test_planning_does_not_prepare_inputs(tmp_path, monkeypatch):
    def fail(self):
        pytest.fail("dry-run prepared inputs")

    monkeypatch.setattr(_Taker, "prepare_inputs", fail, raising=False)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    DefaultRunner(tmp_path / "run", display_results=False).plan(_Taker, design)
    assert not (tmp_path / "run").exists()


def test_execution_order_keeps_preparation_before_snapshot_and_start(tmp_path, monkeypatch):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    events = []
    from xeda.flow import Flow

    original_expectation = default_runner.expectation
    original_snapshot = default_runner.snapshot_inputs
    original_start = Flow.start_run
    original_run = _Taker.run

    def init(self):
        events.append("init")

    def prepare(self):
        assert self.inputs.made.read_text() == "made\n"
        assert _probe(self.inputs.made.parent) == "blocked"
        events.append("hand-over and prepare")

    def expectation(flow, *args):
        if isinstance(flow, _Taker):
            events.append("expectation")
        return original_expectation(flow, *args)

    def snapshot(expected, *args):
        if expected.flow == _Taker.name:
            events.append("snapshot")
        return original_snapshot(expected, *args)

    def start(self, *args, **kwargs):
        if isinstance(self, _Taker):
            events.append("start")
        return original_start(self, *args, **kwargs)

    def run(self):
        events.append("run")
        return original_run(self)

    monkeypatch.setattr(_Taker, "init", init)
    monkeypatch.setattr(_Taker, "prepare_inputs", prepare)
    monkeypatch.setattr(default_runner, "expectation", expectation)
    monkeypatch.setattr(default_runner, "snapshot_inputs", snapshot)
    monkeypatch.setattr(Flow, "start_run", start)
    monkeypatch.setattr(_Taker, "run", run)
    DefaultRunner(tmp_path / "run", display_results=False).launch_flow(_Taker, design, {})
    assert events == ["init", "hand-over and prepare", "expectation", "snapshot", "start", "run"]
