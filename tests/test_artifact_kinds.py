"""The table of netlist, checkpoint and database types, and the two netlists of `yosys`.

`xeda.artifact_kinds.KINDS` has one row for each such member. The tests check the table against an
independent list, against the declarations of the flows, and against what `yosys` writes. The
documentation shows the same table (`docs/design-file.rst`), and a test compares the two.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.artifact_kinds import (
    KINDS,
    Abstraction,
    Container,
    TargetKind,
    member_for,
)
from xeda.design import SourceType
from xeda.flow.io import declared_inputs, declared_outputs, output_required, selected_types
from xeda.flows import Yosys
from xeda.introspect import all_flow_classes

from .tool_utils import require_yosys

#: The members that the table describes, written out here and not derived from the table.
EXPECTED = {
    "VerilogNetlist",
    "GenericVerilogNetlist",
    "JsonNetlist",
    "Edif",
    "FpgaNetlist",
    "FpgaTimingNetlist",
    "SynthCheckpoint",
    "RoutedCheckpoint",
    "OpenroadDbPlaced",
    "OpenroadDbCts",
    "OpenroadDbGlobalRouted",
    "OpenroadDbRouted",
    "VhdlNetlist",
    "Blif",
    "Checkpoint",
    "Odb",
}
#: The members that no flow declares. Their rows say `unspecified` and nothing more.
LEGACY = {"VhdlNetlist", "Blif", "Checkpoint", "Odb"}


def problems(kinds: dict) -> list[str]:
    """What is wrong with a table: a missing or an extra row, or two rows with one triple."""
    found = []
    names = {member.name for member in kinds}
    found += [f"no row for {name}" for name in sorted(EXPECTED - names)]
    found += [f"an extra row for {name}" for name in sorted(names - EXPECTED)]
    triples: dict = {}
    for member, kind in kinds.items():
        triples.setdefault(kind.triple, []).append(member.name)
    found += [
        f"{triple} is the row of {names}" for triple, names in triples.items() if len(names) > 1
    ]
    return found


def test_every_netlist_checkpoint_and_database_member_has_one_row():
    assert problems(KINDS) == []


def test_the_check_finds_a_missing_row_an_extra_row_and_a_repeated_triple():
    """The oracle's teeth: each fault is reported."""
    missing = dict(KINDS)
    del missing[SourceType.Edif]
    assert problems(missing) == ["no row for Edif"]
    extra = {**KINDS, SourceType.Verilog: KINDS[SourceType.Edif]}
    assert "an extra row for Verilog" in problems(extra)
    repeated = {**KINDS, SourceType.GenericVerilogNetlist: KINDS[SourceType.VerilogNetlist]}
    assert any("is the row of" in text for text in problems(repeated))


def test_a_triple_names_one_member():
    for member, kind in KINDS.items():
        assert member_for(*kind.triple) is member
    with pytest.raises(KeyError):
        member_for(Container.BLIF, Abstraction.ROUTED, TargetKind.NONE)


def test_the_legacy_rows_say_unspecified_and_the_other_rows_do_not():
    for member, kind in KINDS.items():
        legacy = member.name in LEGACY
        assert (kind.abstraction is Abstraction.UNSPECIFIED) == legacy, member
        assert (kind.target is TargetKind.UNSPECIFIED) == legacy, member


def test_no_flow_declares_a_legacy_member():
    """The first flow that does has to complete the row."""
    legacy = {SourceType[name] for name in LEGACY}
    for flow in all_flow_classes():
        if not flow.__module__.startswith("xeda."):
            continue
        for declaration in (*declared_inputs(flow).values(), *declared_outputs(flow).values()):
            assert not set(declaration.types) & legacy, (flow.name, declaration.name)


def test_the_members_are_type_only_and_no_suffix_names_them():
    from xeda.design import SOURCE_SUFFIXES, TYPE_ONLY

    assert SourceType.GenericVerilogNetlist in TYPE_ONLY
    assert SourceType.GenericVerilogNetlist not in set(SOURCE_SUFFIXES.values())


def test_the_standard_cell_and_the_generic_netlist_differ_in_what_the_cells_are():
    mapped, generic = KINDS[SourceType.VerilogNetlist], KINDS[SourceType.GenericVerilogNetlist]
    assert (mapped.container, generic.container) == (Container.VERILOG, Container.VERILOG)
    assert (mapped.abstraction, mapped.target) == (Abstraction.MAPPED, TargetKind.STANDARD_CELLS)
    assert (generic.abstraction, generic.target) == (Abstraction.GENERIC, TargetKind.NONE)


# ------------------------------------------------------------------------------ the docs table


def documented_rows() -> dict[str, tuple[str, str, str, str]]:
    """The rows of the table in `docs/design-file.rst`, by member name."""
    text = (Path(__file__).parent.parent / "docs" / "design-file.rst").read_text(encoding="utf-8")
    rows = {}
    for match in re.finditer(
        r"^   \* - ``(\w+)``\n     - (\w+)\n     - (\w+)\n     - (\w+)\n"
        r"     - (.+(?:\n       .+)*)$",
        text,
        re.MULTILINE,
    ):
        name, container, abstraction, target, meaning = match.groups()
        rows[name] = (container, abstraction, target, " ".join(meaning.split()))
    return rows


def test_the_documentation_shows_the_table():
    expected = {
        member.name: (kind.container.value, kind.abstraction.value, kind.target.value, kind.meaning)
        for member, kind in KINDS.items()
    }
    assert documented_rows() == expected


# ------------------------------------------------------------------------------ the two netlists

GRID = [
    {},
    {"platform": "nangate45"},
    {"platform_file": "residue.yaml"},
    {"liberty": ["a.lib"]},
    {"gates": ["AND", "OR"]},
    {"lut": "4"},
    {"nosynth": True},
]


@pytest.mark.parametrize("given", GRID, ids=lambda given: ",".join(sorted(given)) or "none")
def test_exactly_one_netlist_of_yosys_has_a_type_and_the_mapped_one_only_for_a_library(given):
    settings = Yosys.Settings(**given)
    chosen = [
        name
        for name in ("netlist", "netlist_generic")
        if selected_types(Yosys, settings, name, output=True)
    ]
    assert chosen == ["netlist" if settings.maps_to_liberty() else "netlist_generic"]
    assert chosen == [Yosys.netlist_output(settings)]
    types = {name: selected_types(Yosys, settings, name, output=True) for name in chosen}
    assert types == {
        chosen[0]: (
            (
                SourceType.VerilogNetlist
                if chosen[0] == "netlist"
                else SourceType.GenericVerilogNetlist
            ),
        )
    }
    produced = {
        name: output_required(Yosys, settings, declared_outputs(Yosys)[name])
        for name in ("netlist", "netlist_generic")
    }
    assert produced == {name: name == chosen[0] for name in produced}


@pytest.mark.parametrize(
    "given, mapped",
    [
        ({}, False),
        ({"lut": "4"}, False),
        ({"gates": ["AND", "OR", "NOT"]}, False),
        ({"platform": "tiny"}, True),
    ],
    ids=["default", "lut", "gates", "platform"],
)
def test_the_netlist_that_is_declared_is_the_one_the_script_writes(
    tmp_path, monkeypatch, given, mapped
):
    """The script maps to Liberty cells (`dfflibmap`) exactly for the mapped netlist, and a
    generic one is written by the `abc` that maps to gates or LUTs, whatever `maps_to_liberty()`
    says. The script is the evidence here, not the predicate."""
    from .test_yosys_asic import _scripted_launch
    from .tiny_platform import tiny_settings, tiny_store

    settings = dict(given)
    if mapped:
        settings = tiny_settings(tiny_store(tmp_path), clock={"period": "2 ns"})
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = (run_dir / "yosys_synth.ys").read_text()
    chosen = Yosys.netlist_output(Yosys.Settings(**settings))
    assert ("dfflibmap" in script) is mapped
    assert (chosen == "netlist") is mapped


def test_yosys_declares_both_netlists_with_their_types():
    outputs = declared_outputs(Yosys)
    assert outputs["netlist"].types == (SourceType.VerilogNetlist,)
    assert outputs["netlist_generic"].types == (SourceType.GenericVerilogNetlist,)
    assert (
        outputs["netlist"].enabled_by == outputs["netlist_generic"].enabled_by == "netlist_verilog"
    )


def test_without_the_verilog_switch_neither_netlist_is_produced():
    settings = Yosys.Settings(netlist_verilog=None)
    for name, declaration in declared_outputs(Yosys).items():
        if name.startswith("netlist"):
            assert not output_required(Yosys, settings, declaration)


def test_the_generic_netlist_fits_no_input():
    from xeda.flow_runner.chains import fitting_outputs

    for flow in all_flow_classes():
        if not flow.__module__.startswith("xeda."):
            continue
        for declaration in declared_inputs(flow).values():
            fitting, _many = fitting_outputs(Yosys, declaration, output="netlist_generic")
            assert fitting == [], (flow.name, declaration.name)


ARTIFACT_NAME = re.compile(r"Netlist|Checkpoint|Db|Odb|Blif|Edif")


def unclassified(types) -> list[str]:
    """The declared types that look like a netlist, a checkpoint or a database and have no row."""
    return sorted({t.name for t in types if ARTIFACT_NAME.search(t.name) and t not in KINDS})


def test_the_kind_of_each_declared_type_is_in_the_table():
    """A declared netlist, checkpoint or database type has a row, and a row has a declaration or
    is a member with an unspecified row. The first check does not start from the table, so a type that is added to
    a declaration and not to the table is found."""
    declared = set()
    for flow in all_flow_classes():
        if not flow.__module__.startswith("xeda."):
            continue
        for declaration in (*declared_inputs(flow).values(), *declared_outputs(flow).values()):
            declared |= set(declaration.types)
    assert unclassified(declared) == []
    assert {member.name for member in declared & set(KINDS)} == EXPECTED - LEGACY


def test_a_declared_type_without_a_row_is_found():
    from collections import namedtuple

    fake = namedtuple("Fake", "name")("FooNetlist")
    assert unclassified([SourceType.Verilog, fake, SourceType.VerilogNetlist]) == ["FooNetlist"]


# ------------------------------------------------------------------------------ real yosys

RTL = (
    "module top(input clk, a, b, c, output reg y);\n"
    "  always @(posedge clk) y <= (a & b) | c;\n"
    "endmodule\n"
)


def run_yosys(tmp_path: Path, *settings: str) -> tuple[dict, Path]:
    design = tmp_path / "design"
    design.mkdir()
    (design / "top.v").write_text(RTL)
    (design / "design.yaml").write_text(
        "name: top\nrtl:\n  sources: [top.v]\n  top: top\n  clock:\n    port: clk\n"
    )
    run_root = tmp_path / "xeda_run"
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "run", "yosys", "design.yaml", "--run-root", str(run_root)]
        + (["-s", *settings] if settings else [])
        + ["--json"],
        cwd=design,
        capture_output=True,
        text=True,
        timeout=600,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:]
    run_dir = run_root / "top" / "yosys"
    return json.loads((run_dir / "results.json").read_text()), run_dir


@pytest.mark.parametrize("setting", ["lut=4", ""], ids=["lut", "default"])
def test_a_run_without_a_library_records_the_generic_netlist(tmp_path, setting):
    require_yosys()
    results, run_dir = run_yosys(tmp_path, *([setting] if setting else []))
    assert not results["outputs"]["netlist"]["names"]
    netlist = run_dir / results["outputs"]["netlist_generic"]["names"][0]
    assert netlist == run_dir / "netlist.v"
    assert netlist.is_file()


def test_a_run_with_a_library_records_the_mapped_netlist(tmp_path):
    from .tiny_platform import TINY_RESIDUE, tiny_store

    require_yosys()
    store = tiny_store(tmp_path)
    results, run_dir = run_yosys(
        tmp_path, f"platform_file={TINY_RESIDUE}", f"pdk_root={store}", "clock.period=2ns"
    )
    assert "netlist_generic" not in results["outputs"]
    assert Path(results["outputs"]["netlist"]["path"]) == run_dir / "netlist.v"
    assert "_X1 " in (run_dir / "netlist.v").read_text()


# ------------------------------------------------------------------------------ a consumer


def test_a_consumer_of_the_generic_netlist_is_handed_it_and_the_mapped_one_is_not_made(tmp_path):
    """The hand-over of the output that the settings produce works, and the other output of the
    same switch is neither required nor recorded. The consumer is a test flow."""
    from xeda import Design
    from xeda.flow import Flow, In
    from xeda.flow_runner import DefaultRunner

    from .flow_registry import preserved_flow_registry

    require_yosys()
    with preserved_flow_registry():

        class _GenericTaker(Flow):
            """Reads the generic netlist of yosys."""

            results_description = {}

            class Inputs(Flow.Inputs):
                netlist: Path = In(
                    SourceType.GenericVerilogNetlist,
                    producer="yosys",
                    output="netlist_generic",
                    description="The netlist.",
                )

            def run(self) -> None:
                self.results["read"] = "module" in self.inputs.netlist.read_text()

        design_root = tmp_path / "design"
        design_root.mkdir()
        (design_root / "top.v").write_text(RTL)
        design = Design(
            name="top",
            design_root=design_root,
            rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
        )
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        plan = runner.plan(_GenericTaker, design)
        assert [node.name for node in plan.nodes] == ["yosys", "__generic_taker"]
        flow = runner.run_flow(_GenericTaker, design)
    assert flow.succeeded and flow.results["read"] is True
    yosys = json.loads((tmp_path / "xeda_run" / "top" / "yosys" / "results.json").read_text())
    assert yosys["outputs"]["netlist_generic"]["names"] and not yosys["outputs"]["netlist"]["names"]
