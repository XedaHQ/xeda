"""Descriptors of the metrics that a flow writes to `results.json`.

A flow declares each metric as a `MetricSpec`: a description, a type and a unit. `Flow` keeps them
in `results_description`, and `xeda list-results` reports them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional

from ..units import is_metric_unit

__all__ = [
    "COCOTB_METRICS",
    "COMMON_METRICS",
    "METRIC_PATH",
    "MetricSpec",
    "MetricType",
    "describe_results",
    "metric_unit_known",
    "non_finite_in",
    "value_fits",
]

#: The type of the value of a metric.
#:
#: - `number`, `integer`, `boolean`, `text`: one value.
#: - `text_list`: an ordered list of text.
#: - `counts`: a table of text to integer.
#: - `numbers`: a table of text to number, all in the unit of the metric.
#: - `object`: a JSON object whose fields the description documents.
MetricType = Literal[
    "number", "integer", "boolean", "text", "text_list", "counts", "numbers", "object"
]


#: The spelling of a metric path: dot-separated segments of lower snake case, each starting with a
#: letter. Match a path with `METRIC_PATH.fullmatch`.
METRIC_PATH = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")


@dataclass(frozen=True)
class MetricSpec:
    """What one metric of a flow is: a description, the type of its value and the unit.

    `unit` is a unit text of the units table of `xeda.units`, such as `MHz`. For the type
    `numbers`, it is the unit of every value. A metric that is not a physical measurement has no
    unit. A metric in the units of the platform (an area in the unit of the Liberty files) has no
    unit either, and its description says so.
    """

    description: str
    type: MetricType
    unit: Optional[str] = None


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _table_of(is_value: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, Mapping) and all(
        isinstance(key, str) and is_value(item) for key, item in value.items()
    )


_VALUE_TESTS: Dict[str, Callable[[Any], bool]] = {
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "text": lambda value: isinstance(value, str),
    "text_list": lambda value: isinstance(value, (list, tuple))
    and all(isinstance(item, str) for item in value),
    "counts": _table_of(_is_integer),
    "numbers": _table_of(_is_number),
    "object": lambda value: isinstance(value, Mapping),
}


def value_fits(metric_type: str, value: Any) -> bool:
    """Return whether `value` is a value of the metric type `metric_type`.

    An `integer` is an `int` and no `bool`. A `number` is an `int` or a `float`, and no `bool`. A
    `text_list` is a list of text, a `counts` table maps text to integers, a `numbers` table maps
    text to numbers and an `object` is a table. The test is about the type only: whether a number
    is finite is another question (`non_finite_in`)."""
    return _VALUE_TESTS[metric_type](value)


def non_finite_in(value: Any) -> Optional[str]:
    """Return the place of the first number in `value` that is not finite, or None.

    `value` may be a number, or a list or table of them at any depth. The place is `""` for the
    value itself, and otherwise the position of the number: `[2]` for the third item of a list,
    `.name` for the entry `name` of a table."""
    if isinstance(value, float):
        return None if math.isfinite(value) else ""
    if isinstance(value, Mapping):
        for key, item in value.items():
            found = non_finite_in(item)
            if found is not None:
                return f".{key}{found}"
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = non_finite_in(item)
            if found is not None:
                return f"[{index}]{found}"
    return None


def metric_unit_known(unit: str) -> bool:
    """Return whether `unit` is a unit text of the units table of `xeda.units`."""
    return is_metric_unit(unit)


#: Metrics that several flows report with the same meaning. A flow declares the subset it
#: reports through `describe_results`, so `xeda list-results <flow>` never advertises a metric
#: that the flow does not produce.
COMMON_METRICS: Dict[str, MetricSpec] = {
    # timing
    "fmax": MetricSpec(
        "Maximum achievable clock frequency, derived from the constrained period and the worst "
        "negative slack. This is the achieved result, unlike `clock_frequency`. Each tool times "
        "a design with its own model: compare `fmax` only between runs of the same tool.",
        "number",
        "MHz",
    ),
    "clock_period": MetricSpec("Constrained clock period, as given to the tool.", "number", "ns"),
    "clock_frequency": MetricSpec(
        "Constrained clock frequency. This is the constraint that was given to the tool, not "
        "the achieved maximum (see `fmax`).",
        "number",
        "MHz",
    ),
    "clock_name": MetricSpec("Name of the clock the reported timing refers to.", "text"),
    "clock_port": MetricSpec("Design port the reported clock is attached to.", "text"),
    "wns": MetricSpec(
        "Worst negative slack over all setup paths. Negative means setup timing is not met.",
        "number",
        "ns",
    ),
    "whs": MetricSpec(
        "Worst hold slack over all hold paths. Negative means a hold violation.", "number", "ns"
    ),
    "tns": MetricSpec(
        "Total negative slack, summed over every failing setup endpoint.", "number", "ns"
    ),
    "setup_wns": MetricSpec("Worst negative slack on setup paths.", "number", "ns"),
    "hold_wns": MetricSpec("Worst negative slack on hold paths.", "number", "ns"),
    "worst_slack": MetricSpec("Worst slack over all analyzed paths.", "number", "ns"),
    "setup_violations": MetricSpec("Number of endpoints that fail setup timing.", "integer"),
    "hold_violations": MetricSpec("Number of endpoints that fail hold timing.", "integer"),
    "num_violating_paths": MetricSpec("Number of paths that fail setup timing.", "integer"),
    "hold_num_violating_paths": MetricSpec("Number of paths that fail hold timing.", "integer"),
    "minimum_period": MetricSpec("Minimum achievable clock period.", "number", "ns"),
    # FPGA resources
    "lut": MetricSpec("Number of LUTs used.", "integer"),
    "ff": MetricSpec("Number of flip-flops (registers) used.", "integer"),
    "slice": MetricSpec("Number of slices / logic elements occupied.", "integer"),
    "dsp": MetricSpec("Number of DSP blocks used.", "integer"),
    "bram": MetricSpec("Number of block RAM primitives used.", "integer"),
    # ASIC area: the unit of an area is the one of the platform's Liberty files, so it has no
    # unit here
    "area": MetricSpec(
        "Cell area of the synthesized design, in the technology's area unit. Compare areas only "
        "within one platform.",
        "number",
    ),
    "design_area": MetricSpec(
        "Area of the placed design, in `design_area_unit`. Compare areas only within one "
        "platform.",
        "number",
    ),
    "design_area_unit": MetricSpec("Unit the reported `design_area` is expressed in.", "text"),
    "utilization_percent": MetricSpec("Core area utilization, in percent.", "number"),
    "utilization_ratio": MetricSpec("Core area utilization, as a ratio in 0..1.", "number"),
    "aspect_ratio": MetricSpec("Core height divided by core width.", "number"),
    "area_combinational": MetricSpec(
        "Area of the combinational cells, in the technology's area unit.", "number"
    ),
    "area_noncombinational": MetricSpec(
        "Area of the sequential (state-holding) cells, in the technology's area unit.", "number"
    ),
    "area_macro_bbox": MetricSpec(
        "Area of macros and black boxes, in the technology's area unit.", "number"
    ),
    "area_cell_total": MetricSpec(
        "Total cell area, excluding net interconnect, in the technology's area unit.", "number"
    ),
    "area_total": MetricSpec(
        "Total area, including net interconnect where the tool reports it, in the technology's "
        "area unit.",
        "number",
    ),
    "area_core": MetricSpec("Area of the core region, in the technology's area unit.", "number"),
    "utilization": MetricSpec(
        "Share of the core area that the standard cells fill, as a ratio from 0 to 1.", "number"
    ),
    "power": MetricSpec("Total power of the design, as the tool estimates it.", "number", "W"),
    "io": MetricSpec("Number of I/O buffers/pads used.", "integer"),
    "timing_met": MetricSpec(
        "Whether every constrained clock domain met its constraint.", "boolean"
    ),
    "clock_domains": MetricSpec("Number of clock domains the timing analysis reported.", "integer"),
    # simulation: how it ended, from the simulator's end record (`SimFlow.simulation_evidence`)
    "sim.evidence": MetricSpec(
        "Normalized simulation end record, including measured cycles and events; preserved even "
        "when the evidence fails the simulation verdict.",
        "object",
    ),
    "sim.ended_by": MetricSpec(
        "What ended the simulation: `finish` ($finish), `stop_time` (the requested stop time, "
        "reached), `max_cycles`, `exit` (the testbench's own driver exited), `drained` (no "
        "event left, and no $finish), `error`, `fatal`, or `unknown`. Only `finish`, a "
        "requested `stop_time` or `max_cycles`, and an `exit` with status 0 can pass.",
        "text",
    ),
    "sim.time": MetricSpec(
        "Simulated time at the end, as an integer number of femtoseconds. The metric is absent "
        "when the simulator did not report the time.",
        "integer",
        "fs",
    ),
    "sim.errors": MetricSpec(
        "Number of errors the simulation reported: $error, failed assertions, $stop and $fatal.",
        "integer",
    ),
    "sim.warnings": MetricSpec("Number of warnings the simulation reported.", "integer"),
    # cocotb: added to the results of every simulation flow that runs cocotb
    "cocotb.tests": MetricSpec("Number of cocotb tests that ran.", "integer"),
    "cocotb.errors": MetricSpec("Number of cocotb tests that errored.", "integer"),
    "cocotb.failures": MetricSpec("Number of cocotb tests that failed.", "integer"),
    "cocotb.skipped": MetricSpec("Number of cocotb tests that were skipped.", "integer"),
    "cocotb.time": MetricSpec("Wall-clock duration of the cocotb regression.", "number", "s"),
    "cocotb.sim_time": MetricSpec("Total simulated time of the cocotb regression.", "number", "ns"),
}


#: The metrics that `Cocotb.add_results` adds to the results of a simulation flow that runs cocotb.
COCOTB_METRICS = (
    "cocotb.tests",
    "cocotb.errors",
    "cocotb.failures",
    "cocotb.skipped",
    "cocotb.time",
    "cocotb.sim_time",
)


def describe_results(*shared: str, **own: MetricSpec) -> Dict[str, MetricSpec]:
    """Build a flow's `results_description` from the shared metrics plus its own.

    Positional arguments name keys of `COMMON_METRICS`. A keyword argument adds a metric of the
    flow or replaces a shared one. A shared key that `COMMON_METRICS` lacks raises `KeyError`.
    """
    described: Dict[str, MetricSpec] = {}
    for key in shared:
        try:
            described[key] = COMMON_METRICS[key]
        except KeyError:
            raise KeyError(
                f"No shared description for result key {key!r}. Add it to COMMON_METRICS, or "
                "pass it as a keyword argument."
            ) from None
    for key, spec in own.items():
        if not isinstance(spec, MetricSpec):
            raise TypeError(f"The metric {key!r} is described by a MetricSpec, not {spec!r}.")
        described[key] = spec
    return described
