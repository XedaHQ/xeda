"""The design configuration of a node is part of its identity.

A node's identity (`flowrun_hash`, and the suffix of a hashed run directory) is made of its
settings hash, the hash of its design configuration (`flow_runner/configuration.py`) and the
ordered origins of its inputs. The configuration is what the node reads of its design, without
content: for each part the flow reads, its metadata and its sources as `{path, type}` (paths
relative to the design root), and the language. Two designs that differ only in `rtl.top`, or in
a parameter, are two nodes. A design that is edited in a source file, moved, or loaded under a
target label is the same node. The content of a source is evidence of freshness, not identity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Iterable

import pytest
import yaml
from pydantic import Field

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, MetricSpec
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.default_runner import DIR_NAME_HASH_LEN
from xeda.flow_runner.resolver import PlanNode
from xeda.frozen import thaw
from xeda.generation import CACHE_DIRECTORY
from xeda.utils import location_free

from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .io_flows import _Maker
from .test_generator_staleness import World

PART = "LFE5U-25F-6BG381C"

#: The metadata keys of each design part in a configuration.
RTL_KEYS = {"top", "parameters", "defines", "clocks", "attributes"}
TB_KEYS = {"top", "parameters", "defines", "uut", "cocotb"}


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


# ------------------------------------------------------------------------------- the fixtures


def runner(tmp_path: Path, **options: Any) -> DefaultRunner:
    options.setdefault("hashed_run_dirs", True)
    return DefaultRunner(tmp_path / "run", display_results=False, **options)


def verilog_design(
    root: Path, *, top: str = "top", name: str = "d", tb: dict[str, Any] | None = None, **rtl: Any
) -> Design:
    """A design with one Verilog source `top.v` under `root`. `rtl` adds to the `rtl` table."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "top.v").exists():
        (root / "top.v").write_text("module top(input clk); endmodule\n")
    if tb is not None and not (root / "tb.v").exists():
        (root / "tb.v").write_text("module tb(); endmodule\n")
    return Design(
        name=name,
        design_root=root,
        rtl={"sources": ["top.v"], "top": top, "clock": "clk", **rtl},
        **({"tb": {"sources": ["tb.v"], "top": "tb", **tb}} if tb is not None else {}),
    )


def netlist_design(root: Path, top: str) -> Design:
    """A design whose only source is a typed JSON netlist: nextpnr reads it as its netlist."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "n.json").write_text("{}\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": [{"file": "n.json", "type": "JsonNetlist"}], "top": top},
    )


def configuration_of(node: PlanNode) -> dict[str, Any]:
    """The configuration of a plan node as plain tables and lists."""
    return thaw(node.configuration)


def source_reader() -> type[Flow]:
    """Define a flow that reads Verilog and VHDL sources and is judged on Verilog only, unless
    its `wide` setting widens the scope (as the Python hooks of nextpnr do)."""

    class _SourceReader(Flow):
        """Reads the Verilog and VHDL sources of the design, for the tests."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog, SourceType.Vhdl})
        design_source_types = frozenset({SourceType.Verilog})

        class Settings(Flow.Settings):
            wide: bool = Field(False, description="Whether the flow is judged on every source.")

        @classmethod
        def design_scope(cls, settings: Flow.Settings) -> frozenset[SourceType] | None:
            assert isinstance(settings, cls.Settings)
            return None if settings.wide else super().design_scope(settings)

        def run(self) -> None:
            self.results["sources"] = len(self.sources_read())

    return _SourceReader


def mixed_design(root: Path, *vhdl: str) -> Design:
    """A design with a Verilog source, VHDL sources and a constraints file."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("top.v", *vhdl, "c.xdc"):
        if not (root / name).exists():
            (root / name).write_text(f"-- {name}\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.v", *vhdl, "c.xdc"], "top": "top"},
    )


def bluespec_design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Top.bsv").write_text("package Top;\nendpackage\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": ["Top.bsv"], "top": "mkTop", "clock": {"port": "CLK"}},
    )


def converted_chain() -> tuple[type[Flow], type[Flow], str]:
    """Define a converter and a consumer of its Verilog. Return both and the chain that joins
    them."""
    converter, *_ = converter_flows()
    consumer = source_reader()
    return converter, consumer, f"{converter.name}+{consumer.name}"


def assert_identity_formula(nodes: Iterable[PlanNode]) -> None:
    """The identity of every node is made of the three parts in one formula."""
    from xeda.flow_runner.bindings import node_identity
    from xeda.flow_runner.configuration import configuration_hash

    nodes = list(nodes)
    assert nodes
    for node in nodes:
        assert node.flowrun_hash == node_identity(
            node.settings_hash, configuration_hash(node.configuration), node.origins
        ), node.name


# ------------------------------------------------------------------------ the identity cases


def test_two_tops_on_one_netlist_give_two_hashed_directories(tmp_path):
    """A flow that reads its netlist through a declared input still reads the top of the design:
    designs that differ only in `rtl.top` are two nodes, with two run directories."""
    launcher = runner(tmp_path)
    settings = [f"fpga.part={PART}"]
    first = launcher.plan("nextpnr", netlist_design(tmp_path / "d", "a"), flow_settings=settings)
    second = launcher.plan("nextpnr", netlist_design(tmp_path / "d", "b"), flow_settings=settings)
    again = launcher.plan("nextpnr", netlist_design(tmp_path / "d", "a"), flow_settings=settings)

    one, two, same = first.node("nextpnr"), second.node("nextpnr"), again.node("nextpnr")
    assert (one.settings_hash, one.origins) == (two.settings_hash, two.origins)
    assert configuration_of(one)["rtl"]["top"] == "a"
    assert configuration_of(two)["rtl"]["top"] == "b"
    assert one.flowrun_hash != two.flowrun_hash
    assert one.run_path != two.run_path
    assert (same.flowrun_hash, same.run_path) == (one.flowrun_hash, one.run_path)
    assert one.run_path.name == f"nextpnr_{one.flowrun_hash[:DIR_NAME_HASH_LEN]}"
    for plan in (first, second):
        assert_identity_formula(plan.nodes)


def test_a_source_edit_moves_no_directory(tmp_path):
    """The content of a source is evidence of freshness. It is no part of the identity."""
    launcher = runner(tmp_path)
    design = verilog_design(tmp_path / "d")
    before = launcher.plan("yosys", design).node("yosys")

    with open(tmp_path / "d" / "top.v", "a") as source:
        source.write("// an edit\n")
    after = launcher.plan("yosys", verilog_design(tmp_path / "d")).node("yosys")

    assert after.flowrun_hash == before.flowrun_hash
    assert after.run_path == before.run_path


def test_a_parameter_change_moves_the_hashed_directory(tmp_path):
    launcher = runner(tmp_path)
    plans = [
        launcher.plan("yosys", verilog_design(tmp_path / "d", parameters={"W": width})).node(
            "yosys"
        )
        for width in (8, 9, 8)
    ]
    narrow, wide, narrow_again = plans

    assert (narrow.settings_hash, narrow.origins) == (wide.settings_hash, wide.origins)
    assert configuration_of(narrow)["rtl"]["parameters"] == {"W": 8}
    assert configuration_of(wide)["rtl"]["parameters"] == {"W": 9}
    assert narrow.flowrun_hash != wide.flowrun_hash
    assert narrow.run_path != wide.run_path
    assert narrow_again.run_path == narrow.run_path


def test_planned_and_executed_identities_match_for_a_converted_design(tmp_path):
    """The converter runs before its consumer is built. The consumer's identity is known at
    planning, and the run, `results.json` and the trace carry the same one."""
    converter, consumer, chain = converted_chain()
    launcher = runner(tmp_path)
    design = bluespec_design(tmp_path / "d")

    plan = launcher.plan(parse_request(chain), design)
    node = plan.node(consumer.name)
    configuration = configuration_of(node)
    assert [n.name for n in plan.nodes] == [converter.name, consumer.name]
    assert configuration["converted_from"] == converter.name
    assert configuration["transforms"] == ["Bluespec"]
    assert configuration["produces"] == ["Verilog"]
    assert configuration["rtl"]["top"] == "mkTop"
    assert "sources" not in configuration["rtl"]
    assert "converted_from" not in configuration_of(plan.node(converter.name))

    launcher.run(parse_request(chain), design)
    flow = launcher.launched[-1]
    assert flow.name == consumer.name and flow.succeeded
    results = json.loads((flow.run_path / "results.json").read_text())
    trace = json.loads((flow.run_path / "trace.json").read_text())
    assert flow.flow_hash == node.flowrun_hash == results["identity"] == trace["flowrun_hash"]
    assert flow.run_path == node.run_path
    assert results["configuration"] == json.loads(json.dumps(configuration))
    assert_identity_formula(plan.nodes)

    # a dry run judges the consumer under the identity of the plan, not one of the derived design
    again, freshness = launcher.plan_with_freshness(parse_request(chain), design)
    assert again.node(consumer.name).flowrun_hash == node.flowrun_hash
    assert freshness[consumer.name].state == "fresh"


def test_one_design_in_two_directories_has_one_identity(tmp_path):
    """The configuration holds paths relative to the design root: a copy of the design tree in
    another directory (a laptop and a CI checkout) is the same node."""

    def design(root: Path) -> Design:
        (root / "rtl").mkdir(parents=True)
        (root / "rtl" / "top.v").write_text("module top(input clk); endmodule\n")
        (root / "rom.mem").write_text("00\n")
        return Design(
            name="d",
            design_root=root,
            rtl={
                "sources": ["rtl/top.v"],
                "top": "top",
                "parameters": {"ROM": str(root / "rom.mem"), "W": 8},
            },
        )

    one, other = design(tmp_path / "one" / "d"), design(tmp_path / "elsewhere" / "deeper" / "d")
    here = DefaultRunner(tmp_path / "a", hashed_run_dirs=True).plan("yosys", one).node("yosys")
    there = DefaultRunner(tmp_path / "b", hashed_run_dirs=True).plan("yosys", other).node("yosys")

    assert here.flowrun_hash == there.flowrun_hash
    assert here.run_path.name == there.run_path.name
    assert configuration_of(here) == configuration_of(there)
    assert configuration_of(here)["rtl"]["sources"] == [{"path": "rtl/top.v", "type": "Verilog"}]
    assert configuration_of(here)["rtl"]["parameters"]["ROM"] == "$DESIGN_ROOT/rom.mem"
    assert str(tmp_path) not in json.dumps(configuration_of(here))


def test_an_old_hashed_directory_is_left_alone(tmp_path):
    """A run root from before the design was part of the identity holds hashed directories under
    the old names. A launch makes its directory beside them and does not touch them."""
    design = verilog_design(tmp_path / "d")
    launcher = runner(tmp_path)
    old = launcher.run_root / "d" / "__maker_0123456789abcdef"
    old.mkdir(parents=True)
    (old / "results.json").write_text('{"old": true}\n')
    (old / "made.txt").write_text("from an older xeda\n")
    snapshot = {path: path.read_bytes() for path in sorted(old.iterdir())}

    flow = launcher.run(_Maker, design)

    assert flow is not None and flow.succeeded
    assert flow.run_path != old and flow.run_path.parent == old.parent
    assert {path: path.read_bytes() for path in sorted(old.iterdir())} == snapshot

    again = runner(tmp_path).run(_Maker, design)
    assert again is not None and again.reused and again.run_path == flow.run_path
    assert {path: path.read_bytes() for path in sorted(old.iterdir())} == snapshot


# --------------------------------------------------------------------- the shape of a configuration


def test_a_flow_that_reads_only_the_rtl_gets_no_testbench(tmp_path):
    design = verilog_design(tmp_path / "d", tb={})
    configuration = configuration_of(runner(tmp_path).plan("yosys", design).node("yosys"))

    assert set(configuration) == {"rtl", "language"}
    assert set(configuration["rtl"]) == RTL_KEYS | {"sources"}
    assert configuration["rtl"]["top"] == "top"
    assert configuration["rtl"]["sources"] == [{"path": "top.v", "type": "Verilog"}]
    json.dumps(configuration)


def test_a_flow_that_reads_the_testbench_gets_its_metadata_and_sources(tmp_path):
    design = verilog_design(tmp_path / "d", tb={"uut": "dut"})
    plan = runner(tmp_path).plan("verilator", design)
    configuration = configuration_of(plan.node("verilator"))

    assert set(configuration) == {"rtl", "tb", "language"}
    assert set(configuration["rtl"]) == RTL_KEYS | {"sources"}
    assert set(configuration["tb"]) == TB_KEYS | {"sources"}
    assert configuration["tb"]["top"] == ["tb"]
    assert configuration["tb"]["uut"] == "dut"
    assert configuration["tb"]["cocotb"] is None
    assert configuration["tb"]["sources"] == [{"path": "tb.v", "type": "Verilog"}]
    json.dumps(configuration)


def test_a_node_without_a_design_input_has_the_metadata_and_no_sources(tmp_path):
    """nextpnr reads a netlist, not the design's sources: no `sources`, but the metadata and the
    language still count."""
    plan = runner(tmp_path).plan(
        "nextpnr", netlist_design(tmp_path / "d", "a"), flow_settings=[f"fpga.part={PART}"]
    )
    configuration = configuration_of(plan.node("nextpnr"))

    assert set(configuration) == {"rtl", "language"}
    assert set(configuration["rtl"]) == RTL_KEYS


def test_the_clocks_are_complete_and_in_order(tmp_path):
    launcher = runner(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk); endmodule\n")
    clocks = [{"port": "clk"}, {"port": "clk2", "name": "core"}]

    def plan(order):
        design = Design(
            name="d", design_root=root, rtl={"sources": ["top.v"], "top": "top", "clocks": order}
        )
        return launcher.plan("yosys", design).node("yosys")

    forward, backward = plan(clocks), plan(list(reversed(clocks)))

    assert configuration_of(forward)["rtl"]["clocks"] == [
        {"port": "clk", "name": "clk"},
        {"port": "clk2", "name": "core"},
    ]
    assert configuration_of(backward)["rtl"]["clocks"][0]["port"] == "clk2"
    assert forward.flowrun_hash != backward.flowrun_hash


def test_a_clock_written_in_full_or_by_its_port_is_one_configuration(tmp_path):
    launcher = runner(tmp_path)
    short = launcher.plan("yosys", verilog_design(tmp_path / "d")).node("yosys")
    root = tmp_path / "d"
    full = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top", "clocks": [{"port": "clk", "name": "clk"}]},
    )
    spelled_out = launcher.plan("yosys", full).node("yosys")

    assert configuration_of(short)["rtl"]["clocks"] == [{"port": "clk", "name": "clk"}]
    assert configuration_of(short) == configuration_of(spelled_out)
    assert short.flowrun_hash == spelled_out.flowrun_hash


@pytest.mark.parametrize("cocotb", [True, {}, {"module": None, "toplevel": None, "testcase": []}])
def test_the_cocotb_values_are_the_validated_ones_whatever_the_spelling(tmp_path, cocotb):
    launcher = runner(tmp_path)
    plain = launcher.plan("verilator", verilog_design(tmp_path / "d", tb={})).node("verilator")
    design = verilog_design(tmp_path / "d", tb={"cocotb": cocotb})
    node = launcher.plan("verilator", design).node("verilator")

    assert configuration_of(node)["tb"]["cocotb"] == {
        "module": None,
        "toplevel": None,
        "testcase": [],
    }
    assert configuration_of(plain)["tb"]["cocotb"] is None
    assert node.flowrun_hash != plain.flowrun_hash


@pytest.mark.parametrize("vhdl", ["08", {"standard": "08"}, {"version": "08"}])
def test_the_language_values_are_the_validated_ones_whatever_the_spelling(tmp_path, vhdl):
    launcher = runner(tmp_path)
    root = tmp_path / "d"
    plain = launcher.plan("yosys", verilog_design(root)).node("yosys")
    design = verilog_design(root)
    design = Design(
        name="d", design_root=root, rtl=design.rtl.model_dump(), language={"vhdl": vhdl}
    )
    node = launcher.plan("yosys", design).node("yosys")

    assert configuration_of(plain)["language"] == {
        "vhdl": {"standard": None, "synopsys": False},
        "verilog": {"standard": None},
    }
    assert configuration_of(node)["language"] == {
        "vhdl": {"standard": "08", "synopsys": False},
        "verilog": {"standard": None},
    }
    assert node.flowrun_hash != plain.flowrun_hash


def test_a_source_is_its_path_from_the_design_root_and_its_type_in_order(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "ext.v").write_text("module ext(); endmodule\n")
    root = tmp_path / "d" / "design"
    (root / "rtl").mkdir(parents=True)
    for name in ("rtl/top.v", "rtl/pkg.sv"):
        (root / name).write_text("// source\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": [
                "rtl/top.v",
                str(lib / "ext.v"),
                {"file": "rtl/pkg.sv", "type": "SystemVerilog"},
            ],
            "top": "top",
        },
    )

    configuration = configuration_of(runner(tmp_path).plan("yosys", design).node("yosys"))

    assert configuration["rtl"]["sources"] == [
        {"path": "rtl/top.v", "type": "Verilog"},
        {"path": "../../lib/ext.v", "type": "Verilog"},
        {"path": "rtl/pkg.sv", "type": "SystemVerilog"},
    ]


def test_the_sources_are_the_ones_in_the_scope_of_the_flow(tmp_path):
    """The scope is `Flow.design_scope(settings)`, a method: a setting can widen it."""
    reader = source_reader()
    launcher = runner(tmp_path)
    design = mixed_design(tmp_path / "d", "pkg.vhd")

    narrow = launcher.plan(reader, design).node(reader.name)
    wide = launcher.plan(reader, design, flow_settings=["wide=true"]).node(reader.name)

    assert configuration_of(narrow)["rtl"]["sources"] == [{"path": "top.v", "type": "Verilog"}]
    assert configuration_of(wide)["rtl"]["sources"] == [
        {"path": "top.v", "type": "Verilog"},
        {"path": "pkg.vhd", "type": "Vhdl"},
        {"path": "c.xdc", "type": "Xdc"},
    ]

    # a source outside the scope changes nothing for the narrow flow, and something for the wide
    renamed = mixed_design(tmp_path / "d", "other.vhd")
    assert launcher.plan(reader, renamed).node(reader.name).flowrun_hash == narrow.flowrun_hash
    assert (
        launcher.plan(reader, renamed, flow_settings=["wide=true"]).node(reader.name).flowrun_hash
        != wide.flowrun_hash
    )


def test_a_source_edit_changes_no_configuration(tmp_path):
    reader = source_reader()
    launcher = runner(tmp_path)
    before = launcher.plan(reader, mixed_design(tmp_path / "d")).node(reader.name)
    (tmp_path / "d" / "top.v").write_text("module top(input clk); wire edited; endmodule\n")
    after = launcher.plan(reader, mixed_design(tmp_path / "d")).node(reader.name)

    assert configuration_of(after) == configuration_of(before)
    assert after.flowrun_hash == before.flowrun_hash
    assert "sha" not in json.dumps(configuration_of(after))


def test_a_converted_input_has_the_conversion_and_no_sources(tmp_path):
    converter, consumer, chain = converted_chain()
    plan = runner(tmp_path).plan(parse_request(chain), bluespec_design(tmp_path / "d"))

    as_read = configuration_of(plan.node(consumer.name))
    as_converted = configuration_of(plan.node(converter.name))

    assert set(as_read) == {"rtl", "language", "converted_from", "transforms", "produces"}
    assert set(as_read["rtl"]) == RTL_KEYS
    assert as_read["converted_from"] == converter.name
    # the converter reads the sources as they are
    assert as_converted["rtl"]["sources"] == [{"path": "Top.bsv", "type": "Bluespec"}]
    assert "converted_from" not in as_converted
    # the consumer's identity moves with the converter's, and with the conversion it declares
    assert plan.node(consumer.name).origins == (
        ("design", ((plan.node(converter.name).flowrun_hash, "design"),)),
    )


def test_a_design_with_a_target_and_the_same_design_without_it_have_one_configuration(tmp_path):
    base = {
        "name": "d",
        "rtl": {"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    }
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "top.v").write_text("module top(input clk); endmodule\n")
    (tmp_path / "d" / "alt.v").write_text("module alt(input clk); endmodule\n")
    folded = {"top": "alt", "defines": {"A": 1}}
    with_target = tmp_path / "d" / "with_target.yaml"
    with_target.write_text(yaml.safe_dump({**base, "targets": {"fast": folded}}, sort_keys=False))
    flat = tmp_path / "d" / "flat.yaml"
    flat.write_text(
        yaml.safe_dump(
            {**base, "rtl": {**base["rtl"], "top": "alt", "defines": {"A": 1}}}, sort_keys=False
        )
    )
    launcher = runner(tmp_path)

    targeted = launcher.plan("yosys", Design.from_file(with_target, target="fast")).node("yosys")
    plain = launcher.plan("yosys", Design.from_file(flat)).node("yosys")

    assert targeted.run_path.parent.name == "fast" and plain.run_path.parent.name == "d"
    assert configuration_of(targeted) == configuration_of(plain)
    assert targeted.flowrun_hash == plain.flowrun_hash
    text = json.dumps(configuration_of(targeted))
    for never in ("fast", "target", str(tmp_path), "updates"):
        assert never not in text, never


def test_the_configuration_is_immutable(tmp_path):
    node = runner(tmp_path).plan("yosys", verilog_design(tmp_path / "d")).node("yosys")

    with pytest.raises(TypeError):
        node.configuration["rtl"]["top"] = "other"  # type: ignore[index]
    assert isinstance(node.configuration["rtl"]["sources"], tuple)
    assert isinstance(node.configuration["rtl"]["clocks"], tuple)


# ------------------------------------------------------------------------------ the formula


def _plan_join(tmp_path):
    from .io_flows import _Join

    return runner(tmp_path).plan(_Join, verilog_design(tmp_path / "d"))


def _plan_fpga_chain(tmp_path):
    return runner(tmp_path).plan(
        parse_request("yosys_fpga+nextpnr"),
        verilog_design(tmp_path / "d", parameters={"W": 4}),
        flow_settings=[f"fpga.part={PART}"],
    )


def _plan_converted(tmp_path):
    _converter, _consumer, chain = converted_chain()
    return runner(tmp_path).plan(parse_request(chain), bluespec_design(tmp_path / "d"))


def _plan_simulation(tmp_path):
    return runner(tmp_path).plan("verilator", verilog_design(tmp_path / "d", tb={}))


def _plan_plain_directories(tmp_path):
    return runner(tmp_path, hashed_run_dirs=False).plan(
        "yosys", verilog_design(tmp_path / "d", parameters={"W": 4})
    )


@pytest.mark.parametrize(
    "make",
    [_plan_join, _plan_fpga_chain, _plan_converted, _plan_simulation, _plan_plain_directories],
    ids=lambda make: make.__name__.removeprefix("_plan_"),
)
def test_every_node_of_a_plan_is_identified_by_the_one_formula(tmp_path, make):
    plan = make(tmp_path)

    assert_identity_formula(plan.nodes)
    for node in plan.nodes:
        assert node.flowrun_hash != node.settings_hash
        assert node.configuration  # a node always has the configuration its design part gives


def two_netlists(root: Path, name: str, top: str = "top") -> Design:
    """A design whose netlist is the file `name`: nextpnr takes it as its netlist."""
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("{}\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": [{"file": name, "type": "JsonNetlist"}], "top": top},
    )


def test_two_netlists_give_two_identities_and_two_hashed_directories(tmp_path):
    """A file that the sources of the design supply to a declared input is part of the origin of
    that input, by its place and type and not by its content: two netlists are two nodes."""
    launcher = runner(tmp_path)
    settings = [f"fpga.part={PART}"]
    nodes = [
        launcher.plan("nextpnr", two_netlists(tmp_path / "d", name), flow_settings=settings).node(
            "nextpnr"
        )
        for name in ("a.json", "b.json", "a.json")
    ]
    first, second, again = nodes

    assert first.origins[0] == ("netlist", ("source", (("a.json", "JsonNetlist"),)))
    assert second.origins[0] == ("netlist", ("source", (("b.json", "JsonNetlist"),)))
    assert first.settings_hash == second.settings_hash
    assert configuration_of(first) == configuration_of(second)
    assert first.flowrun_hash != second.flowrun_hash
    assert first.run_path != second.run_path
    assert (again.flowrun_hash, again.run_path) == (first.flowrun_hash, first.run_path)
    assert_identity_formula([first, second])


def test_the_same_netlist_in_two_checkouts_is_one_identity(tmp_path):
    settings = [f"fpga.part={PART}"]
    here = DefaultRunner(tmp_path / "a", hashed_run_dirs=True).plan(
        "nextpnr", two_netlists(tmp_path / "one" / "d", "n.json"), flow_settings=settings
    )
    there = DefaultRunner(tmp_path / "b", hashed_run_dirs=True).plan(
        "nextpnr", two_netlists(tmp_path / "two" / "deeper" / "d", "n.json"), flow_settings=settings
    )

    assert here.node("nextpnr").origins == there.node("nextpnr").origins
    assert here.node("nextpnr").flowrun_hash == there.node("nextpnr").flowrun_hash
    assert str(tmp_path) not in repr(here.node("nextpnr").origins)


def test_a_netlist_edit_moves_no_identity(tmp_path):
    launcher = runner(tmp_path)
    settings = [f"fpga.part={PART}"]
    before = launcher.plan(
        "nextpnr", two_netlists(tmp_path / "d", "n.json"), flow_settings=settings
    )
    (tmp_path / "d" / "n.json").write_text('{"modules": {}}\n')
    after = launcher.plan("nextpnr", two_netlists(tmp_path / "d", "n.json"), flow_settings=settings)

    assert after.node("nextpnr").flowrun_hash == before.node("nextpnr").flowrun_hash


def test_the_identity_formula_takes_the_three_parts_in_order():
    from xeda.flow_runner.bindings import node_identity

    origins = (("netlist", "source"),)
    base = node_identity("settings", "configuration", origins)

    assert base != node_identity("settings", "other", origins)
    assert base != node_identity("other", "configuration", origins)
    assert base != node_identity("settings", "configuration", ())
    assert base != node_identity("configuration", "settings", origins)
    assert base == node_identity("settings", "configuration", list(origins))


def test_the_configuration_hash_depends_on_the_values_and_not_on_the_key_order():
    from xeda.flow_runner.configuration import configuration_hash
    from xeda.frozen import freeze

    one = {"rtl": {"top": "a", "sources": [{"path": "a.v", "type": "Verilog"}]}, "language": {}}
    reordered = {
        "language": {},
        "rtl": {"sources": [{"type": "Verilog", "path": "a.v"}], "top": "a"},
    }

    assert (
        configuration_hash(one) == configuration_hash(reordered) == configuration_hash(freeze(one))
    )
    assert configuration_hash(one) != configuration_hash({**one, "rtl": {**one["rtl"], "top": "b"}})
    swapped = {"rtl": {"top": "a", "sources": []}, "language": {}}
    assert configuration_hash(one) != configuration_hash(swapped)


def test_the_path_rule_reads_any_mapping(tmp_path):
    """The planned configuration is frozen with read-only tables of more than one kind."""
    roots = [("DESIGN_ROOT", tmp_path)]
    value = MappingProxyType({"rom": str(tmp_path / "rom.mem"), "deep": MappingProxyType({"x": 1})})

    assert location_free(value, roots) == {"rom": "$DESIGN_ROOT/rom.mem", "deep": {"x": 1}}


# ----------------------------------------------------------------------- freshness and records


def _stale_reason(launcher: DefaultRunner, flow: Any, design: Design) -> str:
    plan, freshness = launcher.plan_with_freshness(flow, design)
    verdict = freshness[plan.nodes[-1].name]
    assert verdict.state == "stale", verdict
    return verdict.reason


@pytest.mark.parametrize(
    "change",
    [{"top": "other"}, {"parameters": {"W": 9}}, {"defines": {"FAST": 1}}],
    ids=["top", "parameter", "define"],
)
def test_a_changed_design_configuration_is_the_reason_a_node_runs_again(tmp_path, change):
    """With plain run directories the run directory is the same, and the trace says what moved."""
    launcher = runner(tmp_path, hashed_run_dirs=False)
    first = launcher.run(_Maker, verilog_design(tmp_path / "d", parameters={"W": 8}))
    assert first is not None and first.succeeded
    assert (
        launcher.plan_with_freshness(_Maker, verilog_design(tmp_path / "d", parameters={"W": 8}))[
            1
        ]["__maker"].state
        == "fresh"
    )

    moved = verilog_design(tmp_path / "d", **{"parameters": {"W": 8}, **change})
    reason = _stale_reason(launcher, _Maker, moved)

    assert reason.startswith("the design configuration changed")
    assert "settings changed" not in reason

    # the launch itself gives the reason and runs the node again
    second = runner(tmp_path, hashed_run_dirs=False).run(_Maker, moved)
    assert second is not None and second.succeeded and not second.reused
    assert second.run_path == first.run_path
    assert second.flow_hash != first.flow_hash


def test_a_consumer_reports_the_change_of_its_own_design_configuration(tmp_path):
    """Both flows of the chain read the top of the design. The consumer says so itself, and does
    not only name the producer that ran again."""
    from .io_flows import _Taker

    launcher = runner(tmp_path, hashed_run_dirs=False)
    first = launcher.run(_Taker, verilog_design(tmp_path / "d", top="a"))
    assert first is not None and first.succeeded

    plan, freshness = launcher.plan_with_freshness(_Taker, verilog_design(tmp_path / "d", top="b"))

    assert [node.name for node in plan.nodes] == ["__maker", "__taker"]
    assert freshness["__maker"].reason == "the design configuration changed"
    assert freshness["__taker"].state == "stale"
    assert freshness["__taker"].reason == "the design configuration changed"


def test_a_changed_source_is_not_a_changed_configuration(tmp_path):
    """An edit of the content is judged by the files the trace recorded."""
    reader = source_reader()
    launcher = runner(tmp_path, hashed_run_dirs=False)
    design = mixed_design(tmp_path / "d")
    first = launcher.run(reader, design)
    assert first is not None and first.succeeded

    (tmp_path / "d" / "top.v").write_text("module top(input clk); wire edited; endmodule\n")
    reason = _stale_reason(launcher, reader, mixed_design(tmp_path / "d"))

    assert "configuration" not in reason
    assert "settings changed" not in reason


def test_the_trace_holds_the_hash_of_the_configuration(tmp_path):
    from xeda.flow_runner.configuration import configuration_hash

    reader = source_reader()
    launcher = runner(tmp_path, hashed_run_dirs=False)
    design = mixed_design(tmp_path / "d")
    plan = launcher.plan(reader, design)
    flow = launcher.run(reader, design)
    assert flow is not None and flow.succeeded

    def values(document: Any):
        if isinstance(document, dict):
            for value in document.values():
                yield from values(value)
        elif isinstance(document, list):
            for value in document:
                yield from values(value)
        else:
            yield document

    trace = json.loads((flow.run_path / "trace.json").read_text())
    hashed = configuration_hash(plan.node(reader.name).configuration)
    assert hashed in set(values(trace))
    assert trace["flowrun_hash"] == plan.node(reader.name).flowrun_hash
    assert trace["settings_hash"] == plan.node(reader.name).settings_hash


def test_a_run_record_carries_the_configuration_of_its_node(tmp_path):
    reader = source_reader()
    launcher = runner(tmp_path)
    design = mixed_design(tmp_path / "d", "pkg.vhd")
    plan = launcher.plan(reader, design, flow_settings=["wide=true"])
    flow = launcher.run(reader, design, flow_settings=["wide=true"])
    assert flow is not None and flow.succeeded

    expected = json.loads(json.dumps(configuration_of(plan.node(reader.name))))
    results = json.loads((flow.run_path / "results.json").read_text())

    assert results["configuration"] == expected
    assert thaw(flow.record.configuration) == expected
    assert expected["rtl"]["sources"][1] == {"path": "pkg.vhd", "type": "Vhdl"}


# ------------------------------------------------------------------------------- a generator


def test_a_generated_source_is_listed_by_its_place_and_type_and_not_by_its_bytes(tmp_path):
    """The generator runs when the design is loaded, so the files it wrote are known at planning.
    The configuration lists them by their place under the design root and their type. Another
    generation under the same names is the same node."""
    world = World(tmp_path)
    launcher = DefaultRunner(world.run_root, display_results=False, hashed_run_dirs=True)

    first_design = world.load()
    first_bytes = world.generated.read_bytes()
    first = launcher.plan("yosys", first_design).node("yosys")
    assert world.runs == 1

    (world.root / "spec.txt").write_text("two, and different\n")
    second_design = world.load()
    assert world.runs == 2 and world.generated.read_bytes() != first_bytes
    second = launcher.plan("yosys", second_design).node("yosys")

    expected = [{"path": "gen/top.v", "type": "Verilog"}]
    assert configuration_of(first)["rtl"]["sources"] == expected
    assert configuration_of(second) == configuration_of(first)
    assert second.flowrun_hash == first.flowrun_hash
    assert second.run_path == first.run_path
    assert "generator" not in json.dumps(configuration_of(first))
    assert str(world.run_root / CACHE_DIRECTORY) not in json.dumps(configuration_of(first))
    assert sys.executable not in json.dumps(configuration_of(first))
    assert_identity_formula([first, second])
