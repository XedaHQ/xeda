"""The design a converter flow derives: its declaration, its record and its hand-over.

`planned_design` builds the design a converter will derive before it ran, from the declaration
alone. `derive_design` builds it from the record of a run. A converter (bsc) records the sources
of the derived design from what it collected, not from a listing: the files it generated, and the
sources of the input design that pass through, by index. A consumer is handed that record only
after the generated files and the pass-through entries verify.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xeda import Design
from xeda.deliver import outputs_to_deliveries, recorded_artifacts
from xeda.design import DesignSource, SourceType
from xeda.flow import FlowDependencyFailure, FlowSettingsException
from xeda.flow.derived import (
    DerivedSources,
    DesignConversion,
    Generated,
    Passed,
    derive_design,
    derived_sources,
    planned_design,
    recorded_files,
)
from xeda.flow_runner import DefaultRunner
from xeda.records import read_record
from xeda.flow_runner.outputs import (
    recorded_outputs,
    declared_output_files,
    handed_design,
    handed_entries,
    handed_over,
    recorded_entries,
)
from xeda.flows import Bsc, BscSim

from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .test_bsc import ADDER_PKG, TB_PKG, TOP_PKG, _write
from .tool_utils import require_bsc, run_report

BLUESPEC = frozenset({SourceType.Bluespec})
VERILOG = frozenset({SourceType.Verilog})

#: Sources of every kind that passes through or is converted, in this order.
FILES = {
    "pass.vhd": "entity pass is end entity;\n",
    "lib/Accum.bsv": ADDER_PKG,
    "rtl/Top.bsv": TOP_PKG,
    "rtl/ext.v": "module ext(); endmodule\n",
    "pins.xdc": "# no constraints\n",
    "tb/Tb.bsv": TB_PKG,
}
SOURCES = ["pass.vhd", "lib/Accum.bsv", "rtl/Top.bsv", "rtl/ext.v", "pins.xdc"]


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _design(root: Path, vhdl: bool = True, **fields) -> Design:
    """The design of `SOURCES`; without `vhdl`, the sources of a Bluespec flow that passes none
    through."""
    _write(root, FILES)
    rtl = {
        "sources": SOURCES if vhdl else SOURCES[1:],
        "top": "mkTop",
        "defines": {"STEP": "2"},
        "parameters": {"WIDTH": 8},
        "clock": "CLK",
    }
    return Design(
        name="d",
        design_root=root,
        rtl=rtl,
        tb={"sources": ["tb/Tb.bsv"], "top": "mkTb"},
        **fields,
    )


def _summary(design: Design) -> list[tuple[str, str]]:
    return [(src.file.name, src.type.name) for src in design.rtl.sources]


# ------------------------------------------------------------------------ the declarations


def test_a_conversion_names_what_it_converts_and_what_it_writes() -> None:
    conversion = DesignConversion(BLUESPEC, VERILOG)
    assert conversion.written_type is SourceType.Verilog
    assert conversion.passes_through(DesignSource({"path": "a.v"}, SourceType.Verilog))
    assert not conversion.passes_through(DesignSource({"path": "a.bsv"}, SourceType.Bluespec))


@pytest.mark.parametrize(
    "transforms, produces",
    [
        (frozenset(), VERILOG),
        (BLUESPEC, frozenset()),
        (BLUESPEC, BLUESPEC | VERILOG),
        ({SourceType.Bluespec}, VERILOG),
        (frozenset({"Bluespec"}), VERILOG),
    ],
)
def test_a_conversion_is_two_nonempty_sets_of_languages_that_do_not_meet(
    transforms, produces
) -> None:
    with pytest.raises(TypeError):
        DesignConversion(transforms, produces)


def test_derived_sources_list_each_source_once() -> None:
    first = Generated(Path("/run/a.v"), SourceType.Verilog)
    DerivedSources("top", (first, Passed(0), Passed(1)))
    with pytest.raises(ValueError, match="twice"):
        DerivedSources("top", (first, Passed(0), first))
    with pytest.raises(ValueError, match="twice"):
        DerivedSources("top", (Passed(1), Passed(0), Passed(1)))
    with pytest.raises(TypeError):
        DerivedSources("top", (Path("/run/a.v"),))  # type: ignore[arg-type]


def test_a_generated_file_and_a_passed_source_may_share_a_number() -> None:
    DerivedSources("top", (Generated(Path("/run/0"), SourceType.Verilog), Passed(0)))


# ------------------------------------------------------------------------ the planned design


def test_the_planned_design_puts_the_generated_source_first_and_carries_the_rest(tmp_path) -> None:
    design = _design(tmp_path / "d")
    before = design.model_dump(mode="json")
    where = tmp_path / "run" / "bsc"
    planned = planned_design(design, DesignConversion(BLUESPEC, VERILOG), where)

    assert design.model_dump(mode="json") == before  # the input design is as it was
    assert [(src.file, src.type) for src in planned.rtl.sources] == [
        (where / "mkTop.v", SourceType.Verilog),
        (tmp_path / "d" / "pass.vhd", SourceType.Vhdl),
        (tmp_path / "d" / "rtl/ext.v", SourceType.Verilog),
        (tmp_path / "d" / "pins.xdc", SourceType.Xdc),
    ]
    assert planned.rtl.top == "mkTop"
    assert planned.rtl.parameters == {} and planned.rtl.generator is None
    # everything else is carried
    assert planned.rtl.defines == {"STEP": "2"}
    assert planned.rtl.clocks == design.rtl.clocks
    assert planned.tb.model_dump(mode="json") == design.tb.model_dump(mode="json")
    assert (planned.name, planned.design_root, planned.language) == (
        design.name,
        design.design_root,
        design.language,
    )


def test_the_planned_design_names_a_file_that_need_not_exist(tmp_path) -> None:
    design = _design(tmp_path / "d")
    planned = planned_design(design, DesignConversion(BLUESPEC, VERILOG), tmp_path / "run")
    generated = planned.rtl.sources[0]
    assert not generated.file.exists()
    assert generated.as_json_value() == {
        "path": str(tmp_path / "run" / "mkTop.v"),
        "type": "Verilog",
    }


def test_a_vhdl_conversion_plan_replaces_the_vhdl_source_with_a_placeholder(tmp_path) -> None:
    """Planning replaces the transformed VHDL source with a placeholder without running a flow."""
    design = _design(tmp_path / "d")
    conversion = DesignConversion(frozenset({SourceType.Vhdl}), VERILOG)
    planned = planned_design(design, conversion, tmp_path / "run")
    assert [src.type for src in planned.rtl.sources][1:] == [
        SourceType.Bluespec,
        SourceType.Bluespec,
        SourceType.Verilog,
        SourceType.Xdc,
    ]


# ------------------------------------------------------------------------ the record, no tool


def _launched(tmp_path, design: Design):
    """Run the first test converter (it writes a module for each Bluespec source)."""
    fake, _rival, _to_vhdl = converter_flows()
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(fake, design, {})
    assert flow is not None and flow.succeeded, flow.results
    return flow


def test_a_converter_records_the_sources_of_the_derived_design(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    record = flow.results["outputs"]["design"]

    assert set(record) == {"kind", "top", "input_design", "sources"}
    assert (record["kind"], record["top"]) == ("design", "mkTop")
    assert record["input_design"] == design.parts_hash({"rtl", "tb"})
    sources = record["sources"]
    generated = [item for item in sources if "generated" in item]
    passed = [item for item in sources if "source" in item]
    assert [Path(item["generated"]).name for item in generated] == ["Accum.v", "Top.v"]
    assert all(item["type"] == "Verilog" for item in generated)
    assert all(len(item["sha"]) == 32 for item in generated)
    assert passed == [
        {"source": 0, "file": "pass.vhd", "type": "Vhdl"},
        {"source": 3, "file": "rtl/ext.v", "type": "Verilog"},
        {"source": 4, "file": "pins.xdc", "type": "Xdc"},
    ]
    json.dumps(record)  # the record is JSON


def test_the_files_of_a_design_record_are_its_generated_files(tmp_path) -> None:
    flow = _launched(tmp_path, _design(tmp_path / "d"))
    results = flow.results.to_dict()
    names = [path.name for path in declared_output_files(results)]
    assert names == ["Accum.v", "Top.v"]
    record = results["outputs"]["design"]
    assert [(entry["generated"], key) for entry, key in recorded_files(record)] == [
        (str(path), "generated") for path in declared_output_files(results)
    ]
    assert recorded_files({"path": "/x", "sha": "0" * 32}) == [
        ({"path": "/x", "sha": "0" * 32}, "path")
    ]
    assert recorded_files(None) == []


def test_a_design_record_has_one_shape(tmp_path) -> None:
    flow = _launched(tmp_path, _design(tmp_path / "d"))
    record = flow.results["outputs"]["design"]
    entries = recorded_entries(record, False, "bsc's output `design`", design=True)
    assert [Path(entry.path).name for entry in entries] == ["Accum.v", "Top.v"]
    assert [entry.sha for entry in entries] == [
        item["sha"] for item in record["sources"] if "generated" in item
    ]
    assert handed_entries(flow, "design") == entries

    def broken(**change):
        return json.loads(json.dumps({**record, **change}))

    first = record["sources"][0]
    last = record["sources"][-1]
    generated = next(item for item in record["sources"] if "generated" in item)
    invalid = [
        None,
        [],
        {**broken(), "kind": "files"},
        broken(top=3),
        broken(input_design=None),
        broken(sources="x"),
        # A pass-through record cannot have a digest field.
        broken(sources=[{**first, "sha": "not a digest"}]),
        # A generated record must reject an invalid digest.
        broken(sources=[{**generated, "sha": "not a digest"}]),
        broken(sources=[{**first, "type": "Nope"}]),
        broken(sources=[{**first, "extra": 1}]),
        broken(sources=[{**last, "source": -1}]),
        broken(sources=[{**last, "source": True}]),
        broken(sources=[{**last, "file": 3}]),
        broken(sources=[first, first]),
        broken(sources=[last, last]),
        {key: value for key, value in record.items() if key != "top"},
    ]
    for case in invalid:
        with pytest.raises(FlowDependencyFailure, match="invalid record"):
            recorded_entries(case, False, "bsc's output `design`", design=True)


def test_ran_or_reused_the_derived_design_is_the_same(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    record = flow.results["outputs"]["design"]
    stored = read_record(flow.run_path, run_root=tmp_path / "run")
    reloaded = recorded_outputs(stored)["design"]
    assert reloaded == record

    ran, entries = handed_design(flow, "design", design)
    reused = derive_design(design, reloaded, flow.run_directory.path)
    assert ran.model_dump(mode="json") == reused.model_dump(mode="json")
    for parts in ({"rtl"}, {"rtl", "tb"}):
        assert ran.parts_hash(parts) == reused.parts_hash(parts)
    assert [entry.path for entry in entries] == [
        item["generated"] for item in record["sources"] if "generated" in item
    ]


def test_the_derived_design_replaces_the_converted_sources_and_carries_the_rest(tmp_path) -> None:
    design = _design(tmp_path / "d")
    before = design.model_dump(mode="json")
    flow = _launched(tmp_path, design)
    derived, _entries = handed_design(flow, "design", design)

    assert design.model_dump(mode="json") == before
    run = flow.run_path
    assert _summary(derived) == [
        ("pass.vhd", "Vhdl"),
        ("Accum.v", "Verilog"),
        ("Top.v", "Verilog"),
        ("ext.v", "Verilog"),
        ("pins.xdc", "Xdc"),
    ]
    assert all(src.file.parent == run for src in derived.rtl.sources[1:3])
    passed = [design.rtl.sources[i] for i in (0, 3, 4)]
    assert [derived.rtl.sources[i] for i in (0, 3, 4)] == passed
    assert (derived.rtl.top, derived.rtl.parameters, derived.rtl.generator) == ("mkTop", {}, None)
    assert derived.rtl.defines == design.rtl.defines and derived.rtl.clocks == design.rtl.clocks
    assert derived.tb.model_dump(mode="json") == design.tb.model_dump(mode="json")
    assert derived.parts_hash({"rtl"}) != design.parts_hash({"rtl"})


def test_deriving_a_design_validates_nothing_again(tmp_path, monkeypatch) -> None:
    """A design with a generator would run it again if the copy were validated."""
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    record = flow.results["outputs"]["design"]

    def refused(*args, **kwargs):
        raise AssertionError("the derived design was validated")

    monkeypatch.setattr(Design, "__init__", refused)
    monkeypatch.setattr(Design, "model_validate", refused)
    assert derive_design(design, record, flow.run_directory.path).rtl.top == "mkTop"
    assert planned_design(design, DesignConversion(BLUESPEC, VERILOG), tmp_path / "run").rtl.sources


def test_the_derivation_of_a_malformed_record_is_a_value_error(tmp_path) -> None:
    design = _design(tmp_path / "d")
    with pytest.raises(ValueError, match="malformed"):
        derived_sources({"top": "mkTop"})
    with pytest.raises(ValueError, match="source 9"):
        derive_design(design, {"top": "mkTop", "sources": [{"source": 9}]}, tmp_path)


# ------------------------------------------------------------------------ the hand-over


def _handed(tmp_path, run_root: str, design: Design) -> Design:
    """The design that the converter derives, with its run directory under `run_root`."""
    fake, _rival, _to_vhdl = converter_flows()
    flow = DefaultRunner(tmp_path / run_root, display_results=False).run_flow(fake, design, {})
    assert flow is not None and flow.succeeded, flow.results
    derived, _entries = handed_design(flow, "design", design)
    return derived


def test_the_hash_of_a_derived_design_does_not_depend_on_where_the_run_directory_is(
    tmp_path,
) -> None:
    """The generated files are named by their place in the converter's output and their content,
    not by a path from the design root to the run directory."""
    design = _design(tmp_path / "d")
    near = _handed(tmp_path, "run", design)
    far = _handed(tmp_path, "deeper/elsewhere/run", design)
    generated = [src.file for src in near.rtl.sources if src.file.name == "Top.v"]
    assert generated != [src.file for src in far.rtl.sources if src.file.name == "Top.v"]
    for parts in ({"rtl"}, {"rtl", "tb"}):
        assert near.parts_hash(parts) == far.parts_hash(parts)
    assert near.rtl_hash == far.rtl_hash
    text = json.dumps(near.rtl_fingerprint)
    assert str(tmp_path) not in text and "run" not in text.replace("rtl", "")


def test_a_source_on_another_drive_has_no_path_from_the_design_root(tmp_path, monkeypatch) -> None:
    """On Windows, `os.path.relpath` raises a `ValueError` for a path on another drive. Such a
    source is named by its absolute path, in the hash of the design and in the record."""
    real = os.path.relpath

    def relpath(path, start=os.curdir):
        if "elsewhere" in os.fspath(path):
            raise ValueError("path is on mount 'D:', start on mount 'C:'")
        return real(path, start)

    monkeypatch.setattr(os.path, "relpath", relpath)
    root = tmp_path / "d"
    _write(root, {"Top.bsv": TOP_PKG})
    outside = tmp_path / "elsewhere" / "ext.v"
    outside.parent.mkdir()
    outside.write_text("module ext(); endmodule\n")
    design = Design(
        name="d", design_root=root, rtl={"sources": ["Top.bsv", str(outside)], "top": "mkTop"}
    )
    assert design.rtl_fingerprint["sources"][1]["path"] == outside.as_posix()
    flow = _launched(tmp_path, design)
    assert flow.results["outputs"]["design"]["sources"][-1] == {
        "source": 1,
        "file": outside.as_posix(),
        "type": "Verilog",
    }
    handed_design(flow, "design", design)


def test_the_hand_over_of_an_untouched_record_succeeds(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    assert [path.name for path in handed_over(flow, "design")] == ["Accum.v", "Top.v"]
    derived, entries = handed_design(flow, "design", design)
    assert len(entries) == 2 and derived.rtl.sources


def test_the_hand_over_refuses_a_generated_file_that_changed(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    generated = flow.run_path / "Top.v"
    generated.write_text("module other(); endmodule\n")
    with pytest.raises(FlowDependencyFailure, match="changed since its run recorded it"):
        handed_design(flow, "design", design)
    with pytest.raises(FlowDependencyFailure, match="changed since its run recorded it"):
        handed_over(flow, "design")


def test_the_hand_over_refuses_a_generated_file_that_is_gone(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    (flow.run_path / "Accum.v").unlink()
    with pytest.raises(FlowDependencyFailure, match="missing or outside"):
        handed_design(flow, "design", design)


def test_the_hand_over_refuses_a_generated_file_outside_the_run_directory(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    outside = tmp_path / "elsewhere.v"
    outside.write_text("module x(); endmodule\n")
    record = flow.results["outputs"]["design"]
    next(item for item in record["sources"] if "generated" in item)["generated"] = str(outside)
    with pytest.raises(FlowDependencyFailure, match="missing or outside"):
        handed_design(flow, "design", design)


def test_the_hand_over_refuses_a_design_that_lists_its_sources_in_another_order(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    reordered = Design(
        name="d",
        design_root=design.design_root,
        rtl={
            **design.rtl.model_dump(mode="json"),
            "sources": [SOURCES[i] for i in (1, 0, 2, 3, 4)],
        },
        tb={"sources": ["tb/Tb.bsv"], "top": "mkTb"},
    )
    with pytest.raises(FlowDependencyFailure, match=r"source 0 of the design was pass\.vhd"):
        handed_design(flow, "design", reordered)


def test_the_hand_over_refuses_a_design_that_dropped_a_source(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    shorter = Design(
        name="d",
        design_root=design.design_root,
        rtl={**design.rtl.model_dump(mode="json"), "sources": SOURCES[:4]},
        tb={"sources": ["tb/Tb.bsv"], "top": "mkTb"},
    )
    with pytest.raises(FlowDependencyFailure, match=r"source 4 of the design was pins\.xdc"):
        handed_design(flow, "design", shorter)


def test_the_hand_over_refuses_a_source_of_another_type_at_the_same_place(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    retyped = Design(
        name="d",
        design_root=design.design_root,
        rtl={
            **design.rtl.model_dump(mode="json"),
            "sources": [*SOURCES[:3], {"file": "rtl/ext.v", "type": "SystemVerilog"}, SOURCES[4]],
        },
        tb={"sources": ["tb/Tb.bsv"], "top": "mkTb"},
    )
    with pytest.raises(FlowDependencyFailure, match=r"source 3 of the design was rtl/ext\.v"):
        handed_design(flow, "design", retyped)


def test_the_input_design_of_a_record_is_provenance_and_nothing_compares_it(tmp_path) -> None:
    """The sources are the same files in the same places, and a define differs. The record names
    the hash of the design it was made from, but the hand-over judges the files and the sources
    that pass through, never that hash: the converter's own trace decides whether it ran again
    for the design. A converter that reads fewer parts of the design than it records would
    otherwise refuse its own record forever."""
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    other = design.model_copy(deep=True)
    other.rtl.defines["STEP"] = "3"
    other.rtl.invalidate_cached_properties()
    other.invalidate_cached_properties()
    assert flow.results["outputs"]["design"]["input_design"] == design.parts_hash({"rtl", "tb"})
    derived, _entries = handed_design(flow, "design", other)
    assert derived.rtl.defines == {"STEP": "3"}
    flow.results["outputs"]["design"]["input_design"] = "provenance"
    handed_design(flow, "design", design)


def test_the_hand_over_of_a_missing_record_says_to_run_again(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    del flow.results["outputs"]["design"]
    with pytest.raises(FlowDependencyFailure, match="recorded no output `design`"):
        handed_design(flow, "design", design)


def test_an_output_the_flow_does_not_declare_is_not_handed_over(tmp_path) -> None:
    design = _design(tmp_path / "d")
    flow = _launched(tmp_path, design)
    with pytest.raises(FlowDependencyFailure, match="recorded no output `nothing`"):
        handed_design(flow, "nothing", design)


# ------------------------------------------------------------------------ bsc


def _bsc(design: Design, tmp_path, **settings):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(Bsc, design, settings)
    assert flow is not None
    return flow


def test_bsc_records_the_design_from_what_it_collected(tmp_path, capfd) -> None:
    """The file that defines the reset macro and the library modules come first. Every source that
    is not Bluespec follows in design order, whatever its language. The generated modules come
    last, the top module at the end. Each source appears once."""
    require_bsc()
    design = _design(tmp_path / "d")
    flow = _bsc(design, tmp_path)
    assert flow.succeeded, run_report(flow, capfd.readouterr())

    record = flow.results["outputs"]["design"]
    shape = [
        ("G", Path(item["generated"]).name) if "generated" in item else ("P", item["source"])
        for item in record["sources"]
    ]
    assert shape[0] == ("G", "bsv_defines.v")
    assert shape[-5:] == [("P", 0), ("P", 3), ("P", 4), ("G", "mkAccum.v"), ("G", "mkTop.v")]
    library = shape[1:-5]
    assert library and all(kind == "G" for kind, _ in library)
    assert [name for kind, name in shape if kind == "G"][1:-2] == [name for _, name in library]
    # artifacts.verilog lists the same Verilog files, in the same order
    verilog = [
        str(design.rtl.sources[item["source"]].file) if "source" in item else item["generated"]
        for item in record["sources"]
        if item.get("type") == "Verilog"
    ]
    assert verilog == flow.artifacts.verilog
    assert all(len(set(item.values())) == len(item) for item in record["sources"])
    assert (record["top"], record["kind"]) == ("mkTop", "design")
    assert record["input_design"] == design.parts_hash({"rtl", "tb"})

    derived, entries = handed_design(flow, "design", design)
    assert [src.type for src in derived.rtl.sources].count(SourceType.Bluespec) == 0
    assert len(entries) == len([1 for kind, _ in shape if kind == "G"])
    assert derived.rtl.sources[-5:-2] == [design.rtl.sources[i] for i in (0, 3, 4)]
    assert derived.rtl.top == "mkTop" and derived.rtl.parameters == {}
    assert derived.tb.sources == design.tb.sources


def test_the_generated_files_of_bsc_are_delivered_once_by_outputs_to(tmp_path, capfd) -> None:
    """The generated files are listed under `artifacts.verilog` and in the record of the design
    output. `--outputs-to` copies each file of the run directory once, and none that the design
    passes through from outside it."""
    require_bsc()
    design = _design(tmp_path / "d")
    flow = _bsc(design, tmp_path)
    assert flow.succeeded, run_report(flow, capfd.readouterr())
    recorded = recorded_artifacts(flow.run_path, flow.run_path.parent.parent)
    assert set(recorded) == {"artifacts", "outputs"}
    deliveries = outputs_to_deliveries(recorded, flow.run_path, tmp_path / "out")
    names = [delivery.name for delivery in deliveries]
    assert len(names) == len(set(names))
    generated = [
        Path(item["generated"]).name
        for item in flow.results["outputs"]["design"]["sources"]
        if "generated" in item
    ]
    assert generated and set(generated) <= {name.name for name in names}
    assert "ext.v" not in {name.name for name in names}


def test_bsc_without_the_reset_macro_has_no_defining_file(tmp_path, capfd) -> None:
    require_bsc()
    design = _design(tmp_path / "d")
    flow = _bsc(design, tmp_path, positive_reset=False)
    assert flow.succeeded, run_report(flow, capfd.readouterr())
    names = [
        Path(item["generated"]).name
        for item in flow.results["outputs"]["design"]["sources"]
        if "generated" in item
    ]
    assert "bsv_defines.v" not in names and names[-1] == "mkTop.v"


@pytest.mark.parametrize("positive_reset", [False, True])
def test_bsc_records_the_modules_it_found_up_to_date(positive_reset, tmp_path, capfd) -> None:
    """Without `cleanup_bobjs`, bsc keeps the Verilog of a package it finds up to date. A run in
    the directory of an earlier run still records those modules, with the content they have."""
    require_bsc()
    design = _design(tmp_path / "d")
    settings = {"cleanup_bobjs": False, "positive_reset": positive_reset}
    first = _bsc(design, tmp_path, **settings)
    assert first.succeeded, run_report(first, capfd.readouterr())
    before = first.results["outputs"]["design"]
    again = _bsc(design, tmp_path, verbose=1, **settings)
    assert again.succeeded, run_report(again, capfd.readouterr())
    assert again.results["outputs"]["design"] == before
    handed_design(again, "design", design)


def test_bsc_never_records_a_generated_module_that_it_did_not_write(tmp_path, capfd) -> None:
    """Without `cleanup_bobjs`, a module changed in the run directory after a run is written
    again by bsc. The record holds what bsc wrote, never the changed file."""
    require_bsc()
    design = _design(tmp_path / "d")
    first = _bsc(design, tmp_path, cleanup_bobjs=False)
    assert first.succeeded, run_report(first, capfd.readouterr())
    before = first.results["outputs"]["design"]
    generated = [Path(item["generated"]) for item in before["sources"] if "generated" in item]
    module = generated[-1]
    original = module.read_bytes()
    module.write_bytes(original + b"// changed after the run\n")
    again = _bsc(design, tmp_path, cleanup_bobjs=False)
    assert again.succeeded, run_report(again, capfd.readouterr())
    assert module.read_bytes() == original
    assert again.results["outputs"]["design"] == before


def test_bsc_passes_a_design_without_other_languages_through_alone(tmp_path, capfd) -> None:
    require_bsc()
    root = tmp_path / "d"
    _write(root, {"lib/Accum.bsv": ADDER_PKG, "rtl/Top.bsv": TOP_PKG})
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["lib/Accum.bsv", "rtl/Top.bsv"], "top": "mkTop"},
    )
    flow = _bsc(design, tmp_path, positive_reset=False)
    assert flow.succeeded, run_report(flow, capfd.readouterr())
    items = flow.results["outputs"]["design"]["sources"]
    assert all("generated" in item for item in items)


def test_bsc_takes_a_design_with_vhdl_beside_bluespec(tmp_path) -> None:
    """The language check of a converter refuses no language it passes through."""
    design = _design(tmp_path / "d")
    Bsc.check_design_supported(design, Bsc.Settings())
    only_vhdl = Design(
        name="v",
        design_root=design.design_root,
        rtl={"sources": ["pass.vhd"], "top": "pass"},
    )
    with pytest.raises(FlowSettingsException, match="reads none of the design's sources"):
        Bsc.check_design_supported(only_vhdl, Bsc.Settings())


# ------------------------------------------------------------------------ BSV_POSITIVE_RESET

REFUSAL = (
    r"the design defines `BSV_POSITIVE_RESET`, which bsc defines itself when "
    r"`positive_reset` is true: remove the define, or set `positive_reset: false`"
)


@pytest.mark.parametrize("flow_class", [Bsc, BscSim])
@pytest.mark.parametrize("part", ["defines", "parameters"])
def test_a_design_that_defines_the_reset_macro_is_refused(flow_class, part, tmp_path) -> None:
    design = _design(tmp_path / "d", vhdl=False)
    getattr(design.rtl, part)["BSV_POSITIVE_RESET"] = None
    with pytest.raises(FlowSettingsException, match=REFUSAL):
        flow_class.check_design_supported(design, flow_class.Settings(positive_reset=True))


@pytest.mark.parametrize("part", ["defines", "parameters"])
def test_bsc_sim_refuses_the_macro_of_the_testbench_and_bsc_does_not(part, tmp_path) -> None:
    design = _design(tmp_path / "d", vhdl=False)
    getattr(design.tb, part)["BSV_POSITIVE_RESET"] = None
    with pytest.raises(FlowSettingsException, match=REFUSAL):
        BscSim.check_design_supported(design, BscSim.Settings(positive_reset=True))
    Bsc.check_design_supported(design, Bsc.Settings())


def test_bsc_sim_takes_the_reset_macro_while_positive_reset_is_off_as_it_is_by_default(
    tmp_path,
) -> None:
    design = _design(tmp_path / "d", vhdl=False)
    design.rtl.defines["BSV_POSITIVE_RESET"] = None
    BscSim.check_design_supported(design, BscSim.Settings())


@pytest.mark.parametrize("flow_class", [Bsc, BscSim])
def test_the_reset_macro_is_the_designs_to_define_without_positive_reset(
    flow_class, tmp_path
) -> None:
    design = _design(tmp_path / "d", vhdl=False)
    design.rtl.defines["BSV_POSITIVE_RESET"] = None
    flow_class.check_design_supported(design, flow_class.Settings(positive_reset=False))
    flow_class.check_design_supported(design)  # no settings: nothing to compare with


def test_the_planning_of_bsc_refuses_the_reset_macro(tmp_path) -> None:
    design = _design(tmp_path / "d")
    design.rtl.defines["BSV_POSITIVE_RESET"] = None
    with pytest.raises(FlowSettingsException, match=REFUSAL):
        DefaultRunner(tmp_path / "run", display_results=False).plan("bsc", design)
    planned = DefaultRunner(tmp_path / "run", display_results=False).plan(
        "bsc", design, flow_settings={"positive_reset": False}
    )
    assert [node.name for node in planned.nodes] == ["bsc"]
