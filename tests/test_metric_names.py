"""The names of the metrics: one path means one quantity in every built-in flow.

A metric path names one tuple: quantity, population, aggregation, analysis scope, sign, type and
unit. The checks below read the `results_description` of every built-in flow. Two checks keep a
reviewed table each: `REVIEWED_UNITLESS_NUMBERS` and `REVIEWED_SHARED_PATHS`. A new metric of
either kind fails until a person adds a row with a reason.
"""

import re
from collections import defaultdict
from typing import Dict, List, Set, Tuple, Type

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


def _shared_paths() -> Set[str]:
    return {path for path, families in _families().items() if len(families) >= 2}


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


#: Text of a row of a table of shared paths that several flows share one for one.
_COCOTB = (
    "Summary of one cocotb regression. Verified: every simulator flow takes the path from the "
    "one COCOTB_METRICS tuple, and `Cocotb.add_results` (cocotb.py) writes the value."
)
_SIM = (
    "Property of one simulator run. Verified: every simulator flow writes it through the one "
    "`judge_evidence` rule of flow/sim.py."
)

#: Every path that two or more tool families report, with what it means. A row has the quantity,
#: the population and the aggregation, and then the evidence or `unverified:` and the work that
#: would verify it. A new share needs a row, and a row needs a share.
REVIEWED_SHARED_PATHS: Dict[str, str] = {
    "bram": (
        "Block RAM primitives used in the implemented design. nextpnr counts the bels of the BRAM "
        "bel types; Diamond reads the map report. unverified: run one design with block RAM "
        "through Diamond and nextpnr and compare the counts"
    ),
    "cells": (
        "Cell instances over the whole hierarchy of the netlist. yosys counts the leaf cells of "
        "`stat` (passes/cmds/stat.cc); Design Compiler prints 'Number of cells'. unverified: "
        "run Design Compiler and check that its count covers the hierarchy the way yosys does"
    ),
    "clock_frequency": (
        "Constrained clock frequency, as given to the tool. Vivado: the first clock of the Clock "
        "Summary table. nextpnr and Diamond: the constraint of the reported clock. unverified "
        "for Diamond: run it with two clocks and check which one it reports"
    ),
    "clock_period": (
        "Constrained clock period in ns, as given to the tool; the capture edge of the one "
        "clock for Design Compiler. Vivado: the first clock of the Clock Summary table. "
        "unverified for Design Compiler and Diamond: run them with two clocks and check which "
        "clock they report"
    ),
    "clock_port": (
        "Top-level port of the reported clock. nextpnr: the only clock domain, when certain. "
        "unverified for Diamond: run it with two clocks and check which port it reports"
    ),
    "cocotb.errors": _COCOTB,
    "cocotb.failures": _COCOTB,
    "cocotb.sim_time": _COCOTB,
    "cocotb.skipped": _COCOTB,
    "cocotb.tests": _COCOTB,
    "cocotb.time": _COCOTB,
    "dsp": (
        "DSP blocks used in the implemented design. nextpnr counts DSP bels; Vivado reads the "
        "utilization report; yosys_fpga sums the DSP primitives of the netlist. Diamond counts "
        "multipliers, ALUs and pre-adders, so a multiply-accumulate counts twice. unverified: "
        "run one multiply-accumulate design through each tool and compare the counts"
    ),
    "ff": (
        "Flip-flops used. nextpnr counts the flip-flop bels, Vivado reads the utilization "
        "report, yosys_fpga sums the FF primitives of the netlist. ISE and Diamond read their "
        "map or synthesis reports. unverified for ISE, Diamond and Quartus (dedicated logic "
        "registers): run one design through each tool and compare the counts"
    ),
    "fmax": (
        "Achievable frequency in MHz: the minimum over the clocks that the tool times. OpenROAD: "
        "the minimum of its own timing__fmax__clock:* metrics (src/Metrics.tcl). Vivado: "
        "1000 / (period - WNS) per clock. nextpnr: the lowest achieved frequency of its "
        "constrained clock domains. ISE gives a synthesis estimate, and Design Compiler writes "
        "it only for one clock. unverified for ISE, Design "
        "Compiler and Quartus: run each tool and compare with 1000 / (period - wns)"
    ),
    "fmax_by_clock": (
        "Achievable frequency of each clock in MHz: the minimum over the corners. OpenROAD: "
        "timing__fmax__clock:* (src/Metrics.tcl). Vivado: from the Intra Clock Table. unverified "
        "for Quartus: run a design with two clocks and two timing models, and check that each "
        "key is the minimum over the models"
    ),
    "hold_violations": (
        "Failing hold endpoints, counted once each. OpenROAD: timing__drv__hold_violation_count "
        "(src/Metrics.tcl). Vivado: the Design Timing Summary (vivado_synth.py). Both are one "
        "analysis, with no corner merge"
    ),
    "latch": (
        "Latches in the implemented design. Vivado reads 'Register as Latch' of the utilization "
        "report; yosys_fpga counts the LDCE and LDPE primitives of the netlist. The two agree "
        "for a design without latch inference outside these primitives. unverified: run one "
        "design with a latch through both tools and compare"
    ),
    "library_area.cells": (
        "Sum of the Liberty `area` of the cell instances; no unit, relative to the libraries. "
        "yosys: passes/cmds/stat.cc, every cell with an area, others add 0. Design Compiler: "
        "'Total cell area'. unverified: run Design Compiler and yosys on one netlist with one "
        "library and compare"
    ),
    "lut": (
        "LUTs used. nextpnr and Vivado count tool cells; yosys_fpga is an estimate whose method "
        "`lut_basis.method` names. Quartus counts combinational ALUTs. ISE and Diamond read "
        "their reports. unverified for ISE, Diamond and Quartus: run one design through each "
        "tool and compare"
    ),
    "lut_basis.method": (
        "Label of the method behind the `lut` count of this flow, as text. The words differ by "
        "design, because each flow counts its own way. Verified: both flows write it next to "
        "`lut`"
    ),
    "lut_basis.stage": (
        "Label of the stage that the `lut` count describes, as text. The words differ by design, "
        "because each flow stops at its own stage. Verified: both flows write it next to `lut`"
    ),
    "platform": (
        "The platform of the run: name, provider, version, corner and the source of each "
        "value. It is a record of the one platform model, not a measurement. Verified: both "
        "families write that model"
    ),
    "setup_violations": (
        "Failing setup endpoints, counted once each. OpenROAD: timing__drv__setup_violation_count "
        "(src/Metrics.tcl). Vivado: the Design Timing Summary (vivado_synth.py). Both are "
        "one analysis, with no corner merge"
    ),
    "sim.ended_by": _SIM,
    "sim.errors": _SIM,
    "sim.evidence": _SIM,
    "sim.time": _SIM,
    "sim.warnings": _SIM,
    "slice": (
        "Slices or logic elements occupied. Vivado reads the utilization report; nextpnr counts "
        "slice bels. ISE and Diamond read their reports. The unit of occupancy differs by "
        "family. unverified: run one design through each tool and compare"
    ),
    "ths": (
        "Total negative hold slack in ns: the sum over the failing hold endpoints, at most 0. "
        "OpenROAD: timing__hold__tns (src/Metrics.tcl). Vivado: the Design Timing Summary "
        "(vivado_synth.py). Both are one analysis, with no corner merge"
    ),
    "tns": (
        "Total negative setup slack in ns: the sum over the failing setup endpoints, at most 0. "
        "OpenROAD: timing__setup__tns (src/Metrics.tcl). Vivado: the Design Timing Summary "
        "(vivado_synth.py). Both are one analysis, with no corner merge"
    ),
    "whs": (
        "Signed worst hold slack in ns: the minimum over the hold paths, the clocks and the "
        "corners that the tool times. OpenROAD: timing__hold__ws. Vivado: the Design Timing "
        "Summary. Design Compiler: the minimum delay report over the path groups. Quartus and "
        "Diamond: the multicorner worst case. unverified for Design Compiler, Quartus and "
        "Diamond: run each tool and compare with its own report"
    ),
    "wns": (
        "Signed worst setup slack in ns: the minimum over the setup paths, the clocks and the "
        "corners that the tool times. OpenROAD: timing__setup__ws, signed. Vivado: the Design "
        "Timing Summary. nextpnr is the documented exception: it covers the paths inside each "
        "constrained clock domain only (nextpnr.py). ISE gives a synthesis estimate. unverified "
        "for Design Compiler, Quartus, Diamond and ISE: run each tool and compare with its own "
        "report"
    ),
}


def test_every_shared_path_has_a_reviewed_row():
    shared = _shared_paths()
    families = _families()
    missing = {path: sorted(families[path]) for path in sorted(shared - set(REVIEWED_SHARED_PATHS))}
    stale = sorted(set(REVIEWED_SHARED_PATHS) - shared)
    assert not missing, (
        f"These paths are reported by two or more tool families and have no row in "
        f"REVIEWED_SHARED_PATHS: {missing}. A shared path must mean one quantity. Check the "
        "quantity, the population and the aggregation in each tool. Then add a row with the "
        "evidence, or with 'unverified:' and the work that would verify the share. If the tools "
        "differ, give one of them another path."
    )
    assert not stale, (
        f"These rows of REVIEWED_SHARED_PATHS name no path that two tool families report: "
        f"{stale}. Remove them."
    )
    empty = sorted(path for path, text in REVIEWED_SHARED_PATHS.items() if not text.strip())
    assert not empty, f"These rows have no text: {empty}."


def test_the_tool_family_is_the_first_word_of_the_flow_name():
    families = {cls.name: tool_family(cls) for cls in BUILT_IN_FLOWS}
    assert families["openroad_place"] == "openroad"
    assert families["vivado_power"] == "vivado"
    assert families["yosys_fpga"] == "yosys"
    assert families["nextpnr"] == "nextpnr"
