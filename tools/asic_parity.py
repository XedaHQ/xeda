#!/usr/bin/env python3
"""Compare xeda's ASIC flow with OpenROAD-flow-scripts (ORFS) on the same design.

The tool is a faithfulness and regression check of the xeda scripts. It is no tuning tool. For one
ORFS design it runs xeda and ORFS with equivalent settings, and prints a Markdown table.

* The ORFS reference row is ORFS's own flow: `make do-yosys-canonicalize do-yosys`, then the place
  and route targets. The tool runs it first when `--root` has no such row.
* The half `synthesis` is xeda's `yosys` flow for the netlist. ORFS places and routes that
  netlist, so the row shows what the xeda synthesis script costs or gains.
* The half `pnr` is xeda's OpenROAD flows on the netlist of ORFS: `openroad` with its default
  producers `openroad_place`, `openroad_cts` and `openroad_grt`. The half `both` is the xeda flow
  chain from the sources: `yosys`, `openroad_place`, `openroad_cts`, `openroad_grt` and `openroad`.
  The OpenROAD flows read the constraint file of ORFS as a source of the design. A xeda that lacks
  these flows stops the tool, which names the missing flows.

The sources, the include directories, the top module, the front end and the constraint file come
from ORFS (`make print-<VARIABLE>`), so both tools read the same inputs. A design file in
`tools/asic_parity/designs/<platform>/<design>.yaml` holds the xeda settings that match the
`config.mk` of the design. The tool refuses a design file that disagrees with the ORFS
variables or with the clock of the constraint file.

Examples (the global options go before the command):
  asic_parity.py --orfs <checkout> --root runs run nangate45/gcd --half synthesis
  asic_parity.py --root runs table
  asic_parity.py --orfs <checkout> --root runs script nangate45/gcd

The ORFS checkout is read-only. All output goes under `--root`:
  <root>/<platform>/<design>/orfs/result.json            the ORFS reference row
  <root>/<platform>/<design>/<half>/result.json          the row of a half
Each tool runs with a scrubbed environment and its own `HOME` and `TMPDIR` under `--root`.
"""

import argparse
import hashlib
import json
import math
import os
import platform as host_platform
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

TOOLS = Path(__file__).resolve().parent
DATA = TOOLS / "asic_parity"
DESIGNS = DATA / "designs"
OVERRIDES = DATA / "overrides"

HALVES = ("synthesis", "pnr", "both")
ROW_ORDER = ("orfs", *HALVES)
#: The make targets of the place and route stages, one `make` call each.
PNR_TARGETS = ["do-1_synth", "do-floorplan", "do-place", "do-cts", "do-route", "do-finish"]
#: The variables that the tool reads from ORFS with `make print-<VARIABLE>`.
MAKE_VARS = [
    "DESIGN_NAME", "DESIGN_NICKNAME", "PLATFORM", "PLATFORM_DIR", "VERILOG_FILES",
    "VERILOG_INCLUDE_DIRS", "SDC_FILE", "ABC_AREA", "SWAP_ARITH_OPERATORS",
    "SYNTH_WRAPPED_OPERATORS", "SYNTH_HIERARCHICAL", "OPENROAD_HIERARCHICAL", "SYNTH_OPERATIONS_ARGS",
    "SYNTH_HDL_FRONTEND", "SYNTH_MINIMUM_KEEP_SIZE", "ADDER_MAP_FILE", "LATCH_MAP_FILE",
    "CLKGATE_MAP_FILE", "LIB_FILES", "DONT_USE_CELLS", "TIEHI_CELL_AND_PORT",
    "TIELO_CELL_AND_PORT", "MIN_BUF_CELL_AND_PORTS", "ABC_DRIVER_CELL", "ABC_LOAD_IN_FF",
    "CORE_UTILIZATION", "CORE_ASPECT_RATIO", "CORE_MARGIN", "PLACE_DENSITY_LB_ADDON",
    "TNS_END_PERCENT", "FLOORPLAN_DEF", "PLACE_PINS_ARGS", "FASTROUTE_TCL", "PDN_TCL",
    "CTS_CLUSTER_SIZE", "CTS_CLUSTER_DIAMETER", "SKIP_INCREMENTAL_REPAIR", "MIN_ROUTING_LAYER",
    "MAX_ROUTING_LAYER", "ENABLE_PLACE_REPAIR_TIMING", "SKIP_CTS_REPAIR_TIMING",
    "REMOVE_ABC_BUFFERS",
]  # fmt: skip
#: The variables that a `config.mk` may set although xeda has no setting for them, with the reason
#: why the result does not depend on them. Any other variable that a `config.mk` sets and that is
#: not in `MAKE_VARS` stops the tool: someone must map it to a xeda setting or list it here.
IGNORED_VARIABLES = {
    "SYNTH_REPEATABLE_BUILD": "it removes paths and times from the debug information of the "
    "netlist, and the netlist stays the same",
}

#: The metrics of a result row, by the name that ORFS gives them in its JSON logs. The first group
#: is what the bar and the table need. The second group is kept when the run gives it.
REQUIRED_METRICS = (
    "finish__timing__setup__ws",
    "finish__timing__setup__tns",
    "finish__timing__hold__ws",
    "finish__timing__hold__tns",
    "finish__design__instance__area__stdcell",
    "finish__power__total",
    "detailedroute__route__drc_errors",
    "detailedroute__antenna__violating__nets",
)
OPTIONAL_METRICS = (
    "synth__design__instance__area",
    "synth__design__instance__count",
    "finish__design__instance__count__stdcell",
    "finish__design__core__area",
    "finish__timing__fmax",
    "finish__timing__drv__setup_violation_count",
    "globalroute__timing__setup__ws",
    "globalroute__antenna__violating__nets",
)
METRIC_KEYS = REQUIRED_METRICS + OPTIONAL_METRICS
#: The JSON logs of ORFS that hold the metrics, in the order that the tool merges them.
METRIC_LOGS = ("1_synth.json", "5_1_grt.json", "5_2_route.json", "6_report.json")

#: The bar: a result is good when its WNS is within 15 ps of the ORFS WNS or better, its final
#: standard cell area is within 2% of the ORFS area or smaller, and it has no DRC error, no
#: antenna violation and a positive hold slack.
WNS_TOLERANCE_NS = 0.015
AREA_TOLERANCE = 0.02
_EPSILON = 1e-9

#: The OpenROAD flows of xeda, in the order of the chain.
OPENROAD_FLOWS = ("openroad_place", "openroad_cts", "openroad_grt", "openroad")
#: The metrics of a row and the result of the last xeda flow that gives each: the ORFS metric, the
#: result, and the factor that turns the unit of xeda into the unit of ORFS. xeda reports `fmax`
#: in MHz and ORFS in Hz. Both give times in ns, areas in um^2 and power in W. The worst slack is
#: the result `worst_slack`: the result `wns` stops at zero, and the ORFS metric does not.
XEDA_RESULTS = (
    ("finish__timing__setup__ws", "worst_slack", 1),
    ("finish__timing__setup__tns", "tns", 1),
    ("finish__timing__hold__ws", "whs", 1),
    ("finish__timing__hold__tns", "hold_tns", 1),
    ("finish__design__instance__area__stdcell", "area", 1),
    ("finish__power__total", "power", 1),
    ("detailedroute__route__drc_errors", "drc_errors", 1),
    ("detailedroute__antenna__violating__nets", "antenna_violating_nets", 1),
    ("finish__timing__fmax", "fmax", 1_000_000),
    ("finish__timing__drv__setup_violation_count", "setup_violations", 1),
    ("finish__design__instance__count__stdcell", "instances", 1),
    ("finish__design__core__area", "core_area", 1),
)

#: The flows that a half needs.
HALF_FLOWS = {
    "synthesis": ("yosys",),
    "pnr": ("openroad",),
    "both": ("yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad"),
}
#: Settings that the tool sets in the generated design: the platform in every flow, the clocks in
#: none (the constraint file of ORFS gives them to the OpenROAD flows, and a second clock on the
#: same port would replace the first), the hierarchy in the OpenROAD flows, the DEF file of the
#: floorplan, and the three verdicts of the last flow (the tool judges the numbers itself).
RESERVED_SETTINGS = (
    "platform",
    "platform_file",
    "pdk_provider",
    "pdk_version",
    "pdk_root",
    "clock",
    "clocks",
    "hierarchical",
    "fail_timing",
    "fail_drc",
    "fail_antenna",
    "floorplan.def_file",
)


class ParityError(Exception):
    """A problem that the user can fix. The message says what to change."""


class MissingFlows(ParityError):
    """A half needs xeda flows that this xeda does not have."""


# ---- processes -------------------------------------------------------------------------------


def scrubbed_env(
    home: Path, tool_paths: Sequence[str | Path], system: str = sys.platform
) -> dict[str, str]:
    """Return the environment of a tool process: nothing of the caller's environment.

    `PATH` holds the directories of `tool_paths` in order, the directory of this Python and the
    system directories. `HOME` and `TMPDIR` are under `home`, so a tool never writes to the real
    home directory of the user. The macOS OpenROAD build has no offscreen plugin of Qt.
    """
    directories = [str(Path(path).parent) for path in tool_paths]
    directories += [str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    return {
        "PATH": os.pathsep.join(dict.fromkeys(directories)),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "QT_QPA_PLATFORM": "cocoa" if system == "darwin" else "offscreen",
    }


def prepare_home(home: Path) -> None:
    """Make the directories that `scrubbed_env(home, ...)` names."""
    (home / "tmp").mkdir(parents=True, exist_ok=True)


def xeda_env(home: Path, yosys: str, openroad: str) -> dict[str, str]:
    """Return the environment of a xeda process. The chosen yosys comes first on `PATH`."""
    return scrubbed_env(home, [yosys, openroad])


def make_argv(
    *,
    make: str,
    orfs: Path,
    config: Path,
    work: Path,
    openroad: str,
    yosys: str,
    python: str,
    cores: int,
    targets: Sequence[str],
) -> list[str]:
    """Return the command line of one `make` call of the ORFS flow."""
    return [
        make, "-f", str(orfs / "flow" / "Makefile"),
        f"DESIGN_CONFIG={config}", f"WORK_HOME={work}",
        f"OPENROAD_EXE={openroad}", f"YOSYS_EXE={yosys}",
        f"PYTHON_EXE={python}", f"NUM_CORES={cores}", *targets,
    ]  # fmt: skip


def xeda_argv(python: str, chain: str, design_file: Path, run_root: Path) -> list[str]:
    """Return the command line that runs `chain` on the generated design file of a half."""
    return [
        python, "-m", "xeda", "run", chain, "--design", str(design_file),
        "--run-root", str(run_root), "--json",
    ]  # fmt: skip


def parse_make_print(text: str) -> dict[str, str]:
    """Read the lines `NAME: value` that `make print-NAME` writes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name in MAKE_VARS:
            values[name] = value.strip()
    return values


def run_logged(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    stdout: Path,
    timeout: float,
    stderr: Optional[Path] = None,
) -> tuple[int, float]:
    """Run `argv` with its output in files. Return the exit code (124 on a timeout) and the
    seconds. The process runs in a group of its own, and the tool stops that group on a timeout
    or an interrupt."""
    start = time.perf_counter()
    with open(stdout, "w") as out, open(stderr, "w") if stderr else open(os.devnull, "w") as err:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdout=out,
            stderr=err if stderr else subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code = 124
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return code, round(time.perf_counter() - start, 2)


def tool_version(executable: str, flag: str, home: Path) -> Optional[str]:
    """Return the first line that `executable flag` prints, or `None` if it does not run."""
    try:
        done = subprocess.run(
            [executable, flag],
            env=scrubbed_env(home, [executable]),
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (done.stdout + done.stderr).splitlines():
        if line.strip():
            return line.strip()
    return None


def registered_flow_names() -> frozenset[str]:
    """Return the names and aliases of the flows that this xeda has."""
    from xeda.introspect import all_flow_classes

    return frozenset(name for cls in all_flow_classes() for name in [cls.name, *cls.aliases])


# ---- the design file -------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignConfig:
    """A design file of `tools/asic_parity/designs`."""

    spec: str  # <platform>/<design>, the ORFS name
    clock_port: str
    clock_period: str  # a time with a unit, such as `0.25 ns`
    flows: dict[str, dict[str, Any]]  # settings by flow name, as written (dotted keys allowed)
    path: Path
    hierarchical: bool = False  # OPENROAD_HIERARCHICAL, for every OpenROAD flow

    @property
    def platform(self) -> str:
        return self.spec.split("/")[0]

    @property
    def design(self) -> str:
        return self.spec.split("/")[1]


def design_config_path(spec: str) -> Path:
    return DESIGNS / f"{spec}.yaml"


def expand_dotted(table: Mapping[str, Any]) -> dict[str, Any]:
    """Return `table` with each dotted key (`abc.strategy: area`) written as nested tables."""
    result: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, Mapping):
            value = expand_dotted(value)
        *parents, last = str(key).split(".")
        node = result
        for parent in parents:
            node = node.setdefault(parent, {})
            if not isinstance(node, dict):
                raise ParityError(f"`{key}` is both a value and a table")
        if last in node and isinstance(node[last], dict) and isinstance(value, dict):
            node[last] = {**node[last], **value}
        elif last in node:
            raise ParityError(f"`{key}` is given twice")
        else:
            node[last] = value
    return result


def leaf_paths(table: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Return the dotted path of each leaf of a nested table. An empty table is a leaf."""
    found: list[str] = []
    for key, value in table.items():
        if isinstance(value, Mapping) and value:
            found += leaf_paths(value, f"{prefix}{key}.")
        else:
            found.append(f"{prefix}{key}")
    return found


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Return `base` with the tables of `over` merged in. A leaf of `over` wins."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_from_table(table: Mapping[str, Any], path: Path) -> DesignConfig:
    """Check a design file that is read already and return it."""
    unknown = sorted(set(table) - {"orfs", "clock", "hierarchical", "flows"})
    if unknown:
        raise ParityError(
            f"{path}: unknown key `{unknown[0]}`; the keys are orfs, clock, hierarchical, flows"
        )
    spec = table.get("orfs")
    if not isinstance(spec, str) or not re.fullmatch(r"[a-z0-9]+/[A-Za-z0-9_]+", spec):
        raise ParityError(f"{path}: `orfs` must name the ORFS design, such as `nangate45/gcd`")
    clock = table.get("clock")
    if not isinstance(clock, Mapping) or set(clock) != {"port", "period"}:
        raise ParityError(f"{path}: `clock` needs `port` and `period`")
    period = clock["period"]
    from xeda.units import QuantityError, Time

    try:
        if not isinstance(period, str):
            raise QuantityError(f"{period!r} is a number")
        Time(period)
    except QuantityError as error:
        raise ParityError(
            f"{path}: clock.period needs a time with a unit, such as `0.25 ns`: {error}"
        ) from None
    if not isinstance(clock["port"], str):
        raise ParityError(f"{path}: clock.port must be text")
    hierarchical = table.get("hierarchical", False)
    if not isinstance(hierarchical, bool):
        raise ParityError(f"{path}: `hierarchical` must be true or false")
    flows = table.get("flows")
    if not isinstance(flows, Mapping) or not flows:
        raise ParityError(f"{path}: `flows` must hold the settings of at least one flow")
    for name, settings in flows.items():
        if not isinstance(settings, Mapping):
            raise ParityError(f"{path}: flows.{name} must be a table of settings")
        paths = leaf_paths(expand_dotted(settings))
        for reserved in RESERVED_SETTINGS:
            if any(leaf == reserved or leaf.startswith(reserved + ".") for leaf in paths):
                raise ParityError(f"{path}: flows.{name}.{reserved} is set by the tool; delete it")
    return DesignConfig(
        spec=spec,
        clock_port=clock["port"],
        clock_period=period,
        flows={str(name): dict(settings) for name, settings in flows.items()},
        path=path,
        hierarchical=hierarchical,
    )


def load_design_config(path: Path) -> DesignConfig:
    from xeda.yaml_loader import load_yaml

    if not path.is_file():
        known = ", ".join(sorted(f"{p.parent.name}/{p.stem}" for p in DESIGNS.glob("*/*.yaml")))
        raise ParityError(f"no design file {path}; the designs are: {known}")
    table = load_yaml(path)
    if not isinstance(table, Mapping):
        raise ParityError(f"{path}: the file must hold a table")
    return config_from_table(table, path)


def check_flow_names(config: DesignConfig, available: Collection[str]) -> None:
    missing = sorted(set(config.flows) - set(available))
    if missing:
        raise ParityError(f"{config.path}: xeda has no flow `{missing[0]}`")


def sdc_clock_period(text: str) -> Optional[str]:
    """Return the first clock period of an SDC file, as written, like the ORFS rule for ABC."""
    match = re.search(r"^\s*set\s+clk_period\s+([0-9.]+)", text, re.M) or re.search(
        r"create_clock\b.*?-period\s+([0-9.]+)", text
    )
    return match.group(1) if match else None


def sdc_clock_port(text: str) -> Optional[str]:
    match = re.search(r"^\s*set\s+clk_port_name\s+(\S+)", text, re.M)
    return match.group(1) if match else None


@dataclass(frozen=True)
class Expectation:
    """The value of a xeda setting that an ORFS variable asks for."""

    flow: str
    setting: str  # dotted name
    expected: Any
    variable: str
    value: str


def wrapped_operators(variables: Mapping[str, str]) -> bool:
    """Whether ORFS wraps the operators (`SWAP_ARITH_OPERATORS` or `SYNTH_WRAPPED_OPERATORS`)."""
    return "1" in (
        variables.get("SWAP_ARITH_OPERATORS", "").strip(),
        variables.get("SYNTH_WRAPPED_OPERATORS", "").strip(),
    )


def yosys_expectations(variables: Mapping[str, str]) -> list[Expectation]:
    """Return what the ORFS variables of a design ask of the settings of the xeda `yosys` flow."""
    get = lambda name: variables.get(name, "").strip()  # noqa: E731
    wrap = wrapped_operators(variables)
    wrap_variable = "SWAP_ARITH_OPERATORS" if get("SWAP_ARITH_OPERATORS") == "1" else (
        "SYNTH_WRAPPED_OPERATORS"
    )  # fmt: skip
    found = [
        Expectation("yosys", "arithmetic.wrap_operators", wrap, wrap_variable, get(wrap_variable)),
        Expectation(
            "yosys", "abc.mapper", "abc_new" if wrap else "abc", wrap_variable, get(wrap_variable)
        ),
        Expectation(
            "yosys",
            "arithmetic.adder_map",
            "platform" if get("ADDER_MAP_FILE") else "none",
            "ADDER_MAP_FILE",
            get("ADDER_MAP_FILE"),
        ),
        Expectation(
            "yosys",
            "hierarchy.keep_min_cost",
            int(get("SYNTH_MINIMUM_KEEP_SIZE") or 0) if get("SYNTH_HIERARCHICAL") == "1" else None,
            "SYNTH_HIERARCHICAL",
            get("SYNTH_HIERARCHICAL"),
        ),
    ]
    if wrap:
        # ORFS maps with abc_new, which takes no ABC_AREA script. xeda writes a Tcl script for
        # the wrapping passes by itself.
        found.append(
            Expectation("yosys", "abc.strategy", "area", wrap_variable, get(wrap_variable))
        )
    else:
        found.append(
            Expectation(
                "yosys",
                "abc.strategy",
                "area" if get("ABC_AREA") == "1" else "speed",
                "ABC_AREA",
                get("ABC_AREA"),
            )
        )
    # `synth.tcl` adds `-extra-map lcu_kogge_stone.v` to the `synth` calls unless
    # SYNTH_OPERATIONS_ARGS replaces it. Wrapped operators bring their own architectures, and
    # xeda refuses an adder architecture then.
    if wrap:
        found.append(
            Expectation("yosys", "arithmetic.adder_architecture", "yosys", wrap_variable, "1")
        )
    elif not get("SYNTH_OPERATIONS_ARGS"):
        found.append(
            Expectation(
                "yosys", "arithmetic.adder_architecture", "kogge_stone", "SYNTH_OPERATIONS_ARGS", ""
            )
        )
    if get("SYNTH_HDL_FRONTEND") == "slang":
        found.append(Expectation("yosys", "systemverilog", "slang", "SYNTH_HDL_FRONTEND", "slang"))
    return found


#: `PLACE_PINS_ARGS` as the shipped designs write it, and the only form that the tool translates.
_PIN_DISTANCE = re.compile(r"-min_distance\s+(\d+)\s+-min_distance_in_tracks")


def openroad_expectations(variables: Mapping[str, str]) -> tuple[list[Expectation], list[str]]:
    """Return what the ORFS variables of a design ask of the settings of the xeda OpenROAD flows,
    and the problems with a variable that the tool cannot translate."""
    from xeda.units import Length

    get = lambda name: variables.get(name, "").strip()  # noqa: E731
    problems: list[str] = []
    found: list[Expectation] = []

    def number(name: str, kind: type = float) -> Any:
        try:
            return kind(get(name))
        except ValueError:
            problems.append(f"{name} is {get(name)!r}, which is not a {kind.__name__}")
            return None

    def add(flow: str, setting: str, expected: Any, variable: str) -> None:
        if expected is not None or get(variable) == "":
            found.append(Expectation(flow, setting, expected, variable, get(variable)))

    place = "openroad_place"
    if get("FLOORPLAN_DEF"):
        add(place, "floorplan.sizing", "def", "FLOORPLAN_DEF")
    elif get("CORE_UTILIZATION"):
        add(place, "floorplan.sizing", "utilization", "CORE_UTILIZATION")
        add(
            place,
            "floorplan.core_utilization_percent",
            number("CORE_UTILIZATION"),
            "CORE_UTILIZATION",
        )
        add(place, "floorplan.aspect_ratio", number("CORE_ASPECT_RATIO"), "CORE_ASPECT_RATIO")
        margin = number("CORE_MARGIN")
        add(
            place,
            "floorplan.core_margin",
            None if margin is None else Length(f"{margin} um"),
            "CORE_MARGIN",
        )
    else:
        problems.append(
            "neither FLOORPLAN_DEF nor CORE_UTILIZATION is set: the tool cannot translate "
            "another way to size the floorplan"
        )
    if get("PLACE_DENSITY_LB_ADDON"):
        add(place, "global_placement.density.mode", "auto", "PLACE_DENSITY_LB_ADDON")
        add(
            place,
            "global_placement.density.headroom",
            number("PLACE_DENSITY_LB_ADDON"),
            "PLACE_DENSITY_LB_ADDON",
        )
    else:
        add(place, "global_placement.density.mode", "fixed", "PLACE_DENSITY_LB_ADDON")
    tns = number("TNS_END_PERCENT")
    for flow, setting in (
        (place, "floorplan.netlist_cleanup.repair.tns_end_percent"),
        (place, "resize.repair_timing.tns_end_percent"),
        ("openroad_cts", "repair.tns_end_percent"),
        ("openroad_grt", "repair.timing.tns_end_percent"),
    ):
        add(flow, setting, tns, "TNS_END_PERCENT")
    pins = get("PLACE_PINS_ARGS")
    if not pins:
        add(place, "io.min_distance_tracks", None, "PLACE_PINS_ARGS")
    elif _PIN_DISTANCE.fullmatch(pins):
        add(
            place,
            "io.min_distance_tracks",
            int(_PIN_DISTANCE.fullmatch(pins).group(1)),  # type: ignore[union-attr]
            "PLACE_PINS_ARGS",
        )
    else:
        problems.append(
            f"PLACE_PINS_ARGS is {pins!r}: the tool cannot translate it, so write the xeda "
            "settings by hand"
        )
    add(
        place,
        "floorplan.netlist_cleanup.mode",
        "remove_buffers" if get("REMOVE_ABC_BUFFERS") == "1" else "repair_timing",
        "REMOVE_ABC_BUFFERS",
    )
    add(
        place,
        "resize.repair_timing.enabled",
        get("ENABLE_PLACE_REPAIR_TIMING") == "1",
        "ENABLE_PLACE_REPAIR_TIMING",
    )
    add(place, "arith_swap.enabled", get("SWAP_ARITH_OPERATORS") == "1", "SWAP_ARITH_OPERATORS")
    cts = "openroad_cts"
    size, diameter = get("CTS_CLUSTER_SIZE"), get("CTS_CLUSTER_DIAMETER")
    add(cts, "sink_clustering.mode", "explicit" if size or diameter else "auto", "CTS_CLUSTER_SIZE")
    add(
        cts,
        "sink_clustering.size",
        number("CTS_CLUSTER_SIZE", int) if size else None,
        "CTS_CLUSTER_SIZE",
    )
    add(
        cts,
        "sink_clustering.max_diameter",
        Length(f"{diameter} um") if diameter else None,
        "CTS_CLUSTER_DIAMETER",
    )
    add(cts, "repair.enabled", get("SKIP_CTS_REPAIR_TIMING") != "1", "SKIP_CTS_REPAIR_TIMING")
    add(
        "openroad_grt",
        "repair.strategy",
        "none" if get("SKIP_INCREMENTAL_REPAIR") == "1" else "incremental",
        "SKIP_INCREMENTAL_REPAIR",
    )
    return found, problems


def _dotted_get(model: Any, name: str) -> Any:
    for part in name.split("."):
        model = getattr(model, part)
    return model


def unknown_variables(config_mk: str) -> list[str]:
    """Return the variables that a `config.mk` exports and that the tool neither reads nor lists
    as without effect. A comment line is no assignment."""
    known = set(MAKE_VARS) | set(IGNORED_VARIABLES)
    names = re.findall(r"^[ \t]*export[ \t]+([A-Za-z0-9_]+)", config_mk, re.M)
    return sorted(dict.fromkeys(name for name in names if name not in known))


def _inside(path: str, directory: str) -> bool:
    return Path(path).resolve().is_relative_to(Path(directory).resolve())


def design_tcl_texts(variables: Mapping[str, str]) -> dict[str, str]:
    """Return the text of the Tcl files that the design names itself, by variable. A file of the
    platform is left out: the platform of xeda holds its values."""
    texts: dict[str, str] = {}
    for name in ("FASTROUTE_TCL", "PDN_TCL"):
        path = variables.get(name, "").strip()
        if path and not _inside(path, variables.get("PLATFORM_DIR", "")):
            try:
                texts[name] = Path(path).read_text(encoding="utf-8")
            except OSError as error:
                raise ParityError(f"cannot read {name}: {error}") from None
    return texts


def _tcl_lines(text: str) -> list[str]:
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line and not line.startswith("#")]


_ADJUSTMENT = re.compile(r"set_global_routing_layer_adjustment\s+(\S+)\s+([0-9.]+)")
_ROUTING_LAYERS = re.compile(
    r"set_routing_layers\s+-(?:clock|signal)\s+"
    r"\$::env\(MIN_(?:CLK_)?ROUTING_LAYER\)-\$::env\(MAX_ROUTING_LAYER\)"
)


def fastroute_problems(
    text: str, variables: Mapping[str, str], layer_adjustments: Any
) -> list[str]:
    """Return the ways in which the layer adjustments of the settings differ from the file
    `FASTROUTE_TCL` of the design. The file may set the routing layers of the platform, and it
    must set one adjustment."""
    layers = f"{variables.get('MIN_ROUTING_LAYER')}-{variables.get('MAX_ROUTING_LAYER')}"
    problems: list[str] = []
    adjustments: dict[str, float] = {}
    for line in _tcl_lines(text):
        found = _ADJUSTMENT.fullmatch(line)
        if found:
            span = found.group(1).replace(
                "$::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)", layers
            )
            adjustments[span] = float(found.group(2))
        elif not _ROUTING_LAYERS.fullmatch(line):
            problems.append(
                f"FASTROUTE_TCL has the line `{line}`, which the tool cannot translate: write "
                "the xeda settings by hand"
            )
    if not adjustments:
        problems.append("FASTROUTE_TCL sets no layer adjustment")
    elif dict(layer_adjustments or {}) != adjustments:
        problems.append(
            f"flows.openroad_place.routing.layer_adjustments is {dict(layer_adjustments or {})!r}, "
            f"but FASTROUTE_TCL sets {adjustments!r}"
        )
    return problems


_STRIPE_FLAG = re.compile(r"-(layer|width|pitch|offset)\s+\{([^}]*)\}")
_PDN_KNOWN = ("add_global_connection", "global_connect", "set_voltage_domain", "add_pdn_connect")


def _stripes(
    settings: Any,
) -> list[tuple[str, Optional[float], Optional[float], Optional[float]]]:
    def um(value: Any) -> Optional[float]:
        return None if value is None else float(value.to("um"))

    return [(s.layer, um(s.width), um(s.pitch), um(s.offset)) for s in settings or []]


def pdn_problems(text: str, place: Any) -> list[str]:
    """Return the ways in which the grid of the settings differs from the file `PDN_TCL` of the
    design: its rails and straps (the layer, width, pitch and offset of each) and its pins. The
    global connections, the voltage domain and the connections between layers are those of the
    platform."""
    problems: list[str] = []
    rails: list[tuple[str, float, Optional[float], Optional[float]]] = []
    straps: list[tuple[str, float, Optional[float], Optional[float]]] = []
    pins = False
    for line in _tcl_lines(text):
        command = line.split()[0]
        if command == "define_pdn_grid" and "-macro" not in line:
            pins = "-pins" in line
        elif command == "add_pdn_stripe":
            given = dict(_STRIPE_FLAG.findall(line))
            row = (
                given.get("layer", ""),
                float(given.get("width", "nan")),
                float(given["pitch"]) if "pitch" in given else None,
                float(given["offset"]) if "offset" in given else None,
            )
            (rails if "-followpins" in line else straps).append(row)
        elif command not in _PDN_KNOWN:
            problems.append(
                f"PDN_TCL has the command `{command}`, which the tool cannot translate: write "
                "the xeda settings by hand"
            )
    grid = place.pdn.grid
    for name, want in (("rails", rails), ("straps", straps)):
        got = _stripes(getattr(grid, name, None) if grid is not None else None)
        if len(got) != len(want) or any(
            a[0] != b[0]
            or not all(
                x == y or (x is not None and y is not None and math.isclose(x, y))
                for x, y in zip(a[1:], b[1:])
            )
            for a, b in zip(got, want)
        ):
            problems.append(
                f"flows.openroad_place.pdn.grid.{name} is {got!r}, but PDN_TCL has {want!r} "
                "(layer, width, pitch and offset in um)"
            )
    if place.pdn.block_pins != pins:
        problems.append(
            f"flows.openroad_place.pdn.block_pins is {place.pdn.block_pins!r}, but PDN_TCL "
            f"{'gives' if pins else 'gives no'} `-pins`"
        )
    return problems


def injected_settings(
    config: DesignConfig, variables: Mapping[str, str], flow: str
) -> dict[str, Any]:
    """Return the settings that the tool sets in `flow`, other than the platform: the hierarchy of
    the OpenROAD flows, the DEF file of the floorplan, and no verdict in the last flow."""
    injected: dict[str, Any] = {}
    if flow in OPENROAD_FLOWS:
        injected["hierarchical"] = config.hierarchical
    if flow == "openroad":
        injected.update(fail_timing=False, fail_drc=False, fail_antenna=False)
    floorplan_def = variables.get("FLOORPLAN_DEF", "").strip()
    if flow == "openroad_place" and floorplan_def:
        injected["floorplan"] = {"def_file": floorplan_def}
    return injected


def flow_classes() -> dict[str, Any]:
    from xeda.introspect import all_flow_classes

    return {cls.name: cls for cls in all_flow_classes()}


def design_config_problems(
    config: DesignConfig,
    variables: Mapping[str, str],
    sdc_text: str,
    tcl_texts: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Return the ways in which the design file disagrees with ORFS. Empty means it agrees.

    `tcl_texts` holds the text of the Tcl files that the design names (`design_tcl_texts`). The
    tool checks the settings that a flow ends up with, not only the ones that the file gives, so a
    flow that the file leaves out must agree with ORFS by its defaults."""
    from xeda.units import Time

    problems: list[str] = []
    period = sdc_clock_period(sdc_text)
    if period is None:
        problems.append("the constraint file has no clock period (`set clk_period`)")
    elif abs(Time(config.clock_period).to("ns") - float(period)) > _EPSILON:
        problems.append(
            f"clock.period is {config.clock_period}, but the constraint file says "
            f"`set clk_period {period}` (in ns)"
        )
    port = sdc_clock_port(sdc_text)
    if port is not None and port != config.clock_port:
        problems.append(
            f"clock.port is {config.clock_port}, but the constraint file says "
            f"`set clk_port_name {port}`"
        )
    hierarchy = variables.get("OPENROAD_HIERARCHICAL", "").strip()
    if config.hierarchical != (hierarchy == "1"):
        problems.append(
            f"hierarchical is {config.hierarchical!r}, but OPENROAD_HIERARCHICAL = {hierarchy!r} "
            f"needs {hierarchy == '1'!r}"
        )
    classes = flow_classes()
    settings: dict[str, Any] = {}
    for flow in ("yosys", *OPENROAD_FLOWS):
        if flow not in classes or (flow == "yosys" and flow not in config.flows):
            continue
        table = deep_merge(
            injected_settings(config, variables, flow), expand_dotted(config.flows.get(flow, {}))
        )
        try:
            settings[flow] = classes[flow].Settings.from_input(
                table, design_root=config.path.parent
            )
        except Exception as error:
            problems.append(f"flows.{flow} does not validate: {error}")
    if "yosys" in settings and variables.get("SYNTH_OPERATIONS_ARGS", "").strip():
        if not wrapped_operators(variables):
            problems.append(
                f"SYNTH_OPERATIONS_ARGS is {variables['SYNTH_OPERATIONS_ARGS']!r}: the tool "
                "cannot translate it, so write the xeda settings by hand"
            )
    expectations = yosys_expectations(variables)
    openroad, untranslatable = openroad_expectations(variables)
    problems += untranslatable
    for item in [*expectations, *openroad]:
        if item.flow not in settings:
            continue
        got = _dotted_get(settings[item.flow], item.setting)
        if got != item.expected:
            problems.append(
                f"flows.{item.flow}.{item.setting} is {got!r}, but {item.variable} = "
                f"{item.value!r} needs {item.expected!r}"
            )
    texts = tcl_texts or {}
    place = settings.get("openroad_place")
    if place is not None and "FASTROUTE_TCL" in texts:
        problems += fastroute_problems(
            texts["FASTROUTE_TCL"], variables, place.routing.layer_adjustments
        )
    if place is not None and "PDN_TCL" in texts:
        problems += pdn_problems(texts["PDN_TCL"], place)
    return problems


_HEADER_TYPES = {
    ".svh": "SVHeader",
    ".sv": "SVHeader",
    ".vh": "VerilogHeader",
    ".v": "VerilogHeader",
}


def words(variables: Mapping[str, str], name: str) -> list[str]:
    return variables.get(name, "").split()


def design_sources(variables: Mapping[str, str]) -> list[Any]:
    """Return the `rtl.sources` of the design: the ORFS source files in their order, then each
    file of an include directory as a header (xeda finds the `-I` directories from the headers).
    With the slang front end, every ORFS source file is SystemVerilog, since ORFS reads all of
    them with one `read_slang`."""
    files = words(variables, "VERILOG_FILES")
    listed = {str(Path(f).resolve()) for f in files}
    sources: list[Any] = list(files)
    if variables.get("SYNTH_HDL_FRONTEND") == "slang":
        # ORFS gives every file to one `read_slang`, the Verilog files too
        sources = [{"file": f, "type": "SystemVerilog"} for f in files]
    for directory in words(variables, "VERILOG_INCLUDE_DIRS"):
        for path in sorted(Path(directory).iterdir()):
            kind = _HEADER_TYPES.get(path.suffix)
            if path.is_file() and kind and str(path.resolve()) not in listed:
                sources.append({"file": str(path), "type": kind})
    return sources


def pinned_files(variables: Mapping[str, str]) -> list[str]:
    """Return the files whose digests pin the sources of a row: the ORFS source files, then every
    regular file below each include directory, sorted, then the floorplan and the Tcl files that
    the design names. A header can change, appear or go away without a change of the ORFS commit,
    so each of them is part of the identity."""
    files = words(variables, "VERILOG_FILES")
    for directory in words(variables, "VERILOG_INCLUDE_DIRS"):
        files += [str(path) for path in sorted(Path(directory).rglob("*")) if path.is_file()]
    for name in ("FLOORPLAN_DEF", "FASTROUTE_TCL", "PDN_TCL"):
        path = variables.get(name, "").strip()
        if path and Path(path).is_file():
            files.append(path)
    return list(dict.fromkeys(files))


def platform_settings(
    config: DesignConfig, *, orfs: Path, orfs_commit: Optional[str], override_file: Path
) -> dict[str, Any]:
    """Return the settings that give every flow the platform of ORFS."""
    settings: dict[str, Any] = {
        "platform": config.platform,
        "platform_file": str(override_file),
        "pdk_provider": "directory",
        "pdk_root": str(orfs / "flow" / "platforms"),
    }
    if orfs_commit:
        settings["pdk_version"] = orfs_commit
    return settings


def generated_design(
    config: DesignConfig,
    variables: Mapping[str, str],
    *,
    orfs: Path,
    orfs_commit: Optional[str],
    override_file: Path,
    sources: list[Any],
) -> dict[str, Any]:
    """Return the xeda design file that runs `config` on the ORFS inputs.

    It configures every flow of the design file and every flow of the whole chain, each with the
    platform of ORFS and what `injected_settings` gives. It gives no flow a clock: the constraint
    file of ORFS, a source of the design, gives the clocks to the OpenROAD flows.
    """
    platform = platform_settings(
        config, orfs=orfs, orfs_commit=orfs_commit, override_file=override_file
    )
    names = dict.fromkeys([*config.flows, *HALF_FLOWS["both"]])
    flows = {
        name: deep_merge(
            {**platform, **injected_settings(config, variables, name)},
            expand_dotted(config.flows.get(name, {})),
        )
        for name in names
    }
    return {
        "name": f"{config.platform}_{config.design}",
        "rtl": {
            "top": variables["DESIGN_NAME"],
            "clock": {"port": config.clock_port},
            "sources": sources,
        },
        "flows": flows,
    }


# ---- the xeda halves -------------------------------------------------------------------------


@dataclass(frozen=True)
class XedaPlan:
    """What xeda runs for a half: the flow chain and the sources of the generated design."""

    chain: str
    sources: list[Any]


def xeda_plan(
    half: str,
    available: Collection[str],
    *,
    design_sources: list[Any],
    orfs_netlist: Optional[Path],
    sdc: Path,
) -> XedaPlan:
    """Return the xeda flow chain of `half` and the sources it reads.

    This is the one place that knows how the halves run xeda. `synthesis` runs `yosys` on the
    design sources. `pnr` runs `openroad` on the netlist of ORFS and the constraint file. `both`
    runs the whole chain from the design sources. Raise `MissingFlows` when `available` lacks a
    flow of the half.
    """
    needed = HALF_FLOWS[half]
    missing = [name for name in needed if name not in available]
    if missing:
        noun = "flow" if len(needed) == 1 else "flows"
        raise MissingFlows(
            f"the half {half} needs the xeda {noun} {', '.join(needed)}, and this xeda has no "
            f"{', '.join(missing)}. The half synthesis needs only yosys"
        )
    constraint = {"file": str(sdc), "type": "Sdc"}
    if half == "synthesis":
        return XedaPlan("yosys", list(design_sources))
    if half == "pnr":
        if orfs_netlist is None:
            raise ParityError("the half pnr needs the netlist of the ORFS reference row")
        return XedaPlan(
            "openroad", [{"file": str(orfs_netlist), "type": "VerilogNetlist"}, constraint]
        )
    return XedaPlan("+".join(needed), [*design_sources, constraint])


def read_xeda_results(document: Mapping[str, Any], half: str) -> dict[str, Any]:
    """Return the part of a result row that the `--json` output of a xeda run gives.

    The `synthesis` half reads the area and the cell count of the yosys flow. The other halves
    read the results of the last flow and give them the ORFS metric names (`XEDA_RESULTS`).
    """
    if not document.get("success", False):
        error = document.get("error") or {}
        raise ParityError(
            f"the xeda run failed: {error.get('type', 'unknown error')}: "
            f"{error.get('message', 'no message')}"
        )
    results = document.get("results", {})
    part: dict[str, Any] = {
        "runtime_s": results.get("runtime"),
        "tools": {t["executable"]: t.get("version") for t in results.get("tools", [])},
        "flow_hash": results.get("flow_hash"),
        "settings_hash": results.get("settings_hash"),
        "design_hash": results.get("design_hash"),
        "run_path": results.get("run_path"),
    }
    if half == "synthesis":
        part["yosys_area"] = results.get("area")
        part["yosys_cells"] = results.get("cells")
        return part
    missing = [
        f"`{key}` ({metric})"
        for metric, key, _factor in XEDA_RESULTS
        if metric in REQUIRED_METRICS and key not in results
    ]
    if missing:
        raise ParityError(f"the xeda results lack {', '.join(missing)}")
    part["metrics"] = {
        metric: results[key] * factor if factor != 1 else results[key]
        for metric, key, factor in XEDA_RESULTS
        if key in results
    }
    return part


def node_results(document: Mapping[str, Any], flow: str) -> dict[str, Any]:
    """Return the area, the cell count and the run time from the results of the flow `flow` in a
    chain, read from the run directory of its node. Return `{}` when there is no such node or no
    results file."""
    for node in document.get("nodes") or []:
        if node.get("flow") == flow and node.get("run_path"):
            try:
                loaded = json.loads((Path(node["run_path"]) / "results.json").read_text())
            except (OSError, ValueError):
                return {}
            return {key: loaded[key] for key in ("area", "cells", "runtime") if key in loaded}
    return {}


def xeda_times(half: str, seconds: float, synthesis_seconds: Optional[float]) -> dict[str, float]:
    """Return the times of a half that runs the OpenROAD flows: all of `seconds` is place and route
    time, except the run time of the `yosys` flow in the chain of `both`."""
    if half == "both" and synthesis_seconds is not None:
        return {
            "synthesis_s": round(synthesis_seconds, 1),
            "pnr_s": round(seconds - synthesis_seconds, 1),
        }
    return {"pnr_s": round(seconds, 1)}


# ---- ORFS ------------------------------------------------------------------------------------


def read_orfs_metrics(logs: Path) -> dict[str, Any]:
    """Return the metrics of the JSON logs of ORFS in `logs`."""
    merged: dict[str, Any] = {}
    for name in METRIC_LOGS:
        path = logs / name
        if path.is_file():
            merged.update(json.loads(path.read_text()))
    return {key: merged[key] for key in METRIC_KEYS if key in merged}


def read_yosys_area(stat: Path) -> Optional[float]:
    """Return the chip area of the last module in the yosys `stat` report of ORFS."""
    if not stat.is_file():
        return None
    found = re.findall(r"Chip area for (?:top )?module '\\?[^']*': ([0-9.]+)", stat.read_text())
    return float(found[-1]) if found else None


def canonical_script(variables: Mapping[str, str], out: Path) -> str:
    """Return the yosys script that writes the canonical RTLIL of a design with the slang front
    end. It does what `synth_canonicalize.tcl` of ORFS does for that front end, and it needs no
    Tcl in yosys."""
    libs = words(variables, "LIB_FILES")
    # `read_slang` is a plugin in some yosys builds; `plugin -i` works with a built-in one too
    lines = ["plugin -i slang"]
    lines += [f"read_liberty -overwrite -setattr liberty_cell -lib {lib}" for lib in libs]
    lines += [
        "read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func "
        f"-ignore_buses {lib}" for lib in libs
    ]  # fmt: skip
    includes = " ".join(f"-I{d}" for d in words(variables, "VERILOG_INCLUDE_DIRS"))
    files = " ".join(words(variables, "VERILOG_FILES"))
    lines.append(
        "read_slang -D SYNTHESIS --keep-hierarchy --compat=vcs --ignore-assertions "
        f"--top {variables['DESIGN_NAME']} {includes} {files} "
        f"{variables.get('CLKGATE_MAP_FILE', '')}".rstrip()
    )
    lines += [
        "setattr -unset init",
        f"hierarchy -check -top {variables['DESIGN_NAME']}",
        "opt_clean -purge",
        "setattr -unset src *",
        "setattr -mod -unset src *",
        f"write_rtlil {out}",
    ]
    return "\n".join(lines) + "\n"


def git_commit(checkout: Path) -> Optional[str]:
    """Return the commit that the checkout has, from its `.git` files and without running git."""
    git = checkout / ".git"
    if git.is_file():  # a worktree: ".git" names the real directory
        text = git.read_text().strip()
        if not text.startswith("gitdir:"):
            return None
        git = (checkout / text.split(":", 1)[1].strip()).resolve()
    try:
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(":", 1)[1].strip()
    common = git
    if (git / "commondir").is_file():
        common = (git / (git / "commondir").read_text().strip()).resolve()
    for base in (git, common):
        try:
            return (base / ref).read_text().strip() or None
        except OSError:
            pass
    try:
        for line in (common / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_pins(orfs: Path, files: Sequence[str | Path], sdc: Path) -> dict[str, Any]:
    """Return the sha256 digest of each source file and of the constraint file. A file inside the
    ORFS checkout is named by its path in the checkout."""

    def name(path: str | Path) -> str:
        path = Path(path)
        try:
            return path.resolve().relative_to(orfs.resolve()).as_posix()
        except ValueError:
            return str(path)

    return {
        "files": {name(f): sha256_file(Path(f)) for f in files},
        "sdc": {name(sdc): sha256_file(sdc)},
    }


def residue_digest(platform: str) -> Optional[str]:
    """Return the sha256 digest of the file `platform.yaml` that xeda ships for a platform."""
    from importlib import resources

    try:
        return hashlib.sha256(
            (resources.files("xeda.platforms") / platform / "platform.yaml").read_bytes()
        ).hexdigest()
    except OSError:
        return None


#: The fields of a row that identify its inputs. A saved row is reused only when every one equals
#: the current value.
IDENTITY_KEYS = (
    "orfs_commit",
    "sources",
    "platform_residue_sha256",
    "override_sha256",
    "design_file_sha256",
    "tools",
    "make_vars",
    "reference_sha256",
    "xeda_sha256",
    "tool_sha256",
)

#: The tools that only the halves run: the ORFS row does not record them.
XEDA_ONLY_TOOLS = ("xeda", "yosys_xeda", "yosys_xeda_path")


def package_sha256(directory: Path) -> str:
    """Return one sha256 digest of every file of a package directory, names and contents, with
    the byte code left out. It pins the code of xeda, whose version string does not change with
    an edit of an editable install."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def xeda_sha256() -> str:
    """Return the digest of the xeda package that the halves run."""
    import xeda

    return package_sha256(Path(xeda.__file__).parent)


def row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the part of a row that says which inputs it came from."""
    identity = {key: row.get(key) for key in IDENTITY_KEYS}
    identity["cores"] = (row.get("machine") or {}).get("cores")
    return identity


def _leaves(value: Any, prefix: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        found: dict[str, Any] = {}
        for key, item in value.items():
            found.update(_leaves(item, f"{prefix}.{key}"))
        return found
    return {prefix: value}


def stale_reasons(saved: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Return what differs between the inputs of a saved row and the current ones. An empty list
    means that the saved row is current."""
    old = {}
    new = {}
    for key, value in row_identity(saved).items():
        old.update(_leaves(value, key))
    for key, value in row_identity(current).items():
        new.update(_leaves(value, key))
    return [
        f"{name} differs" if name in old and name in new else f"{name} is not in both"
        for name in sorted(set(old) | set(new))
        if old.get(name) != new.get(name)
    ]


def result_path(root: Path, spec: str, half: str) -> Path:
    return root / spec / half / "result.json"


# ---- the bar and the table -------------------------------------------------------------------


def _metric(row: Mapping[str, Any], key: str) -> Optional[float]:
    return (row.get("metrics") or {}).get(key)


def bar_problems(row: Mapping[str, Any], reference: Optional[Mapping[str, Any]]) -> list[str]:
    """Return how `row` misses the bar. An empty list means that `row` meets it.

    The bar: WNS within 15 ps of the reference or better, final standard cell area within 2% of
    the reference or smaller, no DRC error, no antenna violation, a positive hold slack.
    """
    if reference is None:
        return ["no ORFS reference row"]
    if not reference.get("success"):
        return [f"the ORFS reference row failed at {reference.get('failed_at')}"]
    if not row.get("success"):
        return [f"failed at {row.get('failed_at') or 'an unknown stage'}"]
    problems: list[str] = []

    def need(key: str) -> Optional[float]:
        value = _metric(row, key)
        if value is None:
            problems.append(f"{key} is missing")
        return value

    wns = need("finish__timing__setup__ws")
    ref_wns = _metric(reference, "finish__timing__setup__ws")
    if ref_wns is None:
        problems.append("the ORFS row has no WNS (finish__timing__setup__ws)")
    elif wns is not None and wns < ref_wns - WNS_TOLERANCE_NS - _EPSILON:
        problems.append(
            f"WNS {wns:.3f} ns is {(ref_wns - wns) * 1000:.0f} ps below the ORFS WNS "
            f"{ref_wns:.3f} ns (the limit is {WNS_TOLERANCE_NS * 1000:.0f} ps)"
        )
    area = need("finish__design__instance__area__stdcell")
    ref_area = _metric(reference, "finish__design__instance__area__stdcell")
    if not ref_area:
        problems.append("the ORFS row has no area (finish__design__instance__area__stdcell)")
    elif area is not None and area > ref_area * (1 + AREA_TOLERANCE) + _EPSILON:
        problems.append(
            f"area {area:.1f} um^2 is {(area / ref_area - 1) * 100:+.1f}% against the ORFS "
            f"area {ref_area:.1f} um^2 (the limit is +{AREA_TOLERANCE * 100:.0f}%)"
        )
    drc = need("detailedroute__route__drc_errors")
    if drc:
        problems.append(f"{drc:g} DRC errors")
    antenna = need("detailedroute__antenna__violating__nets")
    if antenna:
        problems.append(f"{antenna:g} antenna violations")
    hold = need("finish__timing__hold__ws")
    if hold is not None and not hold > 0:
        problems.append(f"hold WNS {hold:.3f} ns is not positive")
    return problems


def _number(value: Any, digits: int, scale: float = 1.0) -> str:
    return "-" if value is None else f"{value * scale:.{digits}f}"


def _integer(value: Any) -> str:
    return "-" if value is None else f"{value:g}"


COLUMNS = (
    "row", "WNS (ns)", "TNS (ns)", "hold WNS (ns)", "hold TNS (ns)", "area (um^2)",
    "power (mW)", "DRC", "antenna", "synth area", "cells", "yosys area", "synth s", "P&R s", "bar",
)  # fmt: skip


def _row_name(row: Mapping[str, Any]) -> str:
    return "orfs" if row.get("half") == "orfs" else f"xeda {row.get('half')}"


def _table_line(row: Mapping[str, Any], reference: Optional[Mapping[str, Any]]) -> str:
    problems = bar_problems(row, reference)
    name = _row_name(row)
    if not row.get("success"):
        cells = [name, problems[0], *[""] * (len(COLUMNS) - 3), "; ".join(problems)]
        return "| " + " | ".join(cells) + " |"
    m = lambda key: _metric(row, key)  # noqa: E731
    times = row.get("times") or {}
    cells = [
        name,
        _number(m("finish__timing__setup__ws"), 3),
        _number(m("finish__timing__setup__tns"), 2),
        _number(m("finish__timing__hold__ws"), 3),
        _number(m("finish__timing__hold__tns"), 2),
        _number(m("finish__design__instance__area__stdcell"), 1),
        _number(m("finish__power__total"), 3, 1000),
        _integer(m("detailedroute__route__drc_errors")),
        _integer(m("detailedroute__antenna__violating__nets")),
        _number(m("synth__design__instance__area"), 1),
        _integer(m("synth__design__instance__count")),
        _number(row.get("yosys_area"), 1),
        _number(times.get("synthesis_s"), 1),
        _number(times.get("pnr_s"), 1),
        "; ".join(problems) if problems else "ok",
    ]
    return "| " + " | ".join(cells) + " |"


def load_rows(root: Path, spec: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for half in ROW_ORDER:
        path = result_path(root, spec, half)
        if path.is_file():
            rows[half] = json.loads(path.read_text())
    return rows


def recorded_designs(root: Path) -> list[str]:
    """Return the designs that have a result file under `root`."""
    if not root.is_dir():
        return []
    return sorted(
        f"{platform.name}/{design.name}"
        for platform in root.iterdir()
        if platform.is_dir() and not platform.name.startswith(".")
        for design in platform.iterdir()
        if design.is_dir() and any(design.glob("*/result.json"))
    )


def render_tables(root: Path, specs: Sequence[str]) -> str:
    """Return the Markdown tables of the recorded rows, one per design."""
    out: list[str] = []
    for spec in specs or recorded_designs(root):
        out.append(f"### {spec}\n")
        rows = load_rows(root, spec)
        if not rows:
            out.append(f"no results under {root / spec}\n")
            continue
        reference = rows.get("orfs")
        out.append("| " + " | ".join(COLUMNS) + " |")
        out.append("| --- | " + " | ".join(["---:"] * (len(COLUMNS) - 2)) + " | --- |")
        out.extend(_table_line(row, reference) for row in rows.values())
        out.append("")
    return "\n".join(out)


# ---- running ---------------------------------------------------------------------------------


@dataclass
class Context:
    """The tools and the settings of one invocation."""

    orfs: Path
    root: Path
    openroad: str
    yosys: str  # a yosys with Tcl, for ORFS
    xeda_yosys: str
    yosys_frontend: str
    cores: int
    timeout: float
    make: str = "make"
    python: str = sys.executable
    versions: dict[str, Optional[str]] = field(default_factory=dict)


def _which(option: Optional[str], variable: str, program: str) -> str:
    found = option or os.environ.get(variable) or shutil.which(program)
    if not found:
        raise ParityError(f"cannot find {program}: use --{program} or set ${variable}")
    return found


def build_context(args: argparse.Namespace) -> Context:
    orfs = args.orfs or os.environ.get("ORFS_DIR")
    if not orfs:
        raise ParityError("give the ORFS checkout: --orfs <directory> or set $ORFS_DIR")
    xeda_yosys = args.xeda_yosys or shutil.which("yosys")
    if not xeda_yosys:
        raise ParityError("cannot find yosys for xeda: use --xeda-yosys or put yosys on PATH")
    return Context(
        orfs=Path(orfs).resolve(),
        root=Path(args.root).resolve(),
        openroad=_which(args.openroad, "OPENROAD_EXE", "openroad"),
        yosys=_which(args.yosys, "YOSYS_EXE", "yosys"),
        xeda_yosys=xeda_yosys,
        yosys_frontend=args.yosys_frontend or xeda_yosys,
        cores=args.cores,
        timeout=args.timeout,
        make=_which(None, "MAKE", "make"),
    )


@dataclass
class OrfsDesign:
    """A design as ORFS resolves it."""

    config: DesignConfig
    config_mk: Path
    variables: dict[str, str]

    @property
    def platform(self) -> str:
        return self.variables["PLATFORM"]

    @property
    def nickname(self) -> str:
        return self.variables["DESIGN_NICKNAME"]

    @property
    def sdc(self) -> Path:
        return Path(self.variables["SDC_FILE"])

    @property
    def slang(self) -> bool:
        return self.variables.get("SYNTH_HDL_FRONTEND") == "slang"

    def results(self, work: Path) -> Path:
        return work / "results" / self.platform / self.nickname / "base"

    def logs(self, work: Path) -> Path:
        return work / "logs" / self.platform / self.nickname / "base"

    def reports(self, work: Path) -> Path:
        return work / "reports" / self.platform / self.nickname / "base"


def resolve_design(ctx: Context, config: DesignConfig) -> OrfsDesign:
    """Read the ORFS variables of the design with `make print-<VARIABLE>`."""
    config_mk = ctx.orfs / "flow" / "designs" / config.spec / "config.mk"
    if not config_mk.is_file():
        raise ParityError(f"ORFS has no design {config.spec}: no file {config_mk}")
    scratch = ctx.root / ".resolve" / config.spec
    scratch.mkdir(parents=True, exist_ok=True)
    prepare_home(scratch / "home")
    argv = make_argv(
        make=ctx.make, orfs=ctx.orfs, config=config_mk, work=scratch / "work",
        openroad=ctx.openroad, yosys=ctx.yosys, python=ctx.python, cores=ctx.cores,
        targets=[f"print-{name}" for name in MAKE_VARS],
    )  # fmt: skip
    argv.insert(1, "-s")
    done = subprocess.run(
        argv,
        cwd=scratch,
        env=scrubbed_env(scratch / "home", [ctx.openroad, ctx.yosys, ctx.make]),
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise ParityError(f"make print failed for {config.spec}:\n{done.stderr}")
    variables = parse_make_print(done.stdout)
    missing = sorted(set(MAKE_VARS) - set(variables))
    if missing:
        raise ParityError(f"make did not print {', '.join(missing)}")
    return OrfsDesign(config, config_mk, variables)


def check_design(design: OrfsDesign) -> None:
    check_flow_names(design.config, registered_flow_names() | set(HALF_FLOWS["both"]))
    problems = design_config_problems(
        design.config,
        design.variables,
        design.sdc.read_text(encoding="utf-8"),
        design_tcl_texts(design.variables),
    )
    problems += [
        f"{design.config_mk} sets {name}, and the tool does not know it: map it to a xeda setting "
        "in the design file and in the tool, or list it in IGNORED_VARIABLES"
        for name in unknown_variables(design.config_mk.read_text(encoding="utf-8"))
    ]
    if problems:
        raise ParityError(f"{design.config.path} does not match ORFS:\n  " + "\n  ".join(problems))


def half_dir(ctx: Context, spec: str, half: str) -> Path:
    return result_path(ctx.root, spec, half).parent


def fresh_directory(ctx: Context, directory: Path) -> None:
    """Make an empty `directory` under the root, deleting what an earlier run left there."""
    if ctx.root.resolve() not in directory.resolve().parents:
        raise ParityError(f"refusing to delete {directory}: it is not under {ctx.root}")
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)


def tool_versions(ctx: Context) -> dict[str, Optional[str]]:
    if not ctx.versions:
        home = ctx.root / ".resolve" / "home"
        prepare_home(home)
        ctx.versions.update(
            openroad=tool_version(ctx.openroad, "-version", home),
            yosys_orfs=tool_version(ctx.yosys, "-V", home),
            yosys_xeda=tool_version(ctx.xeda_yosys, "-V", home),
        )
        if ctx.yosys_frontend != ctx.xeda_yosys:
            ctx.versions["yosys_frontend"] = tool_version(ctx.yosys_frontend, "-V", home)
        # an OpenROAD build without a version stamp reports `bazel-nostamp`: the digest pins it
        openroad = shutil.which(ctx.openroad) or ctx.openroad
        ctx.versions["openroad_path"] = openroad
        ctx.versions["openroad_sha256"] = sha256_file(Path(openroad))
        ctx.versions["yosys_orfs_path"] = shutil.which(ctx.yosys) or ctx.yosys
        ctx.versions["yosys_xeda_path"] = shutil.which(ctx.xeda_yosys) or ctx.xeda_yosys
        from xeda.version import __version__

        ctx.versions["xeda"] = __version__
    return ctx.versions


def new_row(ctx: Context, design: OrfsDesign, half: str) -> dict[str, Any]:
    """Return a row with the facts that pin the run: tools, sources, platform, machine."""
    config = design.config
    commit = git_commit(ctx.orfs)
    override = OVERRIDES / f"{config.platform}.yaml"
    files = pinned_files(design.variables)
    # ORFS reads none of the xeda inputs: a change of xeda, of its yosys or of the files that only
    # xeda reads does not rerun ORFS. This tool drives both runs, so every row records its digest.
    xeda_side = half != "orfs"
    tools = dict(tool_versions(ctx))
    if not xeda_side:
        for key in XEDA_ONLY_TOOLS:
            tools.pop(key, None)
    return {
        "schema": 1,
        "design": config.spec,
        "half": half,
        "success": False,
        "failed_at": None,
        "error": None,
        "metrics": {},
        "times": {},
        "yosys_area": None,
        "yosys_cells": None,
        "tools": tools,
        "orfs_commit": commit,
        "pdk": {"provider": "directory", "pin": commit},
        "platform_residue_sha256": residue_digest(config.platform) if xeda_side else None,
        "override_sha256": sha256_file(override) if xeda_side and override.is_file() else None,
        "design_file_sha256": sha256_file(config.path) if xeda_side else None,
        "xeda_sha256": xeda_sha256() if xeda_side else None,
        "tool_sha256": sha256_file(Path(__file__)),
        "sources": {
            "orfs_commit": commit,
            **source_pins(ctx.orfs, files, design.sdc),
        },
        "machine": {
            "platform": host_platform.platform(),
            "cores": ctx.cores,
            "cpu_count": os.cpu_count(),
        },
        "make_vars": design.variables,
    }


def write_row(ctx: Context, row: dict[str, Any]) -> dict[str, Any]:
    path = result_path(ctx.root, row["design"], row["half"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=1, sort_keys=True) + "\n")
    return row


def seed_results(results: Path, netlist: Optional[Path], sdc: Path) -> None:
    """Put the netlist and the constraint file where ORFS expects the output of its synthesis."""
    results.mkdir(parents=True, exist_ok=True)
    if netlist is not None:
        shutil.copyfile(netlist, results / "1_2_yosys.v")
    shutil.copyfile(sdc, results / "1_2_yosys.sdc")


def run_pnr(ctx: Context, design: OrfsDesign, d: Path, row: dict[str, Any]) -> bool:
    """Run the place and route targets of ORFS in `d`, one `make` call each. Add the stage times
    and the metrics to `row`. Return whether every stage passed."""
    work = d / "work"
    env = scrubbed_env(d / "home", [ctx.openroad, ctx.yosys, ctx.make])
    stages: dict[str, float] = {}
    ok = True
    for target in PNR_TARGETS:
        argv = make_argv(
            make=ctx.make, orfs=ctx.orfs, config=design.config_mk, work=work,
            openroad=ctx.openroad, yosys=ctx.yosys, python=ctx.python, cores=ctx.cores,
            targets=[target],
        )  # fmt: skip
        code, seconds = run_logged(argv, d, env, d / f"{target}.log", ctx.timeout)
        stages[target] = seconds
        if code:
            row["failed_at"] = target
            row["error"] = f"make {target} exited with {code}; see {d / (target + '.log')}"
            ok = False
            break
    row["times"]["pnr_stages"] = stages
    row["times"]["pnr_s"] = round(sum(stages.values()), 1)
    row["metrics"].update(read_orfs_metrics(design.logs(work)))
    return ok


def canonicalize(ctx: Context, design: OrfsDesign, d: Path) -> Path:
    """Write the canonical RTLIL of a slang design with the front-end yosys."""
    out = d / "canonical.rtlil"
    script = d / "canonical.ys"
    script.write_text(canonical_script(design.variables, out))
    home = d / "frontend_home"
    prepare_home(home)
    code, _ = run_logged(
        [ctx.yosys_frontend, "-l", str(d / "canonical.log"), "-s", str(script)],
        d,
        scrubbed_env(home, [ctx.yosys_frontend]),
        d / "canonical.out",
        ctx.timeout,
    )
    if code:
        raise ParityError(
            f"the canonical RTLIL failed (exit {code}); see {d / 'canonical.log'}. A slang design "
            "needs --yosys-frontend, a yosys with read_slang"
        )
    return out


def run_reference(ctx: Context, design: OrfsDesign) -> dict[str, Any]:
    """Run ORFS's own flow and record the reference row."""
    spec = design.config.spec
    d = half_dir(ctx, spec, "orfs")
    fresh_directory(ctx, d)
    row = new_row(ctx, design, "orfs")
    work = d / "work"
    results = design.results(work)
    env = scrubbed_env(d / "home", [ctx.openroad, ctx.yosys, ctx.make])
    prepare_home(d / "home")
    steps = ["do-yosys-canonicalize", "do-yosys"]
    results.mkdir(parents=True, exist_ok=True)
    if design.slang:
        shutil.copyfile(canonicalize(ctx, design, d), results / "1_1_yosys_canonicalize.rtlil")
        steps = ["do-yosys"]
    synthesis = 0.0
    for step in steps:
        argv = make_argv(
            make=ctx.make, orfs=ctx.orfs, config=design.config_mk, work=work,
            openroad=ctx.openroad, yosys=ctx.yosys, python=ctx.python, cores=ctx.cores,
            targets=[step],
        )  # fmt: skip
        code, seconds = run_logged(argv, d, env, d / f"{step}.log", ctx.timeout)
        synthesis += seconds
        if code:
            row.update(failed_at=step, error=f"make {step} exited with {code}; see {d}/{step}.log")
            row["times"] = {"synthesis_s": round(synthesis, 1)}
            return write_row(ctx, row)
    row["times"] = {"synthesis_s": round(synthesis, 1)}
    row["yosys_area"] = read_yosys_area(design.reports(work) / "synth_stat.txt")
    seed_results(results, None, design.sdc)
    row["success"] = run_pnr(ctx, design, d, row)
    return write_row(ctx, row)


def run_xeda(
    ctx: Context, design: OrfsDesign, d: Path, plan: XedaPlan
) -> tuple[dict[str, Any], float]:
    """Run the xeda chain of `plan` as a subprocess in `d`. Return its `--json` document and the
    seconds."""
    config = design.config
    generated = generated_design(
        config,
        design.variables,
        orfs=ctx.orfs,
        orfs_commit=git_commit(ctx.orfs),
        override_file=OVERRIDES / f"{config.platform}.yaml",
        sources=plan.sources,
    )
    import yaml

    design_file = d / "design.yaml"
    design_file.write_text(yaml.safe_dump(generated, sort_keys=False))
    home = d / "xeda_home"
    prepare_home(home)
    argv = xeda_argv(ctx.python, plan.chain, design_file, d / "xeda_run")
    code, seconds = run_logged(
        argv,
        d,
        xeda_env(home, ctx.xeda_yosys, ctx.openroad),
        d / "xeda.json",
        ctx.timeout,
        stderr=d / "xeda.log",
    )
    try:
        document = json.loads((d / "xeda.json").read_text())
    except ValueError:
        raise ParityError(
            f"xeda exited with {code} and wrote no JSON document; see {d / 'xeda.log'}"
        ) from None
    return document, seconds


def run_half(
    ctx: Context, design: OrfsDesign, half: str, reference: dict[str, Any]
) -> dict[str, Any]:
    """Run one half and record its row."""
    spec = design.config.spec
    d = half_dir(ctx, spec, half)
    reference_netlist = design.results(half_dir(ctx, spec, "orfs") / "work") / "1_2_yosys.v"
    plan = xeda_plan(
        half,
        registered_flow_names(),
        design_sources=design_sources(design.variables),
        orfs_netlist=reference_netlist,
        sdc=design.sdc,
    )
    fresh_directory(ctx, d)
    row = new_row(ctx, design, half)
    if half == "pnr":
        row["reference_sha256"] = sha256_file(result_path(ctx.root, spec, "orfs"))
    try:
        document, seconds = run_xeda(ctx, design, d, plan)
        part = read_xeda_results(document, half)
    except ParityError as error:
        row.update(failed_at="xeda", error=str(error))
        return write_row(ctx, row)
    row["xeda"] = {k: v for k, v in part.items() if k not in ("metrics",)}
    row["yosys_area"] = part.get("yosys_area")
    row["yosys_cells"] = part.get("yosys_cells")
    row["times"] = {"synthesis_s": seconds}
    if half == "synthesis":
        results = design.results(d / "work")
        run_path = Path(part["run_path"])
        netlist = run_path / "netlist.v"
        if not netlist.is_file():
            row.update(failed_at="xeda", error=f"xeda wrote no netlist {netlist}")
            return write_row(ctx, row)
        seed_results(results, netlist, design.sdc)
        prepare_home(d / "home")
        row["success"] = run_pnr(ctx, design, d, row)
    else:
        synthesis = node_results(document, "yosys")
        row["metrics"] = part["metrics"]
        row["yosys_area"] = synthesis.get("area")
        row["yosys_cells"] = synthesis.get("cells")
        row["times"] = xeda_times(half, seconds, synthesis.get("runtime"))
        row["success"] = True
    return write_row(ctx, row)


def recorded_row(
    ctx: Context, design: OrfsDesign, half: str, force: bool
) -> Optional[dict[str, Any]]:
    """Return the saved row of `half` when it came from the current inputs, else `None`.

    The row is current when it passed and its tools, sources, platform files, design file, ORFS
    variables and core count equal the current ones. A half row also depends on the code of xeda,
    and the `pnr` row on the reference row that gave its netlist. A failed or stale row is
    reported, and the caller reruns it.
    """
    spec = design.config.spec
    path = result_path(ctx.root, spec, half)
    if force or not path.is_file():
        return None
    saved = json.loads(path.read_text())
    if not saved.get("success"):
        print(f"{spec} {half}: the saved row failed; running it again", flush=True)
        return None
    current = new_row(ctx, design, half)
    if half == "pnr":
        reference = result_path(ctx.root, spec, "orfs")
        current["reference_sha256"] = sha256_file(reference) if reference.is_file() else None
    reasons = stale_reasons(saved, current)
    if reasons:
        shown = "; ".join(reasons[:5]) + ("; ..." if len(reasons) > 5 else "")
        print(f"{spec} {half}: the saved row is stale ({shown}); running it again", flush=True)
        return None
    return saved


def ensure_reference(ctx: Context, design: OrfsDesign, force: bool) -> dict[str, Any]:
    return recorded_row(ctx, design, "orfs", force) or run_reference(ctx, design)


def command_run(args: argparse.Namespace) -> int:
    halves = [h for h in ROW_ORDER[1:] if h in args.half]
    ctx = build_context(args)
    config = load_design_config(design_config_path(args.design))
    available = registered_flow_names()
    for half in halves:  # refuse before the long ORFS run when a flow is missing
        xeda_plan(half, available, design_sources=[], orfs_netlist=Path("."), sdc=Path("."))
    design = resolve_design(ctx, config)
    check_design(design)
    reference = ensure_reference(ctx, design, args.force)
    failed = not reference.get("success")
    rows: dict[str, dict[str, Any]] = {}
    for half in halves:
        saved = recorded_row(ctx, design, half, args.force)
        if saved is not None:
            print(f"{config.spec} {half}: recorded already (--force reruns)", flush=True)
            rows[half] = saved
            continue
        row = run_half(ctx, design, half, reference)
        failed |= not row["success"]
        rows[half] = row
        print(f"{config.spec} {half}: {'ok' if row['success'] else row['error']}", flush=True)
    print(render_tables(ctx.root, [config.spec]))
    # A row that ran and misses the bar fails the command too. A failed row is already counted,
    # and with a failed reference every row would miss it.
    misses = (
        {}
        if not reference.get("success")
        else {
            half: problems
            for half, row in rows.items()
            if row.get("success") and (problems := bar_problems(row, reference))
        }
    )
    for half, problems in misses.items():
        print(f"{config.spec} {half}: misses the bar: {'; '.join(problems)}", flush=True)
    return 1 if failed or misses else 0


def command_table(args: argparse.Namespace) -> int:
    print(render_tables(Path(args.root).resolve(), args.designs))
    return 0


def command_script(args: argparse.Namespace) -> int:
    """Run the xeda `yosys` flow and print the script that it rendered.

    The flow renders its script while it runs (it needs the platform files and the settings),
    and a dry run stops before that, so this command runs the flow and reads the script from the
    run directory.
    """
    ctx = build_context(args)
    config = load_design_config(design_config_path(args.design))
    if "yosys" not in config.flows:
        raise ParityError(f"{config.path} has no settings for the yosys flow")
    design = resolve_design(ctx, config)
    check_design(design)
    d = half_dir(ctx, config.spec, "script")
    fresh_directory(ctx, d)
    plan = xeda_plan(
        "synthesis",
        registered_flow_names(),
        design_sources=design_sources(design.variables),
        orfs_netlist=None,
        sdc=design.sdc,
    )
    document, _ = run_xeda(ctx, design, d, plan)
    run_path = (document.get("results") or {}).get("run_path") or document.get("run_path")
    scripts = sorted(Path(run_path).glob("yosys_synth.*")) if run_path else []
    if not scripts:
        raise ParityError(f"xeda rendered no script; see {d / 'xeda.log'}")
    print(scripts[0].read_text(), end="")
    return 0 if document.get("success") else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--orfs", help="ORFS checkout (read-only; default: $ORFS_DIR)")
    p.add_argument(
        "--root", default="asic_parity_runs", help="output directory (default: %(default)s)"
    )
    p.add_argument("--openroad", help="OpenROAD executable (default: $OPENROAD_EXE or PATH)")
    p.add_argument("--yosys", help="yosys with Tcl, for ORFS (default: $YOSYS_EXE or PATH)")
    p.add_argument("--xeda-yosys", help="yosys for the xeda flow (default: yosys on PATH)")
    p.add_argument(
        "--yosys-frontend",
        help="yosys with read_slang, for the canonical RTLIL of a slang design (default: the "
        "yosys of the xeda flow)",
    )
    p.add_argument("--cores", type=int, default=4, help="NUM_CORES of ORFS (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=3300, help="seconds per make or xeda call")
    commands = p.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run ORFS (when needed) and the halves, then compare")
    run.add_argument("design", help="<platform>/<design>, such as nangate45/gcd")
    run.add_argument("--half", action="append", choices=HALVES, required=True,
                     help="which part of the flow runs in xeda; give it more than once for "
                     "several halves")  # fmt: skip
    run.add_argument("--force", action="store_true", help="rerun rows that are recorded already")
    run.set_defaults(handler=command_run)

    table = commands.add_parser("table", help="print the Markdown tables of the recorded rows")
    table.add_argument("designs", nargs="*", help="designs to print (default: all recorded)")
    table.set_defaults(handler=command_table)

    script = commands.add_parser(
        "script", help="run the xeda yosys flow and print the yosys script it rendered"
    )
    script.add_argument("design")
    script.set_defaults(handler=command_script)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except MissingFlows as error:
        print(f"asic_parity: {error}", file=sys.stderr)
        return 3
    except ParityError as error:
        print(f"asic_parity: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
