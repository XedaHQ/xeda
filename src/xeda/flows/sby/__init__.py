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
#: The directory of the task that holds the files of the storage of the design's generator. They
#: have no place in the design tree.
GENERATED_DIRECTORY = "xeda_generated"
#: The directory of the task that holds one directory for each dependency that is outside the
#: design root, numbered by the order of its first source.
DEPENDENCIES_DIRECTORY = "xeda_dependencies"
#: The flattened model that the task writes beside the staged sources, for `timing_problems`. A
#: source of this name, in any letter case, is refused. Keep it lowercase, as `PARAMETERS_FILE`.
TIMING_MODEL = "xeda_timing.json"
ARCHIVE = "sby_evidence.zip"
#: The SBY engine line of each mode. `smtbmc` runs its presat check of the assumptions. `prove` uses
#: one process of rIC3, IC3 only: the default portfolio of rIC3 writes `/tmp/rIC3` outside the run
#: root, and `btor` has no presat check. SBY gives `btor rIC3` in `prove` no depth.
ENGINES = {"bmc": "smtbmc bitwuzla", "cover": "smtbmc bitwuzla", "prove": "btor rIC3 -e ic3"}
#: The depth of `bmc` and `cover` when `depth` is not set.
DEFAULT_DEPTH = 20
#: The programs that SBY starts in each mode, by the option of SBY that names each one. SBY finds a
#: program without an option (None) on its PATH.
PROGRAMS: dict[str, dict[str, str | None]] = {
    "bmc": {"yosys": "--yosys", "yosys-smtbmc": "--smtbmc", "bitwuzla": None},
    "cover": {"yosys": "--yosys", "yosys-smtbmc": "--smtbmc", "bitwuzla": None},
    # A counterexample of `btor` runs `yosys-witness` and `btorsim`.
    "prove": {"yosys": "--yosys", "rIC3": "--rIC3", "yosys-witness": "--witness", "btorsim": None},
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
    gives the root of each generator of a dependency (`Source.owner`), and `outside` numbers the
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
        home = design.root if src.owner is None else roots[src.owner]
        stored = storage_name(name)
        if stored is not None:
            name = (PurePosixPath(GENERATED_DIRECTORY) / stored).as_posix()
    elif src.dependency_place is not None:
        name = src.dependency_place
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
    roots = {unit.generation_label: unit.root for unit in design.generation_units}
    outside: dict[str, str] = {}
    names: dict[str, Source] = {}
    folded: dict[str, str] = {}
    for src in sources:
        name = _staged_name(design, src, roots, outside)
        if not STAGED_NAME.fullmatch(name if src.located else name.replace("*", "_")):
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
        if name.lower() == TIMING_MODEL:
            raise FlowSettingsException(
                f"sby cannot stage {name!r}: the task writes {TIMING_MODEL} itself; rename the source"
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
        first.located
        and second.located
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


def timing_problems(model: dict[str, Any]) -> list[str]:
    """Return why the default model of SBY cannot hold a flattened design, or an empty list.

    The model is the output of Yosys `write_json` (`TIMING_MODEL`). With `multiclock off`, SBY
    replaces every clock with one implicit step and does not check that this is valid. It is valid
    for state, assertions and memory ports on the rising edge of one bit of one top-level input,
    and for logic that reads no such clock. A clock bit that is read as data has a value in the
    model that it never has in the design. Raise `ValueError`, `KeyError`, `IndexError` or
    `TypeError` for a model of another shape.
    """
    tops = [
        module
        for module in model["modules"].values()
        if int(module.get("attributes", {}).get("top", "0"), 2) == 1
    ]
    if len(tops) != 1:
        raise ValueError(f"the model has {len(tops)} top modules")
    module = tops[0]
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
    for cell in module["cells"].values():
        kind, parameters, ports = cell["type"], cell["parameters"], cell["connections"]
        timing: tuple[str, ...] = ()  # the ports that are no data of the model
        if kind in LEVEL_CELLS:
            problems.add(f"the cell {kind} can change between clock edges")
        elif kind in EDGE_CELLS:
            timing = ("CLK",)
            events.add((ports["CLK"][0], parameters["CLK_POLARITY"][-1]))
        elif kind in ("$check", "$print"):
            timing = ("TRG", "ARGS")  # only a message reads `ARGS`
            if "1" in parameters["TRG_ENABLE"]:
                polarities = parameters["TRG_POLARITY"][::-1]
                events.update((bit, polarities[index]) for index, bit in enumerate(ports["TRG"]))
        elif kind in GLOBAL_CELLS:
            events.add((GLOBAL_CLOCK, "1"))
        elif kind == "$mem_v2":
            timing = ("RD_CLK", "WR_CLK")
            for prefix in ("RD", "WR"):
                polarities = parameters[f"{prefix}_CLK_POLARITY"][::-1]
                for index, enabled in enumerate(parameters[f"{prefix}_CLK_ENABLE"][::-1]):
                    if enabled == "1":
                        events.add((ports[f"{prefix}_CLK"][index], polarities[index]))
            if any(bit != "0" for bit in ports["RD_ARST"]):
                problems.add("a memory read port has an asynchronous reset")
        elif not kind.startswith("$") or any("CLK" in port or port == "TRG" for port in ports):
            problems.add(f"the cell {kind} has timing that the flow does not know")
        directions = cell["port_directions"]
        read.update(
            bit
            for port, bits in ports.items()
            if directions[port] == "input" and port not in timing
            for bit in bits
        )
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
    return sorted(problems)


def depth_of(settings: Sby.Settings) -> int | None:
    """Return the depth that the task asks for: `depth` or its default for `bmc` and `cover`, and
    None for `prove`, which has none."""
    return None if settings.mode == "prove" else settings.depth or DEFAULT_DEPTH


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

    One run is one SBY task. `rtl.top` names the formal harness; its sources hold the design under
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
    global sampling steps.

    `mode` is required. `bmc` passes when no assertion fails within `depth` steps, and `cover`
    when every cover statement is reached within `depth` steps (assertions are not checked); both
    run `smtbmc bitwuzla`, which first checks that the assumptions are satisfiable. `prove` passes
    when `btor rIC3 -e ic3` (the IC3 engine of rIC3) proves every assertion for all time. It has no
    `depth`, needs a `timeout`, and has no check of the assumptions: contradictory assumptions pass
    vacuously, and the counts of the results do not show it. A failed assertion or an unreached
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
            "Symbolic links are left out.",
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
            "unset). A `bmc` pass holds only up to this bound. `prove` has no bound and refuses "
            "`depth`.",
            json_schema_extra=count(),
        )
        multiclock: bool = Field(
            False,
            description="Model every clock edge and asynchronous change (SBY `multiclock on`). "
            "`depth` then counts global sampling steps, and a regular clock needs two for each "
            "cycle. When false, the run fails for a design that has more than one clock, a falling "
            "edge, a derived clock, a clock that is read as data, an asynchronous reset or a latch.",
        )
        timeout: Time | None = Field(
            None,
            description="The time limit of the SBY task, a whole number of seconds such as "
            "`10 min`. A task that reaches it ends with the status TIMEOUT, and the run fails. "
            "None sets no limit, except that `prove` needs one: its proof has no bound.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a time limit that SBY cannot take, a `prove` with a depth or with no time limit,
        library paths, which the task would not stage, and a run in Docker, which has no image
        with SBY."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if settings.mode == "prove":
            if settings.depth is not None:
                raise FlowSettingsException(
                    "sby `prove` has no `depth`: rIC3 proves for all time and SBY gives it no "
                    "bound, so nothing would check it; remove `depth`"
                )
            if settings.timeout is None:
                raise FlowSettingsException(
                    "sby `prove` needs `timeout`: the proof of rIC3 has no bound and may run "
                    "forever; set it, for example `-s sby.timeout=10min`"
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
                f"{' and '.join(sorted({src.type.name for src in others}))} source(s) "
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
        run_directory.remove(WORK, STAGE, TEMPORARY, ARCHIVE)
        names = staged_names(design, self.sources_read())
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
        config = self.copy_from_template(
            "task.sby",
            script_filename=CONFIG,
            timeout=None if ss.timeout is None else int(ss.timeout.to("s")),
            engine=engine,
            depth=depth_of(ss),
            obligation=OBLIGATION[mode],
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
            # `-fpsl` reads the PSL in comments; GHDL keeps assertions and PSL unless told not to.
            ghdl_flags=ghdl.common_flags(language_of(design).vhdl)
            + ["-fpsl"]
            + ghdl.generics_flags(design.rtl.parameters),
            staged=list(names),
            stage=STAGE,
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
            self.exit_status = 0
        except NonZeroExitCode as error:
            self.exit_status = error.exit_code
            # A failed assertion or an unreached cover goal is a verdict: `check_results` judges
            # it from the status. Every other exit fails the run.
            if error.exit_code != EXIT_CODES["FAIL"]:
                raise error.with_message(self._failure_text(error.exit_code)) from error
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
        Tool(executable=str(sby.parent / ENGINES[mode].split()[1]))
        return Tool(executable=str(sby)), options

    def _failure_text(self, exit_code: int) -> str:
        """Return what SBY says about a run that ended with `exit_code`: the status that the code
        stands for and the first error of the task log."""
        status = {code: name for name, code in EXIT_CODES.items()}.get(exit_code, "no status")
        text = f"SBY ended with {status} (exit code {exit_code})"
        task_log = self.report_file(Path(WORK) / "logfile.txt", optional=True)
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
            for directory, subdirectories, files in os.walk(work):
                subdirectories.sort()
                for name in sorted(files):
                    path = Path(directory) / name
                    if not path.is_symlink() and path.is_file():
                        z.write(path, path.relative_to(work).as_posix())

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
        if not ss.multiclock:
            timing = self.report_file(Path(WORK) / "src" / TIMING_MODEL, optional=True)
            if timing is not None:
                try:
                    self.timing = timing_problems(json.loads(timing.read_text()))
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
            elif not ss.multiclock and self.timing is None:
                problem = "SBY wrote no readable timing model for this run"
            elif not ss.multiclock and self.timing:
                problem = (
                    f"the default model of SBY has one clock, and this design does not fit it: "
                    f"{'; '.join(self.timing)}. So a verdict could be wrong. Set "
                    "`multiclock` to model it, or change the design"
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
