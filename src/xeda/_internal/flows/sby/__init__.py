"""Formal property checks with SymbiYosys (SBY)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from ...dataclass import Field
from ...design import Design, Source, SourceType, language_of, storage_name
from ...flow import (
    ArtifactSpec,
    Flow,
    FlowException,
    FlowSettingsException,
    MetricSpec,
    describe_artifacts,
    describe_results,
)
from ...flow.sim import record_failure
from ...tool import Tool
from ...units import Time, count
from ...utils import ExecutableNotFound, NonZeroExitCode, replacing_copy, replacing_file
from ..ghdl import GhdlSynth
from ..yosys.common import process_parameters

log = logging.getLogger(__name__)

__all__ = ["Sby"]

#: The work directory of SBY in the run directory. SBY refuses a work directory that holds
#: files, so the flow removes it before each run, and SBY never deletes anything (no `-f`).
WORK = "sby"
#: The directory where the flow copies the sources under their paths relative to the design root.
STAGE = "sby_stage"
#: The temporary and state directory of SBY and every program it starts.
TEMPORARY = "sby_tmp"
CONFIG = "task.sby"
#: The second task of `prove`, which checks the assumptions, and its work directory.
ASSUMPTIONS_CONFIG = "assumptions.sby"
ASSUMPTIONS_WORK = "sby_assumptions"
#: The directory of the task that holds the files of the storage of the design's generator. They
#: have no place in the design tree.
GENERATED_DIRECTORY = "xeda_generated"
#: The directory of the task that holds one directory for each dependency that is outside the
#: design root, numbered by the order of its first source.
DEPENDENCIES_DIRECTORY = "xeda_dependencies"
#: The flattened model that the task writes beside the staged sources, for `timing_problems`. A
#: source of this name, in any letter case, is refused. Keep it lowercase, as `PARAMETERS_FILE`.
TIMING_MODEL = "xeda_timing.json"
#: The attributes of each property cell before `prep`, for `unreached_property_files`. Kept
#: lowercase and refused as a source name, as `TIMING_MODEL`.
PROPERTIES_FILE = "xeda_properties.txt"
#: A property statement in Verilog or SystemVerilog text without comments and strings.
PROPERTY_STATEMENT = re.compile(r"\b(?:assert|assume|cover)\b")
#: The `src` attribute of a cell in the output of Yosys `printattrs`.
SOURCE_ATTRIBUTE = re.compile(r'\bsrc="([^"]*)"')
ARCHIVE = "sby_evidence.zip"
#: The SBY engine line of each mode. `smtbmc` runs its presat check of the assumptions. `prove` uses
#: one process of rIC3, IC3 only: the default portfolio of rIC3 writes `/tmp/rIC3` outside the run
#: root. `btor` has no presat check, so a second task checks the assumptions of a `prove` that
#: passed. SBY gives `btor rIC3` in `prove` no depth.
ENGINES = {"bmc": "smtbmc bitwuzla", "cover": "smtbmc bitwuzla", "prove": "btor rIC3 -e ic3"}
#: The depth of `bmc` and `cover` when `depth` is not set, and of the check of the assumptions of
#: `prove`. `multiclock` takes twice this: a regular clock needs two steps for each cycle.
DEFAULT_DEPTH = 20
#: The programs that SBY starts in each mode, by the option of SBY that names each one. SBY finds a
#: program without an option (None) on its PATH.
PROGRAMS: dict[str, dict[str, str | None]] = {
    "bmc": {"yosys": "--yosys", "yosys-smtbmc": "--smtbmc", "bitwuzla": None},
    "cover": {"yosys": "--yosys", "yosys-smtbmc": "--smtbmc", "bitwuzla": None},
    # A counterexample of `btor` runs `yosys-witness` and `btorsim`. The check of the assumptions
    # runs `yosys-smtbmc` and `bitwuzla`.
    "prove": {
        "yosys": "--yosys",
        "yosys-smtbmc": "--smtbmc",
        "bitwuzla": None,
        "rIC3": "--rIC3",
        "yosys-witness": "--witness",
        "btorsim": None,
    },
}
#: A directory that SBY can name in its `sh` commands, which hold the paths of programs unquoted.
SHELL_WORD = re.compile(r"[A-Za-z0-9_./+-]+", re.ASCII)
#: A second root of the Verilog compilation. It assigns each parameter to the top by name: slang
#: refuses a named parameter that the top does not have, but ignores such a name in `-G`.
PARAMETERS_ROOT = "xeda_sby_parameters"
#: The file of that root in the task. A staged source of this name, in any letter case, would
#: replace it. Keep it lowercase: `staged_names` compares the lowercase staged name.
PARAMETERS_FILE = f"{PARAMETERS_ROOT}.sv"

#: The exit code of SBY for each status of a task, with the default `expect pass`.
EXIT_CODES = {"PASS": 0, "FAIL": 2, "UNKNOWN": 4, "TIMEOUT": 8, "ERROR": 16, "CANCELLED": 32}
#: The `status` file of a task: the status, its exit code and the time in seconds.
STATUS_LINE = re.compile(r"(?P<status>[A-Z]+) (?P<code>\d+) (?P<time>\d+)\n?")
#: A staged name: SBY splits a line of `[files]` at whitespace and expands `$` and `~` in it,
#: yosys ends a word at whitespace, and a leading `-` or `+` reads as an option.
STAGED_NAME = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./+-]*", re.ASCII)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
#: What ends a word in the generated task, or starts a comment.
NOT_IN_A_WORD = re.compile(r"[\s#;]")
#: The defines that the flow sets or keeps unset itself.
FORMAL_DEFINES = ("FORMAL", "SYNTHESIS")
#: Text that reads as a number, a boolean or a based literal such as `8'hff`. The command line
#: gives every value as text, and the task would pass it as a string (`-G OK="0"`), not as the
#: value.
AMBIGUOUS_TEXT = re.compile(
    r"[-+]?(?:\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|0[xX][0-9a-fA-F_]+"
    r"|\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|(?i:true|false)"
)
#: A sized binary literal: the one text that `process_parameters` passes as a number.
SIZED_BINARY = re.compile(r"\d+'b[01]+")

UNIT_TYPES = (SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl)
HEADER_TYPES = (SourceType.VerilogHeader, SourceType.SVHeader)
#: The legacy property cells of yosys, by the flavor of the `$check` cell that replaces them.
PROPERTY_CELLS = {
    "$assert": "assert",
    "$assume": "assume",
    "$cover": "cover",
    "$live": "live",
    "$fair": "fair",
}
#: The flavors of a property that this flow checks.
CHECKED = ("assert", "assume", "cover")
#: Symbolic values: no fixture has shown that each frontend lowers them as intended.
SYMBOLIC_CELLS = ("$anyconst", "$anyseq", "$allconst", "$allseq")
#: What each mode checks: a run passes only with at least one such statement.
OBLIGATION = {"bmc": "assert", "prove": "assert", "cover": "cover"}
#: The state cells with one clock edge and no other way to change.
EDGE_CELLS = ("$dff", "$dffe", "$sdff", "$sdffe", "$sdffce")
#: The state cells that also change without a clock edge, or have no clock edge. The default model
#: of SBY samples them at the one clock, so a change between two edges would be lost.
LEVEL_CELLS = (
    "$adff",
    "$adffe",
    "$aldff",
    "$aldffe",
    "$dffsr",
    "$dffsre",
    "$sr",
    "$dlatch",
    "$adlatch",
    "$dlatchsr",
)
#: The ports of `LEVEL_CELLS` that change the state with no clock edge.
ASYNCHRONOUS_PORTS = ("ARST", "ALOAD", "AD", "SET", "CLR")
#: The bits of a Yosys model that are no signal: two equal constants share nothing.
CONSTANT_BITS = {"0", "1", "x", "z"}
#: The state cells of the implicit global clock. It counts as one more clock next to a real one.
GLOBAL_CELLS = ("$ff", "$anyinit")
GLOBAL_CLOCK = "the global clock"
#: Removes what `DEFAULT_DISABLE` must not see. A string goes first, because it can hold `//`.
COMMENT_OR_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
DEFAULT_DISABLE = re.compile(r"\bdefault\s+disable\s+iff\b")


def _staged_name(
    design: Design, src: Source, roots: dict[str, Path], outside: dict[str, str]
) -> str:
    """Return the name of `src` in the SBY task, from the root that holds it and its path there.

    A generated file has the place where its generator writes it, so the location of the run root
    does not change the name, and a relative `include` to its neighbors works as in the tree. A
    file in a storage has no such place, and goes into `GENERATED_DIRECTORY` of its root. `roots`
    gives the root of each generator of a dependency (`Source._owner`), and `outside` numbers the
    roots outside the design tree. A Git checkout is one of them wherever the run root is.
    """
    design_root = os.path.realpath(design.root)
    checkouts = {
        os.path.realpath(record.checkout)
        for record in design.resolved_dependencies
        if record.checkout is not None
    }
    if src.generated:
        name = str(src.output_name)
        if src._owner is not None and src._owner not in roots:
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: the design has no generator {src._owner!r}"
            )
        home = design.root if src._owner is None else roots[src._owner]
        stored = storage_name(name)
        if stored is not None:
            name = (PurePosixPath(GENERATED_DIRECTORY) / stored).as_posix()
    elif src._dependency_place is not None:
        name = src._dependency_place
        home = src.path.parents[len(PurePosixPath(name).parts) - 1]
    else:
        # The directory is resolved and the file name is kept, so a link names its own place.
        parent = os.path.relpath(os.path.realpath(src.path.parent), design_root)
        name = (PurePosixPath(Path(parent).as_posix()) / src.path.name).as_posix()
        home = design.root
    root = os.path.realpath(home)
    below = Path(os.path.relpath(root, design_root)).as_posix()
    if root == design_root:
        if name.split("/", 1)[0].lower() == DEPENDENCIES_DIRECTORY or (
            not src.generated and name.split("/", 1)[0].lower() == GENERATED_DIRECTORY
        ):
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: the task keeps {GENERATED_DIRECTORY} and "
                f"{DEPENDENCIES_DIRECTORY} for itself, in any letter case; rename the directory"
            )
    elif root in checkouts or below.split("/", 1)[0] == "..":
        name = f"{DEPENDENCIES_DIRECTORY}/{outside.setdefault(root, str(len(outside)))}/{name}"
    else:
        name = f"{below}/{name}"
    if name.split("/", 1)[0] == "..":
        raise FlowSettingsException(
            f"sby stages each source under its path relative to the design root, and "
            f"{src.path} lies outside the design root {design_root}: move it into the design root"
        )
    if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise FlowSettingsException(f"sby cannot stage {name!r}: it leaves the stage directory")
    return PurePosixPath(name).as_posix()  # one spelling, so that the checks compare the file


def staged_names(design: Design, sources: list[Source]) -> dict[str, Source]:
    """Return each source under the name it has in the SBY task, in design order.

    The name of a file of the design is its path relative to the design root, so a relative
    `include` and a memory file that the HDL names relative to the design root are found as in the
    design (`_staged_name`). A declaration of a generated source has no file yet: its pattern is
    kept, and `run` checks the names of the files. A written source outside the design root and
    outside every dependency, a name that the task cannot hold, two files under one name, a file
    named as a directory of another, and two names that differ only in case (one file on a
    case-insensitive file system) are refused. So is a name equal to the parameters file of the task
    (`PARAMETERS_FILE`) in any letter case, whatever the type of the source, when the task writes
    that file: it would replace the file.
    """
    # Only Verilog and SystemVerilog with parameters have the file; GHDL checks generics itself.
    writes_parameters_file = bool(design.rtl.parameters) and not any(
        src.type is SourceType.Vhdl for src in sources
    )
    roots = {unit._generation_label: unit.root for unit in design.generation_units}
    outside: dict[str, str] = {}
    names: dict[str, Source] = {}
    folded: dict[str, str] = {}
    for src in sources:
        name = _staged_name(design, src, roots, outside)
        if not STAGED_NAME.fullmatch(name if src._located else name.replace("*", "_")):
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: SBY splits its list of files at whitespace and "
                "expands `$` and `~`, so a path relative to the design root may hold only "
                "letters, digits and `_ . + - /`, and starts with neither `-` nor `+`; rename it"
            )
        if writes_parameters_file and name.lower() == PARAMETERS_FILE:
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: the task writes {PARAMETERS_FILE} to check the "
                "parameter names, and a source with that name, in any letter case, would replace "
                "it; rename the source"
            )
        if name.lower() in (TIMING_MODEL, PROPERTIES_FILE):
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: the task writes a file of that name itself; rename "
                "the source"
            )
        if name in names:
            if _same_file(names[name], src):
                continue  # the same file listed twice
            raise FlowSettingsException(
                f"sby cannot stage both {names[name].path} and {src.path} as {name!r}: rename one"
            )
        other = folded.setdefault(name.lower(), name)
        if other != name:
            raise FlowSettingsException(
                f"sby cannot stage both {other!r} and {name!r}: their names differ only in case"
            )
        names[name] = src
    for name in names:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() in names:
                raise FlowSettingsException(
                    f"sby cannot stage {name!r}: {parent.as_posix()!r} is a file of the design"
                )
    return names


def _same_file(first: Source, second: Source) -> bool:
    """Whether two sources with one name are one file. A declaration has no file to resolve."""
    return first.path == second.path or (
        first._located
        and second._located
        and os.path.realpath(first.path) == os.path.realpath(second.path)
    )


def obligations(model: dict[str, Any]) -> Counter[str]:
    """Count the properties and symbolic values in the hierarchy of the top of an SBY model.

    The model is the JNY file that SBY writes (`model/design.json`). A property counts once for
    each instance of its module, by its flavor: `assert`, `assume`, `cover`, `live` or `fair`, or
    the flavor of a `$check` cell. A symbolic value counts by its cell type. Raise `ValueError`,
    `KeyError` or `TypeError` for a model of another shape.
    """
    modules = {module["name"]: module for module in model["modules"]}
    tops = [m for m in modules.values() if int(m.get("attributes", {}).get("top", "0")) == 1]
    if len(tops) != 1:
        raise ValueError(f"the model has {len(tops)} top modules")

    def count_in(name: str, depth: int) -> Counter[str]:
        if depth > len(modules):
            raise ValueError("the hierarchy of the model has a cycle")
        found: Counter[str] = Counter()
        for sort in modules[name]["cell_sorts"]:
            kind, cells = sort["type"], sort["cells"]
            if kind == "$check":
                found.update(str(cell["parameters"]["FLAVOR"]) for cell in cells)
            elif kind in PROPERTY_CELLS:
                found[PROPERTY_CELLS[kind]] += len(cells)
            elif kind in SYMBOLIC_CELLS:
                found[kind] += len(cells)
            elif kind in modules:
                inner = count_in(kind, depth + 1)
                for _ in cells:
                    found.update(inner)
        return found

    return count_in(tops[0]["name"], 0)


def _top_module(model: dict[str, Any]) -> dict[str, Any]:
    """Return the top of a flattened Yosys model (`TIMING_MODEL`), or raise `ValueError`."""
    tops = [
        module
        for module in model["modules"].values()
        if int(module.get("attributes", {}).get("top", "0"), 2) == 1
    ]
    if len(tops) != 1:
        raise ValueError(f"the model has {len(tops)} top modules")
    return tops[0]


def unreached_property_files(names: dict[str, Source], attributes: str) -> list[str]:
    """Return the staged Verilog and SystemVerilog sources that hold a property statement while no
    property cell of the model comes from them.

    `attributes` is the output of `printattrs` over the property cells before `prep`, so a property
    that Yosys would later remove as constant still counts. Such a file holds a checker that no
    `bind` or instance reaches, or a property in an inactive `ifdef` branch. A property that a macro
    writes has no place in its file, so this finds nothing for it."""
    reached = {
        PurePosixPath(place.rsplit(":", 1)[0]).as_posix()
        for value in SOURCE_ATTRIBUTE.findall(attributes)
        for place in value.split("|")
        if place
    }
    return [
        name
        for name, src in names.items()
        if src.type in (*HEADER_TYPES, SourceType.Verilog, SourceType.SystemVerilog)
        and name not in reached
        and PROPERTY_STATEMENT.search(
            COMMENT_OR_STRING.sub(" ", src.path.read_text(errors="replace"))
        )
    ]


def is_combinational(model: dict[str, Any]) -> bool:
    """Return whether a flattened model has no state and no clocked property. GHDL writes a clocked
    assertion as a flop before an `$assert`, so the state counts as well as the trigger."""
    for cell in _top_module(model)["cells"].values():
        kind = cell["type"]
        parameters = cell["parameters"]
        if kind in EDGE_CELLS or kind in LEVEL_CELLS or kind in GLOBAL_CELLS:
            return False
        # A memory with no write port and no clocked read port is a table.
        if kind == "$mem_v2" and (
            int(parameters["WR_PORTS"], 2) or "1" in parameters["RD_CLK_ENABLE"]
        ):
            return False
        if kind == "$check" and "1" in parameters["TRG_ENABLE"]:
            return False
    return True


def _fan_in(bits: set[Any], drivers: dict[Any, set[Any]]) -> set[Any]:
    """Return `bits` and every bit that reaches them through combinational cells."""
    found = set(bits)
    pending = list(bits)
    while pending:
        for bit in drivers.get(pending.pop(), ()):
            if bit not in found:
                found.add(bit)
                pending.append(bit)
    return found


def _clock_races(
    clocked: list[tuple[Any, set[Any]]],
    drivers: dict[Any, set[Any]],
    owners: dict[Any, set[Any]],
    bit_names: dict[Any, str],
) -> set[str]:
    """Return why the time model of `multiclock on` differs from the design.

    `clk2fflogic` gives a cell the value of its data one step before its clock edge. That differs
    from the design for a cell that reads its own clock, because the edge has changed that value.
    It also differs for a cell on a clock that state drives, when it reads state of the clock that
    drives that state: that state changes on the same edge, before the clock of the cell does.
    `clocked` lists each clock bit of a cell with the data bits of the cell, `owners` what changes each state bit
    (its clock and its level-sensitive inputs), and `drivers` the input bits of each output bit of a
    combinational cell.
    """
    problems: set[str] = set()
    clock_side: dict[Any, tuple[set[Any], set[Any]]] = {}
    for clock, data in clocked:
        if clock not in clock_side:
            sources = _fan_in({clock}, drivers)
            # What changes a state bit in the fan-in of this clock, and its sources in turn.
            upstream: set[Any] = set()
            pending = list(sources)
            while pending:
                for changes in owners.get(pending.pop(), ()):
                    for bit in _fan_in({changes}, drivers) - upstream:
                        upstream.add(bit)
                        pending.append(bit)
            clock_side[clock] = (sources, upstream)
        sources, upstream = clock_side[clock]
        read = _fan_in(data, drivers)
        for bit in (sources & read) - CONSTANT_BITS:
            problems.add(
                f"logic on the clock it drives reads {bit_names.get(bit, str(bit))} as data"
            )
        for bit in read:
            # State on this clock changes with this cell, so a shared reset is no race.
            changed_by = owners.get(bit, set())
            if changed_by & upstream and clock not in changed_by:
                problems.add(
                    f"logic on the state-driven clock {bit_names.get(clock, str(clock))} reads "
                    f"{bit_names.get(bit, str(bit))}, which changes on the same edge"
                )
    return problems


def timing_problems(model: dict[str, Any], multiclock: bool = False) -> list[str]:
    """Return why the time model of SBY cannot hold a flattened design, or an empty list.

    The model is the output of Yosys `write_json` (`TIMING_MODEL`). With `multiclock off`, SBY
    replaces every clock with one implicit step and does not check that this is valid. It is valid
    for state, assertions and memory ports on the rising edge of one bit of one top-level input,
    and for logic that reads no such clock. A clock bit that is read as data has a value in the
    model that it never has in the design. `multiclock` models the rest and needs only what
    `_clock_races` says. Raise `ValueError`, `KeyError`, `IndexError` or `TypeError` for a model of
    another shape.
    """
    module = _top_module(model)
    bit_names: dict[Any, str] = {}
    for wire, net in module["netnames"].items():
        for index, bit in enumerate(net["bits"]):
            bit_names.setdefault(bit, wire if len(net["bits"]) == 1 else f"{wire}[{index}]")
    inputs = {
        bit
        for port in module["ports"].values()
        if port["direction"] == "input"
        for bit in port["bits"]
    }
    events: set[tuple[Any, str]] = set()
    read: set[Any] = set()
    problems: set[str] = set()
    unknown: set[str] = set()
    clocked: list[tuple[Any, set[Any]]] = []
    drivers: dict[Any, set[Any]] = {}
    owners: dict[Any, set[Any]] = {}
    for cell in module["cells"].values():
        kind, parameters, ports = cell["type"], cell["parameters"], cell["connections"]
        clock_ports: tuple[str, ...] = ()  # no data of the cell, in use or not
        own: set[Any] = set()  # the clock bits in use
        if kind in LEVEL_CELLS:
            problems.add(f"the cell {kind} can change between clock edges")
            clock_ports = ("CLK",) if "CLK" in ports else ()
            own = set(ports.get("CLK", ()))
        elif kind in EDGE_CELLS:
            clock_ports = ("CLK",)
            own = {ports["CLK"][0]}
            events.add((ports["CLK"][0], parameters["CLK_POLARITY"][-1]))
        elif kind in ("$check", "$print"):
            clock_ports = ("TRG",)
            if "1" in parameters["TRG_ENABLE"]:
                own = set(ports["TRG"])
                polarities = parameters["TRG_POLARITY"][::-1]
                events.update((bit, polarities[index]) for index, bit in enumerate(ports["TRG"]))
        elif kind in GLOBAL_CELLS:
            events.add((GLOBAL_CLOCK, "1"))
        elif kind == "$mem_v2":
            clock_ports = ("RD_CLK", "WR_CLK")
            for prefix in ("RD", "WR"):
                polarities = parameters[f"{prefix}_CLK_POLARITY"][::-1]
                for index, enabled in enumerate(parameters[f"{prefix}_CLK_ENABLE"][::-1]):
                    if enabled == "1":
                        own.add(ports[f"{prefix}_CLK"][index])
                        events.add((ports[f"{prefix}_CLK"][index], polarities[index]))
            if any(bit != "0" for bit in ports["RD_ARST"]):
                problems.add("a memory read port has an asynchronous reset")
        elif (
            not kind.startswith("$")
            or (kind.startswith("$_") and any(state in kind for state in ("FF", "LATCH", "SR")))
            or any("CLK" in port or port == "TRG" for port in ports)
        ):
            unknown.add(f"the cell {kind} has timing that the flow does not know")
        directions = cell["port_directions"]
        data = {
            bit
            for port, bits in ports.items()
            # only a message reads `ARGS`
            if directions[port] == "input" and port not in clock_ports and port != "ARGS"
            for bit in bits
        }
        read.update(data)
        own -= CONSTANT_BITS
        outputs = {
            bit for port, bits in ports.items() if directions[port] == "output" for bit in bits
        }
        clocked.extend((clock, data) for clock in own)
        if kind in LEVEL_CELLS:
            level = ASYNCHRONOUS_PORTS if clock_ports else (*ASYNCHRONOUS_PORTS, "EN", "D")
            changes = {bit for port in level for bit in ports.get(port, ())} - CONSTANT_BITS
            owners.update(dict.fromkeys(outputs, own | changes))
        elif kind in EDGE_CELLS or kind in GLOBAL_CELLS or kind == "$mem_v2":
            owners.update(dict.fromkeys(outputs, own))
            if kind == "$mem_v2" and "0" in parameters["RD_CLK_ENABLE"]:
                # An asynchronous read passes its address through.
                drivers.update(dict.fromkeys(ports["RD_DATA"], set(ports["RD_ADDR"])))
        else:
            for bit in outputs:
                drivers[bit] = data
    if multiclock:
        return sorted(unknown | _clock_races(clocked, drivers, owners, bit_names))
    clocks = sorted({bit for bit, _ in events}, key=str)
    if len(clocks) > 1:
        shown = ", ".join(bit_names.get(bit, str(bit)) for bit in clocks)
        problems.add(f"it has {len(clocks)} clocks: {shown}")
    if any(polarity != "1" for _, polarity in events):
        problems.add("it uses a falling clock edge")
    for bit in clocks:
        if bit == GLOBAL_CLOCK:
            continue
        shown = bit_names.get(bit, str(bit))
        if bit not in inputs:
            problems.add(f"the clock {shown} is no top-level input: it is derived or constant")
        elif bit in read:
            problems.add(f"the clock {shown} is also read as data")
    return sorted(unknown | problems)


def default_depth(settings: Sby.Settings) -> int:
    """Return the depth of a task when `depth` is not set."""
    return 2 * DEFAULT_DEPTH if settings.multiclock else DEFAULT_DEPTH


def depth_of(settings: Sby.Settings) -> int | None:
    """Return the depth that the task asks for: `depth` or its default for `bmc` and `cover`, and
    None for `prove`, which has none."""
    return None if settings.mode == "prove" else settings.depth or default_depth(settings)


def _refuse_default_disable(names: dict[str, Source]) -> None:
    """Refuse `default disable iff` in Verilog and SystemVerilog sources. yosys-slang ignores it, so
    an assumption or a cover statement would hold during reset, which can give a wrong pass.

    shortcut: this reads the text, so it refuses the words in an inactive `ifdef` and misses them
    when a macro spells them; remove it when the frontend refuses or honors the construct."""
    for name, src in names.items():
        if src.type in (*HEADER_TYPES, SourceType.Verilog, SourceType.SystemVerilog):
            text = COMMENT_OR_STRING.sub(" ", src.path.read_text(errors="replace"))
            if DEFAULT_DISABLE.search(text):
                raise FlowException(
                    f"sby cannot read `default disable iff` in {name}: the frontend ignores it, "
                    "so a verdict could be wrong. Write `disable iff (...)` on each property"
                )


def _word(value: Any) -> str:
    """Return `value` as one word of the SBY task: refuse what would split it or end the line."""
    text = str(value)
    if not text or NOT_IN_A_WORD.search(text):
        raise FlowException(f"the SBY task cannot hold {text!r} as one word")
    return text


class Sby(Flow):
    """Check the formal properties of a design with SymbiYosys (SBY) and Yosys.

    A run is one SBY task, and `prove` adds a second one after a proof. `rtl.top` names the formal harness; its sources hold the design under
    test, the assertions, assumptions and cover statements, and the clock, reset and initial state
    that the proof assumes. SystemVerilog and Verilog are read by yosys-slang with `FORMAL`
    defined and `SYNTHESIS` undefined, and VHDL by the GHDL plugin with PSL in comments.
    `rtl.parameters` sets the parameters (generics) of the top only, and a name that the top does
    not have fails the run. `default disable iff` is refused, because the frontend ignores it.
    A source keeps its path relative to the design root, a generated source its name in the
    tree. The files of a generator's storage are in `xeda_generated/`, and the files of a
    dependency outside the design tree in `xeda_dependencies/<number>/`.

    With the default `multiclock` false, the model has one implicit clock. The run fails for a
    design that does not fit it: more than one clock, a falling edge, a derived clock, a clock read
    as data, an asynchronous reset or a latch. `multiclock` models them, and `depth` then counts
    global sampling steps. It still refuses logic that reads its own clock as data, and logic that
    reads the state behind a state-driven clock: SBY gives such a cell the value before the edge.

    `mode` is required. `bmc` passes when no assertion fails within `depth` steps, and `cover`
    when every cover statement is reached within `depth` steps (assertions are not checked); both
    run `smtbmc bitwuzla`, which first checks that the assumptions are satisfiable. `prove` passes
    when `btor rIC3 -e ic3` (the IC3 engine of rIC3) proves every assertion for all time. It has no
    `depth`. After a proof, a second task checks with `smtbmc bitwuzla` that the
    assumptions hold for a trace of 20 steps (40 with `multiclock`), and the run fails if they do
    not. A contradiction that appears only after these steps still passes vacuously, and so does
    one that a stalled clock avoids with `multiclock`. A failed assertion or an unreached
    cover goal is a rejection, with the traces in the evidence archive. Any other status of SBY, a
    status that does not agree with its exit code, a model with no assertion (`bmc`, `prove`) or
    no cover statement (`cover`), and a symbolic-value (`anyconst`, `anyseq`, `allconst`,
    `allseq`), liveness or blackbox construct fail the run. SBY starts its programs itself, so a
    result is never reused. Every program comes from the directory of the `sby` on PATH, as in
    the OSS CAD Suite. The run records the versions of SBY, Yosys and the solver; no version is
    refused. It is tested with SBY 0.70, Yosys 0.70, Bitwuzla 0.9.1 and rIC3 1.5.2 (OSS CAD Suite
    2026-10-28 on macOS arm64; VHDL with a GHDL plugin rebuilt for that Yosys).
    """

    executables = ("sby", "yosys", "yosys-smtbmc", "bitwuzla", "rIC3", "yosys-witness", "btorsim")

    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
            SourceType.Vhdl,
            SourceType.MemoryFile,
        }
    )

    required_settings = {
        "mode": "the analysis to run: `-s sby.mode=bmc`, `prove` or `cover`; in the design file, as "
        "`flows.{flow}.mode`"
    }

    results_description = describe_results(
        **{
            "formal.mode": MetricSpec("The analysis that ran: bmc, prove or cover.", "text"),
            "formal.depth": MetricSpec(
                "The number of steps that bmc and cover asked for. It is not a depth that SBY "
                "reports as reached, and prove has none.",
                "integer",
            ),
            "formal.engine": MetricSpec(
                "The SBY engine: smtbmc (bmc, cover) or btor (prove).", "text"
            ),
            "formal.solver": MetricSpec(
                "The program of the engine: bitwuzla (bmc, cover) or rIC3, run as `-e ic3` "
                "(prove).",
                "text",
            ),
            "formal.status": MetricSpec(
                "The status of the SBY task: PASS, FAIL, UNKNOWN, TIMEOUT, ERROR or CANCELLED.",
                "text",
            ),
            "formal.assertions": MetricSpec(
                "The assertions in the hierarchy of the top of the prepared model, one for each "
                "instance.",
                "integer",
            ),
            "formal.assumptions": MetricSpec(
                "The assumptions in the hierarchy of the top of the prepared model, one for each "
                "instance.",
                "integer",
            ),
            "formal.unreached_property_files": MetricSpec(
                "The Verilog and SystemVerilog sources that hold an `assert`, `assume` or `cover` "
                "from which no property of the model comes: a checker that no `bind` or instance "
                "reaches, or a property in an inactive `ifdef`. The run only warns about them.",
                "text_list",
            ),
            "formal.covers": MetricSpec(
                "The cover statements in the hierarchy of the top of the prepared model, one for "
                "each instance.",
                "integer",
            ),
        }
    )

    artifacts_description = describe_artifacts(
        "log",
        config=ArtifactSpec(f"The SBY task that the run generated, `{CONFIG}`.", "always"),
        status=ArtifactSpec(
            f"The status file of the task, `{WORK}/status`: status, exit code and seconds.",
            "when SBY wrote it",
        ),
        task_logfile=ArtifactSpec(
            f"The log of the task, `{WORK}/logfile.txt`.", "when SBY wrote it"
        ),
        evidence_archive=ArtifactSpec(
            f"Every file of the SBY work directory as a zip file, `{ARCHIVE}`: the task log, the "
            "model and its logs, the engine logs and every trace (`.vcd`, `.yw`, testbench). "
            f"After a proof, `{ASSUMPTIONS_WORK}/` holds the check of the assumptions. Symbolic "
            "links are left out.",
            "when SBY ended by itself",
        ),
    )

    class Settings(Flow.Settings):
        mode: Literal["bmc", "prove", "cover"] | None = Field(
            None,
            description="The analysis: `bmc` searches for a failing assertion within `depth` "
            "steps, `prove` tries to prove every assertion for all time with IC3 (rIC3), and "
            "`cover` searches for a trace to each cover statement within `depth` steps. It has "
            "no default.",
        )
        depth: int | None = Field(
            None,
            ge=1,
            description="The number of steps: the search bound of `bmc` and `cover` (20 when "
            "unset, 40 with `multiclock`). The last step checks no clocked property, so 1 is only "
            "for a design with no state. A `bmc` pass holds only up to this bound. `prove` has no "
            "bound and refuses `depth`.",
            json_schema_extra=count(),
        )
        multiclock: bool = Field(
            False,
            description="Model every clock edge and asynchronous change (SBY `multiclock on`). "
            "`depth` then counts global sampling steps, and a regular clock needs two for each "
            "cycle. When false, the run fails for a design that has more than one clock, a falling "
            "edge, a derived clock, a clock that is read as data, an asynchronous reset or a latch. "
            "When true, it still fails for logic that reads its own clock as data, or that reads "
            "the state behind a state-driven clock.",
        )
        timeout: Time | None = Field(
            None,
            description="The time limit of the SBY task, a whole number of seconds such as "
            "`10 min`. A task that reaches it ends with the status TIMEOUT, and the run fails. "
            "None sets no limit; Ctrl-C stops a run as in every flow.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a time limit that SBY cannot take, a `prove` with a depth, library paths, which
        the task would not stage, and a run in Docker, which has no image with SBY."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if settings.mode == "prove" and settings.depth is not None:
            raise FlowSettingsException(
                "sby `prove` has no `depth`: rIC3 proves for all time and SBY gives it no "
                "bound, so nothing would check it; remove `depth`"
            )
        seconds = None if settings.timeout is None else settings.timeout.to("s")
        if seconds is not None and (seconds < 1 or not seconds.is_integer()):
            raise FlowSettingsException(
                f"sby needs `timeout` as a whole number of seconds, at least 1 s, got {seconds:g} "
                "s: SBY takes an integer number of seconds"
            )
        if settings.lib_paths:
            raise FlowSettingsException(
                "sby reads only the sources of the design, staged in its task: remove `lib_paths`"
            )
        if settings.docker:
            raise FlowSettingsException("sby has no Docker image: remove `docker`")

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design whose formal model the task cannot state exactly.

        The top must be a plain name. The sources must be in one language, all of them staged
        inside the design root (`staged_names`). `FORMAL` and `SYNTHESIS` are the flow's own, and
        a define or parameter value must be one word of the task. The Synopsys packages of VHDL
        are not supported.
        """
        super().check_design_supported(design, settings)
        rtl = design.rtl
        if not rtl.top or not IDENTIFIER.fullmatch(rtl.top):
            raise FlowSettingsException(
                "sby needs the formal top as a plain module or entity name in `rtl.top`, got "
                f"{rtl.top!r}"
            )
        read = [src for src in rtl.sources if src.type in cls.reads_sources]
        kinds = {src.type for src in read if src.type in UNIT_TYPES}
        if SourceType.Vhdl in kinds and len(kinds) > 1:
            vhdl = ", ".join(str(src.file) for src in read if src.type is SourceType.Vhdl)
            others = [src for src in read if src.type in kinds - {SourceType.Vhdl}]
            raise FlowSettingsException(
                f"sby cannot read the design's Vhdl source(s) {vhdl} together with its "
                f"{' and '.join(sorted({SourceType(src.type).name for src in others}))} source(s) "
                f"{', '.join(str(src.file) for src in others)}: a formal model is VHDL, or Verilog "
                "and SystemVerilog, not both"
            )
        staged_names(design, read)
        if SourceType.Vhdl in kinds and language_of(design).vhdl.synopsys:
            raise FlowSettingsException(
                "sby does not support the VHDL Synopsys packages: remove `language.vhdl.synopsys`"
            )
        for name, value in rtl.defines.items():
            if name in FORMAL_DEFINES:
                raise FlowSettingsException(
                    f"sby defines `FORMAL` and leaves `SYNTHESIS` undefined: remove `{name}` from "
                    "`rtl.defines`"
                )
            if not IDENTIFIER.fullmatch(name) or NOT_IN_A_WORD.search(str(value)):
                raise FlowSettingsException(
                    f"sby cannot pass the define `{name}` = {value!r}: a value cannot hold "
                    "whitespace, `#` or `;`"
                )
        for name, value in rtl.parameters.items():
            text = str(value)
            # GHDL reads the text of a generic by the type of the generic; the Verilog task cannot.
            if (
                SourceType.Vhdl not in kinds
                and isinstance(value, str)
                and AMBIGUOUS_TEXT.fullmatch(value)
                and not SIZED_BINARY.match(value)
            ):
                raise FlowSettingsException(
                    f"sby cannot pass the parameter `{name}` = {value!r}: text that reads as a "
                    f'number or a boolean becomes a string in the task (`-G {name}="{value}"`), '
                    f"not that value. Give it as a typed YAML value in the design file, such as "
                    f"`{name}: {value}`"
                )
            if (
                not IDENTIFIER.fullmatch(name)
                or NOT_IN_A_WORD.search(text)
                or (isinstance(value, str) and '"' in value)
            ):
                raise FlowSettingsException(
                    f"sby cannot pass the parameter `{name}` = {value!r}: a value cannot hold "
                    'whitespace, `#`, `;` or `"`'
                )

    def init(self) -> None:
        self.add_template_filter("word", _word)
        #: The exit code of SBY in this run; None until SBY ends by itself.
        self.exit_status: int | None = None
        #: The status file of this run: its status and exit code, or None.
        self.status: tuple[str, int] | None = None
        self.status_problem: str | None = None
        self.properties: Counter[str] | None = None
        #: Why the default model of SBY cannot hold this design; None until the model is read.
        self.timing: list[str] | None = None
        #: Whether the model has no state and no clocked property.
        self.combinational = False
        #: The staged sources of this run (`staged_names`).
        self.staged: dict[str, Source] = {}

    def always_runs(self) -> str | None:
        return super().always_runs() or (
            "sby starts yosys, its plugins and the programs of its engine itself, and Xeda does "
            "not trace their contents, so it never reuses a formal result"
        )

    def run(self) -> None:
        """Stage the sources, write the task and run SBY in a new work directory."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        design = self.design
        run_directory = self.run_directory
        # First, so that a run that stops before SBY starts leaves no evidence of an earlier one.
        run_directory.remove(WORK, ASSUMPTIONS_WORK, STAGE, TEMPORARY, ARCHIVE)
        names = staged_names(design, self.sources_read())
        self.staged = names
        _refuse_default_disable(names)
        mode = ss.mode or "bmc"
        engine = ENGINES[mode]
        sby, options = self._programs(mode)
        for name, src in names.items():
            staged = run_directory.writable(Path(STAGE) / name)
            staged.parent.mkdir(parents=True, exist_ok=True)
            replacing_copy(src.path, staged)
        vhdl = any(src.type is SourceType.Vhdl for src in names.values())
        parameters = process_parameters(design.rtl.parameters)
        ghdl = GhdlSynth.Settings()
        task = dict(
            timeout=None if ss.timeout is None else int(ss.timeout.to("s")),
            vhdl=vhdl,
            units=[name for name, src in names.items() if src.type in UNIT_TYPES],
            include_dirs=list(
                dict.fromkeys(
                    PurePosixPath(name).parent.as_posix()
                    for name, src in names.items()
                    if src.type in HEADER_TYPES
                )
            ),
            defines=[
                f"-D{k}" if v is None else f"-D{k}={v}" for k, v in design.rtl.defines.items()
            ],
            parameters=parameters,
            # GHDL itself refuses a generic that the top does not have.
            parameters_root=PARAMETERS_ROOT if parameters and not vhdl else None,
            parameters_file=PARAMETERS_FILE,
            timing_model=TIMING_MODEL,
            properties_file=PROPERTIES_FILE,
            # `-fpsl` reads the PSL in comments; GHDL keeps assertions and PSL unless told not to.
            ghdl_flags=ghdl.common_flags(language_of(design).vhdl)
            + ["-fpsl"]
            + ghdl.generics_flags(design.rtl.parameters),
            staged=list(names),
            stage=STAGE,
        )
        config = self.copy_from_template(
            "task.sby",
            script_filename=CONFIG,
            task_mode=mode,
            engine=engine,
            depth=depth_of(ss),
            obligation=OBLIGATION[mode],
            assumptions_only=False,
            **task,
        )
        temporary = run_directory.writable(TEMPORARY)
        temporary.mkdir()
        # Put the directory of `sby` first on its PATH, as the SBY wrapper of the OSS CAD Suite
        # does, so that SBY finds a program without an option there too.
        path = os.pathsep.join(
            [str(Path(sby.executable).parent), self.env_view.get("PATH", os.defpath)]
        )
        env = {"PATH": path, "TMPDIR": temporary, "XDG_STATE_HOME": temporary}
        try:
            sby.run(*options, "-d", WORK, config, env=env)
        except NonZeroExitCode as error:
            self.exit_status = error.exit_code
            # A failed assertion or an unreached cover goal is a verdict: `check_results` judges
            # it from the status. Every other exit fails the run.
            if error.exit_code != EXIT_CODES["FAIL"]:
                raise error.with_message(self._failure_text(error.exit_code)) from error
        else:
            self.exit_status = 0
            if mode == "prove":
                self._check_assumptions(sby, options, env, task)
        finally:
            run_directory.remove(TEMPORARY)
            if self.exit_status is not None:
                self._archive_evidence()
            for key, name in (
                ("config", CONFIG),
                ("status", f"{WORK}/status"),
                ("task_logfile", f"{WORK}/logfile.txt"),
                ("evidence_archive", ARCHIVE),
            ):
                if self.wrote_output(name):
                    self.artifacts[key] = name

    def _check_assumptions(
        self, sby: Tool, options: list[str], env: dict[str, Any], task: dict[str, Any]
    ) -> None:
        """Fail a proof whose assumptions no trace can meet, as `smtbmc` does for `bmc`.

        A second task keeps only the assumptions and asks `smtbmc` for its presat check at each
        step, so it solves no assertion. A counterexample of `prove` meets the assumptions along
        its trace, so only a pass needs this. It checks `default_depth` steps."""
        assert isinstance(self.settings, self.Settings)
        config = self.copy_from_template(
            "task.sby",
            script_filename=ASSUMPTIONS_CONFIG,
            task_mode="bmc",
            engine=ENGINES["bmc"],
            depth=default_depth(self.settings),
            obligation=OBLIGATION["prove"],
            assumptions_only=True,
            **task,
        )
        try:
            sby.run(*options, "-d", ASSUMPTIONS_WORK, config, env=env)
        except NonZeroExitCode as error:
            text = self._failure_text(error.exit_code, ASSUMPTIONS_WORK)
            raise error.with_message(f"rIC3 passed, but its assumptions fail: {text}") from error
        status = self.report_file(Path(ASSUMPTIONS_WORK) / "status", optional=True)
        match = (
            None if status is None else STATUS_LINE.fullmatch(status.read_text(errors="replace"))
        )
        if match is None or (match["status"], match["code"]) != ("PASS", "0"):
            self.status_problem = "SBY wrote no PASS for the check of the assumptions"

    def _programs(self, mode: str) -> tuple[Tool, list[str]]:
        """Return the `sby` on PATH and the SBY options that select the programs of `mode` in its
        directory. Record the versions of `sby`, `yosys` and the solver.

        The SBY wrapper of the OSS CAD Suite puts its own directory first on PATH, so the programs
        in the directory of `sby` are the ones that run. Each must be there: a program of the same
        name elsewhere does not replace it. The options override a variable such as `YOSYS`. The
        paths are kept as PATH gives them, so a wrapper script stays in its place."""
        path = self.env_view.get("PATH", os.defpath)
        found = shutil.which("sby", path=path)
        if found is None:
            raise ExecutableNotFound("sby", type(self).__name__, path)
        sby = Path(os.path.abspath(found))
        if not SHELL_WORD.fullmatch(str(sby.parent)):
            raise FlowException(
                f"sby cannot run the programs in {str(sby.parent)!r}: SBY puts their paths into "
                "shell commands without quotes, so the directory of sby may hold only letters, "
                "digits and `_ . + - /`; install SBY in such a directory"
            )
        options: list[str] = []
        for name, option in PROGRAMS[mode].items():
            program = sby.parent / name
            if not (program.is_file() and os.access(program, os.X_OK)):
                raise ExecutableNotFound(name, f"sby, which runs it from {sby.parent}")
            if option is not None:
                options += [option, str(program)]
        Tool(executable=str(sby.parent / "yosys"), version_flag="-V")
        for solver in {ENGINES[mode].split()[1], ENGINES["bmc"].split()[1]} & set(PROGRAMS[mode]):
            Tool(executable=str(sby.parent / solver))
        return Tool(executable=str(sby)), options

    def _failure_text(self, exit_code: int, work: str = WORK) -> str:
        """Return what SBY says about a run that ended with `exit_code`: the status that the code
        stands for and the first error of the log of the task in `work`."""
        status = {code: name for name, code in EXIT_CODES.items()}.get(exit_code, "no status")
        text = f"SBY ended with {status} (exit code {exit_code})"
        task_log = self.report_file(Path(work) / "logfile.txt", optional=True)
        if task_log is None:
            return text
        for line in task_log.read_text(errors="replace").splitlines():
            if "Assumptions are unsatisfiable" in line:  # the presat check of `smtbmc`
                return f"{text}: the assumptions are unsatisfiable, so every check would hold"
            if "error:" not in line.lower():  # GHDL writes `error:`, before yosys's `ERROR:`
                continue
            error = line.split("] ", 1)[-1].strip()
            if "selection is empty" in error:
                assert isinstance(self.settings, self.Settings)
                kind = OBLIGATION[self.settings.mode or "bmc"]
                return (
                    f"{text}: the hierarchy of `{self.design.rtl.top}` has no {kind} ({error}). A "
                    "checker module has no effect until a `bind` places it: list the file that "
                    "holds the `bind`"
                )
            if "selection is not empty" in error:
                return (
                    f"{text}: the design has an unsupported construct: a symbolic value "
                    f"(anyconst, anyseq, allconst, allseq), liveness, fairness or a blackbox "
                    f"({error})"
                )
            if "clk2fflogic" in error:
                return f"{text}: {error} Set `multiclock` to model this design"
            if "unsupported SVA feature" in error:
                return (
                    f"{text}: {error}. The frontend reads a concurrent assertion only as "
                    "`[disable iff (c)] <boolean>`: write `|->`, `|=>` and `##` as a Boolean check "
                    "with `$past` or a register"
                )
            if "No such file or directory" in error:
                return (
                    f"{text}: {error}. If it is a header, list it in `rtl.sources`, with "
                    "`type: SVHeader` for a header with the extension `.sv`"
                )
            return f"{text}: {error}"
        return text

    def _failed_property(self) -> str:
        """Return the place of the first failed property in the task log, as text for a verdict,
        or nothing when the log does not name one."""
        task_log = self.report_file(Path(WORK) / "logfile.txt", optional=True)
        if task_log is None:
            return ""
        for line in task_log.read_text(errors="replace").splitlines():
            for mark in ("Assert failed in ", "Unreached cover statement at "):
                if mark in line:
                    return f" (in {line.split(mark, 1)[1].strip()})"
        return ""

    def _archive_evidence(self) -> None:
        """Write every regular file of the work directory into the evidence archive. A symbolic
        link is neither followed nor archived."""
        work = self.run_file(WORK)
        if work.is_symlink() or not work.is_dir():
            return
        target = self.run_directory.writable(ARCHIVE)
        with replacing_file(target, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for tree, prefix in ((work, ""), (self.run_file(ASSUMPTIONS_WORK), ASSUMPTIONS_WORK)):
                if tree.is_symlink():
                    continue
                for directory, subdirectories, files in os.walk(tree):
                    subdirectories.sort()
                    for name in sorted(files):
                        path = Path(directory) / name
                        if not path.is_symlink() and path.is_file():
                            z.write(path, (Path(prefix) / path.relative_to(tree)).as_posix())

    def parse_reports(self) -> bool:
        """Read the status of the task and the properties of its model. Judge nothing."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.results["formal.mode"] = ss.mode
        if (depth := depth_of(ss)) is not None:
            self.results["formal.depth"] = depth
        self.results["formal.engine"], self.results["formal.solver"] = ENGINES[
            ss.mode or "bmc"
        ].split()[:2]
        model = self.report_file(Path(WORK) / "model" / "design.json", optional=True)
        if model is not None:
            try:
                self.properties = obligations(json.loads(model.read_text()))
            except (ValueError, KeyError, TypeError) as error:
                log.error("The SBY model %s is unreadable: %s", model, error)
            else:
                self.results["formal.assertions"] = self.properties["assert"]
                self.results["formal.assumptions"] = self.properties["assume"]
                self.results["formal.covers"] = self.properties["cover"]
        attributes = self.report_file(Path(WORK) / "src" / PROPERTIES_FILE, optional=True)
        if attributes is not None:
            unreached = unreached_property_files(
                self.staged, attributes.read_text(errors="replace")
            )
            self.results["formal.unreached_property_files"] = unreached
            if unreached:
                log.warning(
                    "No property of the model comes from %s, which hold(s) one: a checker there "
                    "needs a `bind` or an instance under `%s`, or the property is in an inactive "
                    "`ifdef`",
                    ", ".join(unreached),
                    self.design.rtl.top,
                )
        timing = self.report_file(Path(WORK) / "src" / TIMING_MODEL, optional=True)
        if timing is not None:
            try:
                flattened = json.loads(timing.read_text())
                self.timing = timing_problems(flattened, ss.multiclock)
                self.combinational = is_combinational(flattened)
            except (ValueError, KeyError, IndexError, TypeError) as error:
                log.error("The timing model %s is unreadable: %s", timing, error)
        status_file = self.report_file(Path(WORK) / "status")
        if status_file is None:
            self.status_problem = "SBY wrote no status for this run"
            return False
        text = status_file.read_text(errors="replace")
        match = STATUS_LINE.fullmatch(text)
        if match is None or match["status"] not in EXIT_CODES:
            self.status_problem = f"the SBY status {text[:80]!r} is malformed"
            return False
        self.status = (match["status"], int(match["code"]))
        self.results["formal.status"] = match["status"]
        return True

    def check_results(self) -> bool:
        """Pass a fresh PASS, reject a fresh FAIL, and fail everything else."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        problem = self.status_problem
        if problem is None and self.status is not None:
            status, code = self.status
            kind = OBLIGATION[ss.mode or "bmc"]
            unsupported = sorted(
                key
                for key, number in (self.properties or {}).items()
                if number and key not in CHECKED
            )
            if code != EXIT_CODES[status] or code != self.exit_status:
                problem = (
                    f"the SBY status {status} with exit code {code} does not agree with the exit "
                    f"code {self.exit_status} of SBY"
                )
            elif status not in ("PASS", "FAIL"):
                problem = f"SBY ended with {status}"
            elif self.properties is None:
                problem = "SBY wrote no readable model for this run"
            elif unsupported:
                problem = (
                    f"the model has unsupported formal constructs: {', '.join(unsupported)}; "
                    "sby checks only assertions, assumptions and cover statements"
                )
            elif self.timing is None:
                problem = "SBY wrote no readable timing model for this run"
            elif self.timing and ss.multiclock:
                problem = (
                    f"SBY reads the data of a clocked cell one step before its clock edge, and "
                    f"this design depends on the later value: {'; '.join(self.timing)}. So a "
                    "verdict could be wrong. Change the design"
                )
            elif self.timing:
                problem = (
                    f"the default model of SBY has one clock, and this design does not fit it: "
                    f"{'; '.join(self.timing)}. So a verdict could be wrong. Set "
                    "`multiclock` to model it, or change the design"
                )
            elif ss.depth == 1 and not self.combinational:
                problem = (
                    "a `depth` of 1 checks no clocked property, and this design has state or one: "
                    "set `depth` to at least 2"
                )
            elif not self.properties[kind]:
                problem = (
                    f"the hierarchy of `{self.design.rtl.top}` has no {kind} statement, so "
                    f"`{ss.mode}` checks nothing"
                )
            elif status == "FAIL":
                depth = depth_of(ss)
                within = "" if depth is None else f" within depth {depth}"
                where = self._failed_property()
                self.reject(
                    f"a cover goal is not reached{within}{where}"
                    if ss.mode == "cover"
                    else f"an assertion fails{within}{where}: the counterexample is in `{ARCHIVE}`"
                )
                return False
        if problem is not None:
            record_failure(self, problem)
            return False
        return self.status is not None
