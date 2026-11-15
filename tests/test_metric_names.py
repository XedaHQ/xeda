"""The names of the metrics: one path means one quantity in every built-in flow.

A metric path names one tuple: quantity, population, aggregation, analysis scope, sign, type and
unit. The checks below read the `results_description` of every built-in flow. Two checks keep a
reviewed table each: `REVIEWED_UNITLESS_NUMBERS` and `REVIEWED_SHARED_PATHS`. A new metric of
either kind fails until a person adds a row with a reason. A unitless `integer` is a count: its
description says so, or `REVIEWED_UNITLESS_INTEGERS` has a row for it.
"""

import re
from collections import defaultdict
from typing import Dict, FrozenSet, List, Set, Tuple, Type

import pytest

from xeda._internal.flow import COMMON_METRICS, Flow

from .test_documentation import BUILT_IN_FLOWS

pytestmark = pytest.mark.python_compat


def tool_family(flow_class: Type[Flow]) -> str:
    """Return the tool behind a flow: the first word of its name.

    `openroad_place` and `openroad_grt` belong to `openroad`, `vivado_impl` and `vivado_power` to
    `vivado`, `yosys_fpga` to `yosys`, and `ghdl_sim` and `ghdl_synth` to `ghdl`. The `nextpnr`
    flow also drives openXC7, so that one name is the whole family."""
    return flow_class.name.split("_", 1)[0]


def _described() -> Dict[str, Dict[str, Tuple[str, str | None]]]:
    """Return path -> flow name -> (type, unit) over every built-in flow."""
    found: Dict[str, Dict[str, Tuple[str, str | None]]] = defaultdict(dict)
    for flow_class in BUILT_IN_FLOWS:
        for path, spec in flow_class.results_description.items():
            found[path][flow_class.name] = (spec.type, spec.unit)
    return found


def _families() -> Dict[str, Set[str]]:
    """Return path -> the tool families that describe it."""
    by_flow = {cls.name: tool_family(cls) for cls in BUILT_IN_FLOWS}
    return {path: {by_flow[name] for name in flows} for path, flows in _described().items()}


def _unitless_numbers() -> Set[str]:
    return {
        path
        for path, flows in _described().items()
        if any(kind in ("number", "numbers") and unit is None for kind, unit in flows.values())
    }


def test_one_path_has_one_type_and_one_unit_in_every_flow():
    conflicts = {
        path: sorted({signature for signature in flows.values()})
        for path, flows in _described().items()
        if len(set(flows.values())) > 1
    }
    assert not conflicts, (
        f"These paths have more than one (type, unit): {conflicts}. One path is one quantity, so "
        "give the flow that differs its own path, or make its type and unit equal."
    )


def test_no_path_is_a_leaf_in_one_flow_and_a_group_in_another():
    paths = sorted(_described())
    clashes = [(leaf, inner) for leaf in paths for inner in paths if inner.startswith(leaf + ".")]
    assert not clashes, (
        f"These paths are a leaf in one flow and the prefix of another path: {clashes}. A result "
        "cannot hold a value and an object at one place. Rename one of them."
    )


def test_an_area_is_in_square_microns_and_a_library_area_has_no_unit():
    wrong = []
    for path, flows in _described().items():
        for flow, (_, unit) in flows.items():
            if path.startswith("area.") and unit != "um^2":
                wrong.append(f"{flow}: {path} has the unit {unit!r}, not 'um^2'")
            if path.startswith("library_area.") and unit is not None:
                wrong.append(f"{flow}: {path} has the unit {unit!r}, not no unit")
    assert not wrong, (
        f"{wrong}. `area.*` comes from the geometry of the layout and is in um^2. "
        "`library_area.*` is a sum of Liberty `area` values: it is relative to the libraries and "
        "has no unit."
    )


#: A unit as the last word of a segment (`delay_ns`) or as a whole segment.
UNIT_SUFFIX = re.compile(
    r"(^|_)(percent|pct|ns|ps|fs|us|ms|s|hz|khz|mhz|ghz|w|mw|uw|nw|um|um2|mm|mm2|mv|v|ma)$"
)


def _grammar_problems(path: str) -> List[str]:
    problems = []
    for segment in path.split("."):
        if segment.startswith("num_"):
            problems.append(f"the segment {segment!r} starts with 'num_'")
        if UNIT_SUFFIX.search(segment):
            problems.append(f"the segment {segment!r} ends in a percent or a unit")
    return problems


def test_a_metric_name_has_no_count_prefix_and_no_unit_suffix():
    found = {path: problems for path in _described() if (problems := _grammar_problems(path)) != []}
    assert not found, (
        f"{found}. A population word alone is its count (`cells`, not `num_cells`). The unit is "
        "in the descriptor, never in the name. `_ratio` is allowed."
    )


def test_the_grammar_check_finds_a_prefix_and_a_suffix():
    assert _grammar_problems("num_cells")
    assert _grammar_problems("utilization_percent")
    assert _grammar_problems("delay_ns")
    assert _grammar_problems("power.total_w")
    assert not _grammar_problems("utilization_ratio")
    assert not _grammar_problems("clock_skew_setup")
    assert not _grammar_problems("cocotb.sim_time")


def test_every_common_metric_is_used_by_a_built_in_flow():
    used = {
        path
        for flow_class in BUILT_IN_FLOWS
        for path, spec in flow_class.results_description.items()
        if COMMON_METRICS.get(path) == spec
    }
    unused = sorted(set(COMMON_METRICS) - used)
    assert not unused, (
        f"No built-in flow uses these shared metrics: {unused}. Remove them from "
        "`COMMON_METRICS`, or describe them in the flow that reports them."
    )


def test_no_flow_redeclares_a_common_metric_with_another_type_or_unit():
    wrong = [
        f"{flow_class.name}: {path} is ({spec.type}, {spec.unit}), the shared one is "
        f"({COMMON_METRICS[path].type}, {COMMON_METRICS[path].unit})"
        for flow_class in BUILT_IN_FLOWS
        for path, spec in flow_class.results_description.items()
        if path in COMMON_METRICS
        and (spec.type, spec.unit) != (COMMON_METRICS[path].type, COMMON_METRICS[path].unit)
    ]
    assert not wrong, (
        f"{wrong}. A flow may give a shared path its own description, but not another type or "
        "unit. Use another path for another quantity."
    )


#: Every `number` and `numbers` metric without a unit, and why it has none.
REVIEWED_UNITLESS_NUMBERS: Dict[str, str] = {
    "area_core": "Design Compiler label; the unit of the value is not verified",
    "aspect_ratio": "ratio of two lengths",
    "bram_tile": "tile count; a RAMB18 is half a tile, so the value can end in .5",
    "coverage.branch": "ratio from 0 to 1",
    "coverage.statement": "ratio from 0 to 1",
    "coverage.toggle": "ratio from 0 to 1",
    "library_area.cells": "library-relative area",
    "library_area.combinational": "library-relative area",
    "library_area.macros_blackboxes": "library-relative area",
    "library_area.nets": "library-relative area",
    "library_area.sequential": "library-relative area",
    "library_area.with_nets": "library-relative area",
    "utilization": "ratio of two areas",
    "utilization_ratio": "ratio of two areas, as Design Compiler prints it",
}


def test_every_number_without_a_unit_has_a_reviewed_reason():
    unitless = _unitless_numbers()
    missing = sorted(unitless - set(REVIEWED_UNITLESS_NUMBERS))
    stale = sorted(set(REVIEWED_UNITLESS_NUMBERS) - unitless)
    assert not missing, (
        "These `number` or `numbers` metrics have no unit and no row in "
        f"REVIEWED_UNITLESS_NUMBERS: {missing}. A physical quantity needs a unit. Add one, or add "
        "a row that says why the value has none (a ratio, a library-relative area, a count)."
    )
    assert not stale, (
        f"These rows of REVIEWED_UNITLESS_NUMBERS name no unitless `number` or `numbers` "
        f"metric: {stale}. Remove them."
    )


#: A description that names a count.
COUNT_DESCRIPTION = re.compile(r"^(the |total )*(number|count) of\b", re.IGNORECASE)

#: Every unitless `integer` metric whose description does not start with "Number of" or "Count
#: of", and why it is a count.
REVIEWED_UNITLESS_INTEGERS: Dict[str, str] = {
    "cells": "count of cell instances; the description says 'Total number of cells' (dc, yosys)",
    "formal.assertions": "count of assertions, one for each instance",
    "formal.assumptions": "count of assumptions, one for each instance",
    "formal.covers": "count of cover statements, one for each instance",
    "formal.depth": "count of steps; the description says 'The number of steps'",
    "lut_use.logic": "count of LUTs, an estimate of the footprint",
    "lut_use.ram": "count of LUTs, an estimate of the footprint",
    "lut_use.srl": "count of LUTs, an estimate of the footprint",
}


def _integer_problems(
    metrics: Dict[str, Dict[str, Tuple[str, str | None, str]]], reviewed: Dict[str, str]
) -> List[str]:
    """Return a problem for each unitless `integer` that is no count and has no reviewed row.

    `metrics` maps path -> flow name -> (type, unit, description)."""
    problems = []
    unitless = set()
    for path, flows in metrics.items():
        for flow, (kind, unit, text) in flows.items():
            if kind != "integer" or unit is not None:
                continue
            unitless.add(path)
            if path not in reviewed and not COUNT_DESCRIPTION.match(text):
                problems.append(f"{flow}: {path} is an `integer` without a unit and is no count")
    problems.extend(
        f"The row of {path!r} names no unitless `integer`"
        for path in sorted(set(reviewed) - unitless)
    )
    return problems


def _described_with_text() -> Dict[str, Dict[str, Tuple[str, str | None, str]]]:
    found: Dict[str, Dict[str, Tuple[str, str | None, str]]] = defaultdict(dict)
    for flow_class in BUILT_IN_FLOWS:
        for path, spec in flow_class.results_description.items():
            found[path][flow_class.name] = (spec.type, spec.unit, spec.description)
    return found


def test_every_integer_without_a_unit_is_a_count():
    problems = _integer_problems(_described_with_text(), REVIEWED_UNITLESS_INTEGERS)
    assert not problems, (
        f"{problems}. A physical quantity needs a unit, also when it is an `integer`. Give the "
        "metric a unit. If it is a count, start its description with 'Number of' or 'Count of'. "
        "Otherwise add a row to REVIEWED_UNITLESS_INTEGERS that says why it has no unit. Remove "
        "a row that names no unitless `integer`."
    )


def test_a_unitless_physical_integer_is_found():
    def metrics(unit, text):
        return {"delay": {"fake": ("integer", unit, text)}}

    assert _integer_problems(metrics(None, "Propagation delay."), {})
    assert not _integer_problems(metrics("ps", "Propagation delay."), {})
    assert not _integer_problems(metrics(None, "Number of stages in the path."), {})
    assert not _integer_problems(metrics(None, "Propagation delay."), {"delay": "a count"})
    assert _integer_problems(metrics("ps", "Propagation delay."), {"delay": "a count"})
    assert _integer_problems(metrics(None, "Number of stages."), {"gone": "a count"})


#: Text of a row of a table of shared paths that several flows share one for one.
_COCOTB = (
    "Summary of one cocotb regression. Verified: every simulator flow takes the path from the "
    "one COCOTB_METRICS tuple, and `Cocotb.add_results` (cocotb.py) writes the value."
)
_SIM = (
    "Property of one simulator run. Verified: every simulator flow writes it through the one "
    "`judge_evidence` rule of flow/sim.py."
)


def _row(families: str, text: str) -> Tuple[FrozenSet[str], str]:
    """Return a row of `REVIEWED_SHARED_PATHS`: the tool families that it covers, and its text."""
    return frozenset(families.split()), text


#: Every path that two or more tool families report, with the families that the row covers and
#: what the path means. The text has the quantity, the population and the aggregation, and then the
#: evidence or `unverified:` and the work that would verify it. A new share needs a row, a row needs
#: a share, and a family that starts to report a shared path needs a review of the row.
REVIEWED_SHARED_PATHS: Dict[str, Tuple[FrozenSet[str], str]] = {
    "bram": _row(
        "diamond nextpnr",
        (
            "Block RAM primitives used in the implemented design. nextpnr counts the bels of the BRAM "
            "bel types; Diamond reads the map report. unverified: run one design with block RAM "
            "through Diamond and nextpnr and compare the counts"
        ),
    ),
    "cells": _row(
        "dc yosys",
        (
            "Cell instances over the whole hierarchy of the netlist. yosys counts the leaf cells of "
            "`stat` (passes/cmds/stat.cc); Design Compiler prints 'Number of cells'. unverified: "
            "run Design Compiler and check that its count covers the hierarchy the way yosys does"
        ),
    ),
    "clock_frequency": _row(
        "diamond nextpnr vivado",
        (
            "Constrained clock frequency, as given to the tool. Vivado: the first clock of the Clock "
            "Summary table. nextpnr and Diamond: the constraint of the reported clock. unverified "
            "for Diamond: run it with two clocks and check which one it reports"
        ),
    ),
    "clock_period": _row(
        "dc diamond nextpnr vivado",
        (
            "Constrained clock period in ns, as given to the tool; the capture edge of the one "
            "clock for Design Compiler. Vivado: the first clock of the Clock Summary table. "
            "unverified for Design Compiler and Diamond: run them with two clocks and check which "
            "clock they report"
        ),
    ),
    "clock_port": _row(
        "diamond nextpnr",
        (
            "Top-level port of the reported clock. nextpnr: the only clock domain, when certain. "
            "unverified for Diamond: run it with two clocks and check which port it reports"
        ),
    ),
    "cocotb.errors": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "cocotb.failures": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "cocotb.sim_time": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "cocotb.skipped": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "cocotb.tests": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "cocotb.time": _row("ghdl iverilog nvc verilator xezim", _COCOTB),
    "dsp": _row(
        "diamond nextpnr vivado yosys",
        (
            "DSP blocks used in the implemented design. nextpnr counts DSP bels; Vivado reads the "
            "utilization report; yosys_fpga sums the DSP primitives of the netlist. Diamond counts "
            "multipliers, ALUs and pre-adders, so a multiply-accumulate counts twice. unverified: "
            "run one multiply-accumulate design through each tool and compare the counts"
        ),
    ),
    "ff": _row(
        "diamond ise nextpnr quartus vivado yosys",
        (
            "Flip-flops used. nextpnr counts the flip-flop bels, Vivado reads the utilization "
            "report, yosys_fpga sums the FF primitives of the netlist. ISE and Diamond read their "
            "map or synthesis reports. unverified for ISE, Diamond and Quartus (dedicated logic "
            "registers): run one design through each tool and compare the counts"
        ),
    ),
    "fmax": _row(
        "dc ise nextpnr openroad quartus vivado",
        (
            "Achievable frequency in MHz: the minimum over the clocks that the tool times. OpenROAD: "
            "the minimum of its own timing__fmax__clock:* metrics (src/Metrics.tcl). Vivado: "
            "1000 / (period - WNS) per clock. nextpnr: the lowest achieved frequency of its "
            "constrained clock domains. ISE gives a synthesis estimate, and Design Compiler writes "
            "it only for one clock. unverified for ISE, Design "
            "Compiler and Quartus: run each tool and compare with 1000 / (period - wns)"
        ),
    ),
    "fmax_by_clock": _row(
        "openroad quartus vivado",
        (
            "Achievable frequency of each clock in MHz: the minimum over the corners. OpenROAD: "
            "timing__fmax__clock:* (src/Metrics.tcl). Vivado: from the Intra Clock Table. unverified "
            "for Quartus: run a design with two clocks and two timing models, and check that each "
            "key is the minimum over the models"
        ),
    ),
    "hold_violations": _row(
        "openroad vivado",
        (
            "Failing hold endpoints, counted once each. OpenROAD: timing__drv__hold_violation_count "
            "(src/Metrics.tcl). Vivado: the Design Timing Summary (vivado_synth.py). Both are one "
            "analysis, with no corner merge"
        ),
    ),
    "latch": _row(
        "vivado yosys",
        (
            "Latches in the implemented design. Vivado reads 'Register as Latch' of the utilization "
            "report; yosys_fpga counts the LDCE and LDPE primitives of the netlist. The two agree "
            "for a design without latch inference outside these primitives. unverified: run one "
            "design with a latch through both tools and compare"
        ),
    ),
    "library_area.cells": _row(
        "dc yosys",
        (
            "Sum of the Liberty `area` of the cell instances; no unit, relative to the libraries. "
            "yosys: passes/cmds/stat.cc, every cell with an area, others add 0. Design Compiler: "
            "'Total cell area'. unverified: run Design Compiler and yosys on one netlist with one "
            "library and compare"
        ),
    ),
    "lut": _row(
        "diamond ise nextpnr quartus vivado yosys",
        (
            "LUTs used. nextpnr and Vivado count tool cells; yosys_fpga is an estimate whose method "
            "`lut_basis.method` names. Quartus counts combinational ALUTs. ISE and Diamond read "
            "their reports. unverified for ISE, Diamond and Quartus: run one design through each "
            "tool and compare"
        ),
    ),
    "lut_basis.method": _row(
        "nextpnr yosys",
        (
            "Label of the method behind the `lut` count of this flow, as text. The words differ by "
            "design, because each flow counts its own way. Verified: both flows write it next to "
            "`lut`"
        ),
    ),
    "lut_basis.stage": _row(
        "nextpnr yosys",
        (
            "Label of the stage that the `lut` count describes, as text. The words differ by design, "
            "because each flow stops at its own stage. Verified: both flows write it next to `lut`"
        ),
    ),
    "platform": _row(
        "openroad yosys",
        (
            "The platform of the run: name, provider, version, corner and the source of each "
            "value. It is a record of the one platform model, not a measurement. Verified: both "
            "families write that model"
        ),
    ),
    "setup_violations": _row(
        "openroad vivado",
        (
            "Failing setup endpoints, counted once each. OpenROAD: timing__drv__setup_violation_count "
            "(src/Metrics.tcl). Vivado: the Design Timing Summary (vivado_synth.py). Both are "
            "one analysis, with no corner merge"
        ),
    ),
    "sim.ended_by": _row("bsc ghdl iverilog modelsim nvc vcs verilator vivado xezim yosys", _SIM),
    "sim.errors": _row("bsc ghdl iverilog modelsim nvc vcs verilator vivado xezim yosys", _SIM),
    "sim.evidence": _row("bsc ghdl iverilog modelsim nvc vcs verilator vivado xezim yosys", _SIM),
    "sim.time": _row("bsc ghdl iverilog modelsim nvc vcs verilator vivado xezim yosys", _SIM),
    "sim.warnings": _row("bsc ghdl iverilog modelsim nvc vcs verilator vivado xezim yosys", _SIM),
    "slice": _row(
        "diamond ise nextpnr vivado",
        (
            "Slices or logic elements occupied. Vivado reads the utilization report; nextpnr counts "
            "slice bels. ISE and Diamond read their reports. The unit of occupancy differs by "
            "family. unverified: run one design through each tool and compare"
        ),
    ),
    "ths": _row(
        "openroad vivado",
        (
            "Total negative hold slack in ns: the sum over the failing hold endpoints, at most 0. "
            "OpenROAD: timing__hold__tns (src/Metrics.tcl). Vivado: the Design Timing Summary "
            "(vivado_synth.py). Both are one analysis, with no corner merge"
        ),
    ),
    "tns": _row(
        "openroad vivado",
        (
            "Total negative setup slack in ns: the sum over the failing setup endpoints, at most 0. "
            "OpenROAD: timing__setup__tns (src/Metrics.tcl). Vivado: the Design Timing Summary "
            "(vivado_synth.py). Both are one analysis, with no corner merge"
        ),
    ),
    "whs": _row(
        "dc diamond openroad quartus vivado",
        (
            "Signed worst hold slack in ns: the minimum over the hold paths, the clocks and the "
            "corners that the tool times. OpenROAD: timing__hold__ws. Vivado: the Design Timing "
            "Summary. Design Compiler: the minimum delay report over the path groups. Quartus and "
            "Diamond: the multicorner worst case. unverified for Design Compiler, Quartus and "
            "Diamond: run each tool and compare with its own report"
        ),
    ),
    "wns": _row(
        "dc diamond ise nextpnr openroad quartus vivado",
        (
            "Signed worst setup slack in ns: the minimum over the setup paths, the clocks and the "
            "corners that the tool times. OpenROAD: timing__setup__ws, signed. Vivado: the Design "
            "Timing Summary. nextpnr is the documented exception: it covers the paths inside each "
            "constrained clock domain only (nextpnr.py). ISE gives a synthesis estimate. unverified "
            "for Design Compiler, Quartus, Diamond and ISE: run each tool and compare with its own "
            "report"
        ),
    ),
}


def _shared_path_problems(
    families: Dict[str, Set[str]], reviewed: Dict[str, Tuple[FrozenSet[str], str]]
) -> List[str]:
    """Return a problem for each shared path whose reviewed row does not match its families."""
    problems = []
    for path in sorted(reviewed):
        if path not in families or len(families[path]) < 2:
            problems.append(
                f"The row of {path!r} names no path that two tool families report. Remove it."
            )
            continue
        covered, text = reviewed[path]
        actual = families[path]
        if not text.strip():
            problems.append(f"The row of {path!r} has no text.")
        for family in sorted(actual - covered):
            problems.append(
                f"The tool family {family!r} reports {path!r}, which its reviewed row does not "
                f"cover (it covers {sorted(covered)}). Check that {family!r} means the same "
                "quantity, population and aggregation as the row says. Then add the family to "
                "the row and extend its text with the evidence or with 'unverified:'. If it "
                f"means something else, give {family!r} another path."
            )
        for family in sorted(covered - actual):
            problems.append(
                f"The row of {path!r} covers {family!r}, which no longer reports it. Remove the "
                "family from the row, and check the text."
            )
    for path in sorted(set(families) - set(reviewed)):
        if len(families[path]) >= 2:
            problems.append(
                f"{path!r} is reported by {sorted(families[path])} and has no row in "
                "REVIEWED_SHARED_PATHS. A shared path must mean one quantity. Check the quantity, "
                "the population and the aggregation in each tool. Then add a row with the "
                "families and the evidence, or with 'unverified:' and the work that would verify "
                "the share. If the tools differ, give one of them another path."
            )
    return problems


def test_every_shared_path_has_a_reviewed_row_that_covers_its_families():
    problems = _shared_path_problems(_families(), REVIEWED_SHARED_PATHS)
    assert not problems, "\n".join(problems)


def test_a_new_family_on_a_shared_path_fails_the_check():
    reviewed = {"cells": (frozenset({"dc", "yosys"}), "Cell instances.")}
    assert not _shared_path_problems({"cells": {"dc", "yosys"}}, reviewed)
    problems = _shared_path_problems({"cells": {"dc", "yosys", "nextpnr"}}, reviewed)
    assert len(problems) == 1
    assert "'nextpnr'" in problems[0] and "'cells'" in problems[0]
    assert "another path" in problems[0]
    assert _shared_path_problems({"cells": {"dc"}}, reviewed)  # no longer shared
    assert _shared_path_problems({"cells": {"dc", "nextpnr"}}, reviewed)  # yosys left, nextpnr new
    assert _shared_path_problems({"cells": {"dc", "yosys"}, "ff": {"dc", "ise"}}, reviewed)


def test_the_tool_family_is_the_first_word_of_the_flow_name():
    families = {cls.name: tool_family(cls) for cls in BUILT_IN_FLOWS}
    assert families["openroad_place"] == "openroad"
    assert families["vivado_power"] == "vivado"
    assert families["yosys_fpga"] == "yosys"
    assert families["nextpnr"] == "nextpnr"
