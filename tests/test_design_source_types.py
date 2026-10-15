"""`Flow.design_source_types`: a flow is judged on the sources of the types it reads.

A flow that takes its inputs through declarations (`nextpnr`, `fpga_pack`, `openfpgaloader`,
`vivado_power`) needs no HDL source. Its identity (`design_hash`) and the design files its trace
records cover the sources of the types it can take, in their order, and the design's metadata
(top, parameters, defines, clocks, language). An edit to a Verilog file that only its producer
reads then leaves it alone, and an edit to a pin file it reads moves it.

The risk is one-sided, as for `design_parts`: a type wrongly left out is a stale reuse. So a
mechanical check ties the declaration to what the flow can read, and the guards that protect the
user's files (read inputs, deliveries) keep seeing the whole design.
"""

import json
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow
from xeda.flow.flow import registered_flows
from xeda.flow.io import declared_inputs
from xeda.flow_runner.trace_inputs import design_files

from .settings_samples import flow_classes
from .test_declared_fpga_flows import PART, tools  # noqa: F401  (the stand-in tools fixture)
from .test_source_contracts import python_source_reads
from .tool_utils import producers_of

RTL = frozenset({"rtl"})
PINS = frozenset({SourceType.Lpf})

#: the flows judged on a narrowed set of source types, and the types they take
NARROWED = {
    "nextpnr": {
        SourceType.JsonNetlist,
        SourceType.Lpf,
        SourceType.Pcf,
        SourceType.Pdc,
        SourceType.Xdc,
        SourceType.Sdc,
    },
    "fpga_pack": {SourceType.EcpConfig, SourceType.IceAsc, SourceType.Fasm},
    "openfpgaloader": {SourceType.Bitstream},
    "openroad_place": {SourceType.Sdc, SourceType.VerilogNetlist},
    "vivado_power": {SourceType.Saif, SourceType.RoutedCheckpoint},
}


def _flow(name: str) -> type[Flow]:
    return registered_flows[name][1]


def _design(root: Path, verilog: str = "module a; endmodule\n", lpf: str = "LOCATE a;\n") -> Design:
    """A Verilog source, two pin files and a Verilog netlist-free design."""
    root.mkdir(exist_ok=True)
    (root / "a.v").write_text(verilog)
    (root / "one.lpf").write_text(lpf)
    (root / "two.lpf").write_text("LOCATE b;\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": ["a.v", "one.lpf", "two.lpf"], "top": "a"},
    )


# ------------------------------------------------------------------------------ the declaration


def test_no_flow_is_narrowed_by_default() -> None:
    assert Flow.design_source_types is None
    assert {name for cls, name in flow_classes() if cls.design_source_types is not None} == set(
        NARROWED
    )


@pytest.mark.parametrize("name", sorted(NARROWED))
def test_a_narrowed_flow_declares_the_types_it_takes(name: str) -> None:
    assert _flow(name).design_source_types == NARROWED[name]


@pytest.mark.parametrize("name", sorted(NARROWED))
def test_the_declaration_holds_everything_the_flow_can_take_from_the_design(name: str) -> None:
    """A type a flow can take as an input, or hand its tool, is in its scope: otherwise an edit
    to such a source, or to the order of two, would reuse a result built from the old one."""
    cls = _flow(name)
    scope = cls.design_source_types
    assert scope is not None
    reads = {t for d in declared_inputs(cls).values() for t in d.types} | set(
        cls.reads_sources or ()
    )
    assert (
        reads <= scope
    ), f"{name} can read {sorted(map(str, reads - scope))} but is not judged on it"


@pytest.mark.parametrize("name", sorted(NARROWED))
def test_a_narrowed_flow_reads_no_source_in_its_code_or_templates(name: str) -> None:
    """The converse: a flow with a declared scope that read the design's sources by itself, in
    code or in a template, would read what its identity does not cover."""
    from .test_source_contracts import source_reads

    cls = _flow(name)
    assert cls.reads_sources is None
    assert source_reads(cls) == []


def test_the_scan_finds_a_flow_that_reads_a_source_of_its_own() -> None:
    reads = python_source_reads(
        "class F:\n    def run(self):\n        return self.design.rtl.sources\n"
    )
    assert reads, "a read of rtl.sources is not found"


# ------------------------------------------------------------------------------- the helpers


def test_a_hash_scoped_to_types_ignores_the_other_sources_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    before = _design(tmp_path)
    plain = before.parts_hash(RTL)
    scoped = before.parts_hash(RTL, PINS)
    assert scoped != plain
    # the Verilog file is not part of it, the pin file is, and so is the order of two pin files
    verilog = _design(tmp_path, verilog="module a; wire x; endmodule\n")
    assert verilog.parts_hash(RTL, PINS) == scoped
    assert verilog.parts_hash(RTL) != plain
    pins = _design(tmp_path, lpf="LOCATE changed;\n")
    assert pins.parts_hash(RTL, PINS) != scoped
    other = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["a.v", "two.lpf", "one.lpf"], "top": "a"},
    )
    assert other.parts_hash(RTL, PINS) != _design(tmp_path).parts_hash(RTL, PINS)
    # the design's metadata is whole
    moved = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["a.v", "one.lpf", "two.lpf"], "top": "other"},
    )
    assert moved.parts_hash(RTL, PINS) != _design(tmp_path).parts_hash(RTL, PINS)


def test_no_scope_is_the_hash_a_design_always_had(tmp_path: Path) -> None:
    design = _design(tmp_path)
    assert design.parts_hash(RTL, None) == design.parts_hash(RTL)


def test_design_files_follow_the_scope_and_the_guards_keep_the_whole_design(
    tmp_path: Path,
) -> None:
    design = _design(tmp_path)
    verilog, one, two = ((tmp_path / n).resolve() for n in ("a.v", "one.lpf", "two.lpf"))
    assert design_files(design, RTL, PINS) == [one, two]
    assert design_files(design, RTL) == [verilog, one, two]
    assert design_files(design) == [verilog, one, two]


# ---------------------------------------------------------------------------------- launches


def test_nextpnr_is_not_judged_on_the_verilog_its_producer_reads(
    tmp_path: Path, monkeypatch, tools  # noqa: F811
) -> None:
    from xeda.flow_runner import DefaultRunner
    from xeda.flows import Nextpnr

    root = tmp_path / "d"
    design = _design(root)
    design = Design(
        name="blink",
        design_root=root,
        rtl={"sources": ["a.v", "one.lpf"], "top": "a"},
    )
    monkeypatch.chdir(tmp_path)

    def launch():
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        return runner.run_flow(Nextpnr, design, {"fpga": {"part": PART}}), runner

    first, _ = launch()
    trace = json.loads((first.run_path / "trace.json").read_text())
    recorded = {Path(p).name for p in {**trace["inputs"], **trace["implicit_inputs"]}}
    assert "a.v" not in recorded and "one.lpf" in recorded

    (root / "a.v").write_text("module a; wire edited; endmodule\n")
    design = Design(name="blink", design_root=root, rtl={"sources": ["a.v", "one.lpf"], "top": "a"})
    second, runner = launch()
    assert second.design_hash == first.design_hash
    (yosys,) = producers_of(runner, second)
    assert not yosys.reused, "the edit re-runs the producer"
    assert "a.v" not in (second.stale_reason or ""), second.stale_reason

    (root / "one.lpf").write_text("LOCATE edited;\n")
    design = Design(name="blink", design_root=root, rtl={"sources": ["a.v", "one.lpf"], "top": "a"})
    third, _ = launch()
    assert third.design_hash != second.design_hash
    assert not third.reused


def test_a_nextpnr_hook_is_judged_on_every_source(
    tmp_path: Path, monkeypatch, tools  # noqa: F811
) -> None:  # noqa: F811
    """A Python hook can read any source of the design (a placement table, typed `Data`): with
    one, nextpnr is judged on the whole parts, and an edit to such a file moves it."""
    from xeda.flow_runner import DefaultRunner
    from xeda.flows import Nextpnr

    settings = Nextpnr.Settings.from_input({"fpga": {"part": PART}})
    assert Nextpnr.design_scope(settings) == NARROWED["nextpnr"]
    for hook in ({"pre_place": "hook.py"}, {"extra_args": ["--run", "hook.py"]}):
        hooked = Nextpnr.Settings.from_input({"fpga": {"part": PART}, **hook})
        assert Nextpnr.design_scope(hooked) is None

    root = tmp_path / "d"
    root.mkdir()
    (root / "a.v").write_text("module a; endmodule\n")
    (root / "hook.py").write_text("# a hook\n")
    (root / "table.txt").write_text("a 1\n")
    monkeypatch.chdir(tmp_path)

    def launch(**hook):
        design = Design(
            name="d",
            design_root=root,
            rtl={
                "sources": ["a.v", {"file": "table.txt", "type": "Data"}],
                "top": "a",
            },
        )
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        flow = runner.run_flow(Nextpnr, design, {"fpga": {"part": PART}, **hook})
        assert flow is not None and flow.succeeded
        return flow

    for hook in ({}, {"pre_place": str(root / "hook.py")}):
        first = launch(**hook)
        (root / "table.txt").write_text(first.design_hash[:8] + "\n")  # a new table
        second = launch(**hook)
        assert (second.design_hash != first.design_hash) is bool(hook), hook
