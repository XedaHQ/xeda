"""A design and everything in it is immutable.

An assignment or an in-place change raises an error. A changed design is a new value, made by
`model_copy(update=...)`. The generator, the cocotb settings, the clocks and the language are read-only
tables that a model validates when the design loads.
"""

from __future__ import annotations

import copy
import pickle
from collections.abc import Mapping
from pathlib import Path

import pydantic
import pytest

from xeda import Design
from xeda import design as design_module
from xeda.design import Rtl, Source, SourceType, cocotb_of, generator_of, language_of
from xeda.flows import GhdlSim
from xeda.frozen import FrozenDict
from xeda.introspect import json_safe
from xeda.utils import dump_json

from .flow_registry import preserved_flow_registry
from .test_converted_designs import Events, consumer_flows, launch
from .test_source_contracts import _calls, _settings
from .test_tcl_paths import needs_tclsh


def _design(root: Path, **extra) -> Design:
    """A design with a generator, a cocotb testbench, two clocks and a language."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("top.v", "tb.py", "gen.py"):
        (root / name).write_text("module top; endmodule\n")
    return Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["top.v", {"path": "made.v"}],
            "top": "top",
            "parameters": {"W": 8, "LIST": [1, 2]},
            "clocks": [{"port": "clk"}, {"port": "clk2", "name": "slow"}],
            "attributes": {"keep": {"top": "true"}},
            "generator": {
                "command": "python gen.py",
                "args": ["a"],
                "sources": [str(root / "gen.py")],
                "generated_sources": ["made.v"],
                "always_runs": True,
            },
        },
        tb={"sources": ["tb.py"], "top": "top", "defines": {"X": 0}, "cocotb": {"module": "tb"}},
        hdl={"vhdl": "08"},
        **extra,
    )


@pytest.fixture
def design(tmp_path, monkeypatch) -> Design:
    # the generator writes `made.v`, so the design loads once the file exists
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "made.v").write_text("module made; endmodule\n")
    monkeypatch.setattr("xeda.design.judging_generation", _skipping_generation)
    return _design(tmp_path / "d")


class _skipping_generation:
    """Stand in for the judgment of a generator: it does not run."""

    def __init__(self, *args, **kwargs):
        self.reason = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def produced(self):
        pass


@pytest.mark.parametrize(
    "assign",
    [
        lambda d: setattr(d, "name", "x"),
        lambda d: setattr(d.rtl, "top", "x"),
        lambda d: setattr(d, "rtl", d.rtl),
        lambda d: setattr(d.tb, "top", ("x",)),
        lambda d: setattr(d.rtl, "parameters", {}),
        lambda d: d.rtl.parameters.__setitem__("W", 9),
        lambda d: d.rtl.parameters["LIST"].append(3),
        lambda d: d.rtl.sources.append(d.rtl.sources[0]),
        lambda d: setattr(d.rtl.sources[0], "type", SourceType.Vhdl),
        lambda d: setattr(d.rtl.sources[0], "generated_name", "x"),
        lambda d: setattr(d.rtl.sources[0], "file", Path("/x")),
        lambda d: d.rtl.attributes["keep"].__setitem__("top", "false"),
        lambda d: d.rtl.clocks.append({}),
        lambda d: d.rtl.clocks[0].__setitem__("port", "x"),
        lambda d: d.tb.defines.__setitem__("X", 1),
        lambda d: d.rtl.generator.__setitem__("command", "x"),
        lambda d: d.rtl.generator["args"].append("x"),
        lambda d: d.tb.cocotb.__setitem__("module", "x"),
        lambda d: d.tb.cocotb["testcase"].append("x"),
        lambda d: d.language.__setitem__("vhdl", {}),
        lambda d: d.language["vhdl"].__setitem__("standard", "93"),
        lambda d: d.flow.__setitem__("ghdl_sim", {}),
        lambda d: setattr(d, "authors", ("x",)),
    ],
)
def test_every_design_value_refuses_mutation(design, assign):
    with pytest.raises((TypeError, AttributeError, pydantic.ValidationError)):
        assign(design)


def test_the_generator_and_cocotb_are_mappings(design):
    assert isinstance(design.rtl.generator, Mapping)
    assert design.rtl.generator["command"] == "python gen.py"
    assert design.rtl.generator["args"] == ("a",)
    assert isinstance(design.tb.cocotb, Mapping) and design.tb.cocotb["module"] == "tb"
    assert isinstance(design.language, Mapping) and design.language["vhdl"]["standard"] == "08"


def test_a_clock_is_a_table_with_a_port_and_a_name(design):
    assert [dict(clock) for clock in design.rtl.clocks] == [
        {"port": "clk", "name": "clk"},
        {"port": "clk2", "name": "slow"},
    ]
    assert all(isinstance(clock, Mapping) for clock in design.rtl.clocks)


def test_the_models_are_built_from_the_tables(design):
    generator = generator_of(design.rtl)
    assert generator is not None and generator.command == "python gen.py"
    assert generator.args == ["a"] and generator.always_runs
    cocotb = cocotb_of(design.tb)
    assert cocotb is not None and cocotb.module == "tb"
    assert language_of(design).vhdl.standard == "08"
    assert cocotb_of(design_module.Testbench()) is None
    assert generator_of(Rtl(sources=())) is None


@pytest.mark.parametrize(
    "rtl, message",
    [
        ({"generator": {"args": 3}}, "args"),
        ({"generator": {"nothing": 1}}, "nothing"),
        ({"generator": {"class": "scala"}}, "unknown generator class"),
        ({"generator": 3}, "command text"),
        ({"generator": ["a", 1]}, "holds only text"),
    ],
)
def test_an_invalid_recipe_fails_when_the_design_loads(tmp_path, rtl, message):
    from xeda.design import DesignValidationError

    with pytest.raises(DesignValidationError, match=message):
        Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", **rtl})
    with pytest.raises(pydantic.ValidationError, match=message):
        Rtl(sources=(), **rtl)


def test_an_invalid_cocotb_recipe_fails_when_the_design_loads(tmp_path):
    from xeda.design import DesignValidationError

    with pytest.raises(DesignValidationError, match="testcase"):
        Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": [], "top": "t"},
            tb={"cocotb": {"testcase": 3}},
        )


def test_a_command_text_and_an_argument_list_stay_as_declared(tmp_path):
    assert Rtl(sources=(), generator="make rtl").generator == "make rtl"
    assert Rtl(sources=(), generator=["make", "rtl"]).generator == ("make", "rtl")


def test_a_changed_design_is_a_copy(design):
    changed = design.model_copy(
        update={"rtl": design.rtl.model_copy(update={"top": "other", "parameters": FrozenDict()})}
    )
    changed.invalidate_cached_properties()
    assert (changed.rtl.top, dict(changed.rtl.parameters)) == ("other", {})
    assert design.rtl.top == "top" and design.rtl.parameters["W"] == 8
    assert changed.rtl_hash != design.rtl_hash


def test_a_design_copies_and_pickles(design):
    for other in (copy.deepcopy(design), pickle.loads(pickle.dumps(design))):
        assert other == design
        assert other.rtl_hash == design.rtl_hash
        assert other.rtl.sources[0].type is design.rtl.sources[0].type


def test_a_design_is_written_as_plain_json(design, tmp_path):
    document = design.model_dump(mode="json")
    assert document["rtl"]["generator"]["args"] == ["a"]
    assert document["rtl"]["clocks"][1] == {"port": "clk2", "name": "slow"}
    assert document["tb"]["cocotb"] == {"module": "tb", "toplevel": None, "testcase": []}
    assert document["language"]["vhdl"]["standard"] == "08"
    written = tmp_path / "design.json"
    dump_json({"rtl": design.rtl.parameters, "language": design.language}, written)
    assert written.read_text().count("FrozenDict") == 0
    assert json_safe(design.rtl.attributes) == {"keep": {"top": "true"}}


def test_the_design_loads_again_from_its_own_dump(design):
    again = Design(
        **{**design.model_dump(), "design_root": design.design_root},
    )
    assert again.rtl_hash == design.rtl_hash and again.tb_hash == design.tb_hash
    assert again.rtl.clocks == design.rtl.clocks and again.language == design.language


def test_a_dependency_is_merged_into_a_design_that_cannot_change(tmp_path):
    root = tmp_path / "d"
    (root / "dep").mkdir(parents=True)
    for name in ("top.v", "dep/lib.v", "dep/lib_tb.v"):
        (root / name).write_text("module m; endmodule\n")
    (root / "dep" / "dep.yaml").write_text(
        "name: dep\nrtl:\n  sources: [lib.v, ../top.v]\n  top: lib\n  parameters: {W: 4}\n"
        "  clocks: [{port: dclk}]\ntb:\n  sources: [lib_tb.v]\n  top: lib_tb\n"
    )
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.v"]},
        dependencies=[{"uri": "dep/dep.yaml", "rtl": {"pos": -1}}],
    )
    assert [source.file.name for source in design.rtl.sources] == ["top.v", "lib.v"]
    assert (design.rtl.top, dict(design.rtl.parameters)) == ("lib", {"W": 4})
    assert [dict(clock) for clock in design.rtl.clocks] == [{"port": "dclk", "name": "dclk"}]
    assert design.tb.top == ("lib_tb",) and design.dependencies == ()
    with pytest.raises((TypeError, AttributeError, pydantic.ValidationError)):
        design.rtl.sources.append(design.rtl.sources[0])


def test_a_converted_design_plans_and_runs_with_frozen_values(tmp_path):
    with preserved_flow_registry():
        converter, reader, design, the_runner, flow = launch(
            tmp_path, consumer_flows(Events()), sources=("Top.bsv", "ext.v")
        )
    assert flow is not None and flow.succeeded
    derived = flow.design
    assert [(s.file.name, s.generated) for s in derived.rtl.sources] == [
        ("Top.v", True),
        ("ext.v", False),
    ]
    # the design the user gave is as it was
    assert [s.file.name for s in design.rtl.sources] == ["Top.bsv", "ext.v"]
    assert not any(source.generated for source in design.rtl.sources)
    assert isinstance(derived.rtl.parameters, FrozenDict) and derived.rtl.generator is None


def test_a_generated_source_is_built_in_one_call():
    source = Source({"path": "out/top.v"}, SourceType.Verilog, generated_name="top.v")
    assert source.generated and source.generated_name == "top.v"
    assert source.type is SourceType.Verilog
    assert not Source({"path": "out/top.v"}).generated


def test_source_types_keep_enum_identity_and_their_name(tmp_path):
    (tmp_path / "tb.vhd").write_text("entity tb is end entity;\n")
    (tmp_path / "top.vhd").write_text("entity top is end entity;\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.vhd"], "top": "top"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
    )
    for source in (*design.rtl.sources, *design.tb.sources):
        assert source.type is SourceType.Vhdl and source.type.name == "Vhdl"
    assert GhdlSim.runs_without_testbench_top(design)


@needs_tclsh
def test_a_vhdl_source_is_assigned_as_vhdl_to_quartus(tmp_path, monkeypatch):
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.vhd").write_text("entity top is end entity;\n")
    design = Design(
        name="d", design_root=root, rtl={"sources": ["top.vhd"], "top": "top", "clock": "clk"}
    )
    calls = _calls("quartus", design, _settings(root, "quartus"), tmp_path, monkeypatch)
    assert ["set_global_assignment", "-name", "VHDL_FILE", str(root / "top.vhd")] in calls
