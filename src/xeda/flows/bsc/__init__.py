"""The Bluespec flows, which compile Bluespec (BSV and BH) with the Bluespec compiler, `bsc`.

The `bsc` flow generates Verilog for the design's top module, with every Verilog file that
module needs. The `bsc_sim` flow compiles a Bluespec testbench and simulates it with Bluesim.
It can also simulate with Verilator, Icarus Verilog or another Verilog simulator, through the
Verilog link step of `bsc`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from importlib.resources import files
from abc import ABCMeta
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, Literal

import colorama

from ...dataclass import WORKING, Field, field_validator
from ...design import Design, DesignSource, SourceType
from ...flow import (
    DESIGN,
    ArtifactSpec,
    Flow,
    FlowFatalError,
    FlowSettingsException,
    MetricSpec,
    Out,
    SimFlow,
    describe_artifacts,
    describe_results,
)
from ...flow.derived import DerivedSources, DesignConversion, Generated, Passed
from ...flow.metrics import program_logs
from ...flow.sim import SimEvidence
from .sim_evidence import (
    bluesim_evidence,
    iverilog_evidence,
    modelsim_evidence,
    vcs_evidence,
    verilator_evidence,
    xsim_evidence,
)
from ...tool import Docker, Tool
from ...units import count
from ...utils import NonZeroExitCode, replacing_copy, replacing_file, tcl_word, unique

log = logging.getLogger(__name__)

_UNCERTIFIED_SIMULATORS = {"cvc", "cver", "isim", "ncverilog", "veriwell"}
_SUPPORTED_SIMULATORS = (
    "bluesim",
    "verilator",
    "iverilog",
    "modelsim",
    "questa",
    "vcs",
    "vcsi",
    "xsim",
)

__all__ = [
    "MIN_BSC_VERSION",
    "Bsc",
    "BscSim",
    "BscTool",
]

#: The oldest bsc release the flows support. 2026.07 turned `-aggressive-conditions` and
#: `-sched-conditions` on by default, stopped generalizing untyped `let`s, removed `-cross-info`,
#: and made Bluesim exit with a failure status on `$fatal`; 2026.07.1 is its bug-fix release.
MIN_BSC_VERSION: tuple[int, ...] = (2026, 7, 1)
#: The oldest Verilator release that `bsc_sim` links Xeda's runtime hooks into. The model runs under
#: bsc's own driver, so the floor of the `verilator` flow (Verilator's `--main`) does not apply.
MIN_BSC_VERILATOR_VERSION: tuple[int, ...] = (5, 44)

#: "bluesim", or a Verilog simulator `bsc -vsim` links a model for (`$BLUESPECDIR/exec`).
SimulatorName = Literal[
    "bluesim",
    "verilator",
    "iverilog",
    "cvc",
    "cver",
    "isim",
    "modelsim",
    "ncverilog",
    "questa",
    "vcs",
    "vcsi",
    "veriwell",
    "xsim",
]

#: bsc message tags (`G0010`), or the keywords its message lists also take.
_MESSAGE_TAG = re.compile(r"^(?:[A-Z]\d{4}|ALL|NONE)$")
_VERILOG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_MACRO_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERILOG_MODULE = re.compile(r"\b(?:module|macromodule)\s+([A-Za-z_][A-Za-z0-9_$]*)")
_VERILOG_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
#: A module instance: its type, an optional parameter list (parentheses nested two deep, as in
#: `#(.init(0))`), the instance name and its port list's opening parenthesis.
_VERILOG_INSTANCE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)\s*)?"
    r"[A-Za-z_][A-Za-z0-9_$]*\s*\("
)
#: With `positive_reset`, the first file of the Verilog that bsc collects: it defines
#: `BSV_POSITIVE_RESET`.
BSV_DEFINES_FILE = "bsv_defines.v"
#: The macro that `positive_reset` defines.
BSV_POSITIVE_RESET = "BSV_POSITIVE_RESET"
#: bsc compiles BSV from `.bsv` and BH (Bluespec Classic) from `.bs`; it rejects other suffixes.
_BSC_SOURCE_SUFFIXES = (".bsv", ".bs")
#: The Verilog sources of a design: the modules a Bluespec package imports, which bsc finds on
#: its Verilog search path and the simulators read.
_VERILOG_TYPES = (SourceType.Verilog, SourceType.SystemVerilog)
#: What bsc links in for imported C functions: C and C++ sources, objects and archives. A source
#: is one of them by its type, as for every flow, whatever its suffix.
_FOREIGN_TYPES = (SourceType.C, SourceType.Cpp, SourceType.ObjectFile)
#: What separates the entries of a bsc search path, so no entry can contain it.
_PATH_SEPARATOR = ":"


@dataclass(frozen=True)
class _Collected:
    """What `Bsc._collect_verilog` found: the modules under the top, and the Verilog files that
    define them, by where each came from.

    `defines` is the file that defines `BSV_POSITIVE_RESET` (with `positive_reset`), `library`
    the modules copied from bsc's library, `design_verilog` the design's own Verilog sources and
    `generated` the modules bsc generated, each module after those it instantiates."""

    modules: list[str]
    defines: list[Path]
    library: list[Path]
    design_verilog: list[Path]
    generated: list[Path]

    @property
    def files(self) -> list[Path]:
        """The Verilog files of the top module, each once, in order."""
        return unique(self.defines + self.library + self.design_verilog + self.generated)


class BscTool(Tool):
    """The Bluespec compiler: https://github.com/B-Lang-org/bsc"""

    executable: str = "bsc"
    version_flag: list[str] | None = ["-v"]
    version_regexps: list[re.Pattern[str] | str] = [
        re.compile(r"Bluespec Compiler,\s+version\s+(?P<version>\d+(?:\.\d+)+)", re.IGNORECASE)
    ]
    minimum_version: tuple[int | str, ...] | None = MIN_BSC_VERSION
    # There is no public bsc image: build one from the Dockerfile in bsc's repository
    # (`docker build -t bsc .`) or name your own with the flow's `docker` setting.
    docker: Docker | None = Docker(image="bsc")  # type: ignore
    highlight_rules: dict[str, str] | None = {
        r"^(Error:)(.*)$": colorama.Fore.RED + colorama.Style.BRIGHT + r"\g<0>",
        r"^(Warning:)(.*)$": colorama.Fore.YELLOW
        + colorama.Style.BRIGHT
        + r"\g<1>"
        + colorama.Style.NORMAL
        + r"\g<2>",
    }


#: The number of diagnostic lines that a failed bsc run quotes in the message of the failure.
DIAGNOSTICS_SHOWN = 5
#: The log of the compilation of each Bluespec package, and the log of the link. A run that
#: compiles several packages keeps the log of the last one: the one that failed, if any did.
COMPILE_LOG = "bsc_compile.log"
LINK_LOG = "bsc_link.log"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: The lines of the output of bsc, and of Verilator, the C++ compiler, the linker and `make` that
#: it runs, that say what went wrong: bsc's own `Error:` lines, Verilator's `%Error:` lines, GCC
#: and Clang diagnostics (`file:1:2: error: ...`, `<command-line>: fatal error: ...`), a linker's
#: unresolved symbol, and a failed `make` recipe (`make: *** [...] Error 1`, `make[2]: *** ...`).
#: Verilator's closing count of its errors and make's wait for its other jobs say nothing more.
_DIAGNOSTIC = re.compile(
    r"^Error:"
    r"|^%Error(?:-[A-Z0-9_]+)?:(?! Exiting due to)"
    r"|: (?:fatal )?error:"
    r"|undefined reference to "
    r"|^make(?:\[\d+\])?: \*\*\* (?!Waiting for unfinished jobs)"
)
#: bsc and Verilator continue an error on the lines after it, indented: bsc's first line holds only
#: the position and the code (`Error: "A.bsv", line 5, column 3: (T0043)`).
_CONTINUED = re.compile(r"^%?Error")
#: The longest diagnostic quoted, continuation included.
_DIAGNOSTIC_LENGTH = 300


def diagnostic_lines(text: str) -> list[str]:
    """Return the errors in the output of a bsc run, in the order of the output, each once: one
    line each, with the indented lines that continue a bsc or Verilator error joined to it.

    The first one is the cause. Whatever follows may be a consequence, and with a parallel `make`
    it is far from the end of the output. A line that only says that the compiler gave up
    (`compilation terminated.`) is not one. The result is empty for an output with no error line.
    """
    found: list[str] = []
    continuing = False
    for raw in text.splitlines():
        plain = _ANSI_ESCAPE.sub("", raw)
        line = plain.strip()
        if continuing and line and plain[:1].isspace():
            found[-1] = f"{found[-1]} {line}"
            continue
        continuing = False
        if line and _DIAGNOSTIC.search(line):
            found.append(line)
            continuing = bool(_CONTINUED.match(line))
    return list(
        dict.fromkeys(
            line if len(line) <= _DIAGNOSTIC_LENGTH else line[: _DIAGNOSTIC_LENGTH - 3] + "..."
            for line in found
        )
    )


def _message_list(tags: Sequence[str]) -> str:
    """Return bsc's `:`-separated message list."""
    return ":".join(tags)


def _bsc_path(paths: Iterable[str | Path], what: str) -> str:
    """Return a bsc search path: directories separated by `:`, then `+` for the current value."""
    entries = []
    for path in paths:
        text = str(path)
        if _PATH_SEPARATOR in text:
            raise FlowSettingsException(
                f"{what}: bsc separates search-path entries with ':', so it cannot use the "
                f"directory {text!r}"
            )
        entries.append(text)
    return ":".join(unique(entries) + ["+"])


def _macro_flags(macros: dict[str, Any], what: str, cpp: bool = False) -> list[str]:
    """Return `-D NAME` or `-D NAME=value` for each macro.

    These flags are for bsc's own (BSV) preprocessor and for a Verilog link. With `cpp`, also
    return `-Xcpp -DNAME...` for the C preprocessor, the only preprocessor that BH sources go
    through. `True` (or no value) defines a bare macro. `False` leaves the macro undefined, so a
    design can switch an `ifdef` on and off.
    """
    definitions: list[str] = []
    for name, value in macros.items():
        if not _MACRO_NAME.match(str(name)):
            raise FlowSettingsException(f"{what}: {name!r} is not a valid preprocessor macro name")
        if value is None or value is True:
            definitions.append(str(name))
        elif value is False:
            continue
        else:
            text = str(value)
            if not text or any(c.isspace() for c in text):
                raise FlowSettingsException(
                    f"{what}: the value of macro {name} ({text!r}) must be non-empty and contain "
                    "no whitespace, as bsc requires"
                )
            definitions.append(f"{name}={text}")
    flags: list[str] = []
    for definition in definitions:
        flags += ["-D", definition]
    if cpp:
        for definition in definitions:
            flags += ["-Xcpp", f"-D{definition}"]
    return flags


def _toggle(flag: str, value: bool) -> str:
    """Return bsc's `-flag` when `value` is true and `-no-flag` otherwise."""
    return f"-{flag}" if value else f"-no-{flag}"


def _parse_haskell_strings(text: str) -> list[str]:
    """Return the strings in the Haskell `show` text of a `[String]`.

    `bsc -print-flags-raw` prints paths in this form.
    """
    named = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
    strings: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != '"':
            i += 1
            continue
        i += 1
        chars: list[str] = []
        while i < len(text) and text[i] != '"':
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                i += 1
                esc = text[i]
                if esc.isdigit():
                    j = i
                    while j < len(text) and text[j].isdigit():
                        j += 1
                    chars.append(chr(int(text[i:j])))
                    i = j
                    continue
                if esc != "&":  # `\&` only separates a numeric escape from a following digit
                    chars.append(named.get(esc, esc))
            else:
                chars.append(c)
            i += 1
        strings.append("".join(chars))
        i += 1
    return strings


def _verilog_instances(path: Path, module: str) -> list[str]:
    """Return the module types that `module` instantiates in the Verilog file at `path`.

    An instance has the form `Type [#(...)] name (`. Only the module's own body counts, because
    bsc's library files also hold test modules.
    """
    try:
        text = _VERILOG_COMMENT.sub(" ", path.read_text(errors="replace"))
    except OSError:
        return []
    body = re.search(rf"\bmodule\s+{re.escape(module)}\b(.*?)\bendmodule\b", text, re.DOTALL)
    return unique(_VERILOG_INSTANCE.findall(body.group(1) if body else text))


def _verilog_modules(path: Path) -> list[str]:
    """Return the names of the modules that a Verilog file declares."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return _VERILOG_MODULE.findall(_VERILOG_COMMENT.sub(" ", text))


class BscFlow(Flow, metaclass=ABCMeta):
    """Compile Bluespec with bsc: the settings and the compilation `bsc` and `bsc_sim` share."""

    executables: ClassVar[tuple[str, ...]] = ("bsc",)

    artifacts_description = describe_artifacts("log")

    #: The name of the setting for the output directory, which `cleanup_bobjs` removes modules from.
    output_dir_setting: ClassVar[str]

    #: With the Verilog backend, `_path_flags` puts the testbench's Verilog directories on
    #: `-vsearch`, so `bsc` reads `tb` as `bsc_sim` does. The Bluesim backend reads no Verilog.
    #: `tb` stays declared there, a safe over-approximation.
    design_parts = frozenset({"rtl", "tb"})

    reads_sources = frozenset({SourceType.Bluespec, *_VERILOG_TYPES})

    #: The parts of the design whose `defines` and `parameters` become preprocessor macros
    #: (`_macros`).
    macro_parts: ClassVar[tuple[str, ...]] = ("rtl",)

    class Settings(Flow.Settings):
        # Directories and paths
        bobj_dir: Path = Field(
            Path("bobjs"),
            description="Directory for the packages bsc compiles (`.bo`/`.ba` files, `-bdir`), "
            "relative to the run directory.",
            json_schema_extra=WORKING,
        )
        info_dir: Path | None = Field(
            None,
            description="Directory for bsc's informational files (`-info-dir`), such as the "
            "schedules of `show_schedule` and the graphs of `sched_dot`, relative to the run "
            "directory. Defaults to `bobj_dir`.",
            json_schema_extra=WORKING,
        )
        cleanup_bobjs: bool = Field(
            True,
            description="Before compiling, remove the packages (`.bo`/`.ba`) that an earlier run "
            "compiled in `bobj_dir`. bsc recompiles a package (`-u`) only when its source is newer "
            "than its `.bo`, so without this a change of flags or macros alone is not compiled. "
            "Disable only to reuse the packages of a run with identical settings and sources. "
            "The Verilog modules that an earlier run generated are removed in either case, and "
            "bsc writes them again.",
        )
        search_paths: list[Path] = Field(
            [],
            description="Additional directories bsc searches for imported packages (`-p`), after "
            "the directories of the design's Bluespec sources and before bsc's own libraries, "
            "e.g. for libraries such as bsc-contrib's. Relative to the design root.",
        )
        verilog_search_paths: list[Path] = Field(
            [],
            description="Additional directories searched for the Verilog files of imported "
            '(`import "BVI"`) and library modules (`-vsearch`), before bsc\'s own Verilog '
            "library. The directories of the design's Verilog sources are always searched. "
            "Relative to the design root. Not used by Bluesim, which reads no Verilog.",
        )
        fdir: Path | None = Field(
            None,
            description="Directory that relative file names are resolved against while bsc "
            "elaborates the design, e.g. files opened with `openFile` (`-fdir`); relative to the "
            "design root. Defaults to the run directory.",
        )
        # Preprocessing
        cpp: bool = Field(
            False,
            description="Run the C preprocessor on the sources before bsc's own preprocessor "
            "(`-cpp`), and define the design's macros for it too. BH (`.bs`) sources go through "
            "no other preprocessor, so this is the only way they see `rtl.defines`. They test a "
            "macro with `#ifdef`, where BSV uses `` `ifdef ``.",
        )
        cpp_flags: list[str] = Field(
            [],
            description="Arguments passed to the C preprocessor (`-Xcpp`), one per item, "
            "with `cpp`.",
        )
        # Semantics
        aggressive_conditions: bool = Field(
            True,
            description="Propagate the implicit condition of an action in one branch of an "
            "`if` to the rule predicate together with that branch's condition, so the rule can "
            "fire when only the other branch is blocked (`-aggressive-conditions`). On by "
            "default since bsc 2026.07; turning it off can shorten compile times.",
        )
        sched_conditions: bool = Field(
            True,
            description="Take method conditions into account when computing rule conflicts "
            "(`-sched-conditions`). On by default since bsc 2026.07; turning it off can "
            "shorten compile times.",
        )
        split_if: bool = Field(
            False,
            description="Split rules at `if` statements in their actions, so the branches are "
            "scheduled as separate rules (`-split-if`). Can multiply the number of rules; the "
            "`(* split *)` attribute does it for individual rules. See also `lift`.",
        )
        lift: bool = Field(
            True,
            description="Lift method calls out of the branches of `if` statements, calling a "
            "method once with a multiplexed argument (`-lift`). Recommended without `split_if`; "
            "bsc recommends turning it off with `split_if`.",
        )
        let_gen: bool = Field(
            False,
            description="Generalize the types of untyped `let` bindings, as bsc did before "
            "2026.07 (`-let-gen`). Only for code that relied on it; an explicit type signature "
            "is the fix.",
        )
        resource_scheduling: Literal["off", "simple"] = Field(
            "off",
            description='When more rules call a method than it has ports: "off" fails the '
            'compilation (`-resource-off`); "simple" blocks rules, arbitrarily, until they fit '
            "(`-resource-simple`). Not for finished designs.",
        )
        sat_solver: Literal["yices", "stp"] = Field(
            "yices",
            description="SMT solver bsc uses for disjointness tests and SAT (`-sat-yices` or "
            "`-sat-stp`). Change it only for scheduling or optimization performance problems.",
        )
        check_assert: bool = Field(
            True,
            description="Check the assertions of bsc's `Assert` library, such as "
            "`staticAssert` and `dynamicAssert` (`-check-assert`). Without it they are ignored, "
            "so a testbench's `dynamicAssert` can never fail.",
        )
        # Code generation
        unspecified_to: Literal["X", "0", "1", "Z", "A"] = Field(
            "A",
            description="Value the don't-care bits left in the generated code are tied to: "
            '"X" or "Z" (as in Verilog), "0", "1", or "A" for alternating ones and zeros '
            '(`-unspecified-to`). "X" gives synthesis the most freedom; "X" and "Z" require '
            "`opt_undetermined_vals` and are not supported by Bluesim.",
        )
        opt_undetermined_vals: bool = Field(
            False,
            description="Let the Verilog backend choose don't-care values from their context "
            "(`-opt-undetermined-vals`). Better hardware, but Bluesim and Verilog simulations "
            "may then differ in intermediate values.",
        )
        optimize: bool = Field(
            False,
            description="Simplify the conditions of rules and `if`s during elaboration (`-O`, "
            "a hidden bsc flag, the same as `-opt-bool`): two-level minimization of a condition "
            "of up to 8 variables, bsc's BDD-based simplifier beyond. It can save a few percent "
            "of the logic after synthesis, but can multiply bsc's run time: the core of "
            "bluespec/Piccolo, which compiles in about 80 s without it, did not finish in 30 "
            "minutes with it.",
        )
        extra_optimize_flags: list[str] = Field(
            [],
            description="Further bsc optimization flags: the `-opt-*` flags of `bsc "
            '-help-hidden` that bsc leaves off, e.g. ["-opt-bit-const"]. Measure before adopting '
            'one: "-opt-if-mux" enlarges some designs.',
        )
        synthesize_to_boolean: bool = Field(
            False,
            description="Synthesize all primitive operators into simple boolean operations "
            "(`-synthesize`). It hides the arithmetic from a downstream synthesis tool: on the "
            "examples, their adders and multipliers then mapped to no carry chains or DSP blocks, "
            "for up to 2.4 times the LUTs.",
        )
        remove_false_rules: bool = Field(
            True,
            description="Remove rules whose condition is provably false (`-remove-false-rules`). "
            "bsc warns about each.",
        )
        remove_empty_rules: bool = Field(
            True,
            description="Remove rules whose bodies have no actions (`-remove-empty-rules`). bsc "
            "warns about each.",
        )
        remove_starved_rules: bool = Field(
            False,
            description="Remove rules the generated schedule never fires "
            "(`-remove-starved-rules`). bsc warns about each; they usually point at a scheduling "
            "problem.",
        )
        remove_unused_modules: bool = Field(
            False,
            description="Remove submodules not connected to any output from the generated "
            "Verilog (`-remove-unused-modules`). For modules headed for synthesis, not for "
            "testbenches.",
        )
        keep_fires: bool = Field(
            False,
            description="Keep every rule's `CAN_FIRE_` and `WILL_FIRE_` signals in the generated "
            "code, so waveforms show rule firings (`-keep-fires`).",
        )
        keep_inlined_boundaries: bool = Field(
            False,
            description="Keep the boundaries of inlined registers and wires as signals "
            "(`-keep-inlined-boundaries`).",
        )
        keep_method_conds: bool = Field(
            False,
            description="Keep the predicates of method calls inside rules as signals "
            "(`-keep-method-conds`).",
        )
        readable_mux: bool = Field(
            True,
            description="Generate multiplexers as readable `case` statements (`-readable-mux`). "
            "bsc 2026.07.1 fails with an internal error on some designs without it.",
        )
        remove_dollar: bool = Field(
            True,
            description="Use `_` rather than `$` between instance and port names in generated "
            "Verilog identifiers (`-remove-dollar`). Verilog output only.",
        )
        reset_prefix: str | None = Field(
            None,
            description="Name of the reset port of generated modules (`-reset-prefix`), e.g. "
            '"RST_P". bsc names it "RST_N" by default.',
        )
        positive_reset: bool = Field(
            False,
            description="Make resets active-high: define `BSV_POSITIVE_RESET` for the Bluespec "
            "preprocessor and for the Verilog code (bsc's Verilog library and generated modules "
            "take their reset polarity from it). Bluesim applies reset itself and does not "
            "depend on it. Consider naming the port with `reset_prefix`.",
        )
        v95: bool = Field(
            False,
            description="Generate strict Verilog-95 (`-v95`), without the Verilog-2001 features "
            "bsc uses otherwise, such as named parameter passing and `$signed`.",
        )
        use_dpi: bool = Field(
            False,
            description="Implement imported C functions with the SystemVerilog DPI rather than "
            "VPI in generated Verilog (`-use-dpi`). Required to simulate them with Verilator.",
        )
        system_verilog_tasks: bool = Field(
            False,
            description="Keep SystemVerilog system tasks such as `$fatal` and `$error` in the "
            "generated Verilog (`-system-verilog-tasks`). Without it bsc lowers `$fatal` to "
            "`$display` and `$finish(1)`, which exits a Verilog simulation successfully.",
        )
        verilog_filters: list[str] = Field(
            [],
            description="Commands bsc runs on each Verilog file it generates, in order, with the "
            "file's name as their only argument (`-verilog-filter`).",
        )
        elab: bool = Field(
            False,
            description="Also write the elaborated and scheduled modules (`.ba` files, `-elab`), "
            "which Bluetcl needs to inspect a compiled design.",
        )
        # Diagnostics
        show_schedule: bool = Field(
            False,
            description="Write each generated module's schedule to a `.sched` file in "
            "`info_dir` (`-show-schedule`).",
        )
        sched_dot: bool = Field(
            False,
            description="Write each generated module's scheduling graphs as `.dot` files in "
            "`info_dir` (`-sched-dot`).",
        )
        show_rule_rel: list[tuple[str, str]] = Field(
            [],
            description="Pairs of rules whose scheduling relationship bsc reports "
            '(`-show-rule-rel r1 r2`); ["*", "*"] reports every pair.',
        )
        show_method_conf: bool = Field(
            False,
            description="Document the conflicts between the methods of each generated module in "
            "its Verilog (`-show-method-conf`).",
        )
        show_method_bvi: bool = Field(
            False,
            description="Document each generated module's method schedule in its Verilog, in the "
            'form an `import "BVI"` of it takes (`-show-method-bvi`).',
        )
        show_range_conflict: bool = Field(
            True,
            description="Include the conditions of the method calls when reporting a parallel "
            "composability error, G0004 (`-show-range-conflict`).",
        )
        show_stats: bool = Field(
            False,
            description="Report package statistics after each compiler stage (`-show-stats`).",
        )
        show_elab_progress: bool = Field(
            False,
            description="Trace the elaboration of modules, rules and methods, to locate where a "
            "compilation hangs (`-show-elab-progress`).",
        )
        show_version: bool = Field(
            True,
            description="Record the compiler version in the generated files (`-show-version`).",
        )
        show_timestamps: bool = Field(
            False,
            description="Record a timestamp in the generated files (`-show-timestamps`). Off, so "
            "that unchanged inputs regenerate identical files.",
        )
        # Messages
        warn_method_urgency: bool = Field(
            True,
            description="Warn when the urgency between a method and a rule is chosen "
            "arbitrarily (`-warn-method-urgency`).",
        )
        warn_action_shadowing: bool = Field(
            True,
            description="Warn when a rule's action is overwritten by a later rule in the same "
            "cycle (`-warn-action-shadowing`).",
        )
        warn_undetermined_predicate: bool = Field(
            True,
            description="Warn when a rule or method predicate has an undetermined value "
            "(`-warn-undet-predicate`).",
        )
        promote_warnings: list[str] = Field(
            [],
            description="Warnings reported as errors (`-promote-warnings`), by tag, or "
            '"ALL"/"NONE". Candidates are the scheduling warnings that can mean a design will '
            'not behave as written: ["G0009", "G0010", "G0117"] for a conflict added to break a '
            "cycle, a rule arbitrarily made more urgent than another, and an action shadowed by "
            "a later rule. None is promoted by default: working designs raise them by design "
            "(bluespec/Piccolo: 26 G0010 and 85 G0117), and promoting one changes no hardware, "
            "it only rejects those designs.",
        )
        suppress_warnings: list[str] = Field(
            [],
            description='Warnings not reported (`-suppress-warnings`), by tag, e.g. ["G0021"], '
            'or "ALL"/"NONE".',
        )
        demote_errors: list[str] = Field(
            [],
            description="Errors reported as warnings (`-demote-errors`), by tag, or "
            '"ALL"/"NONE". Not every error can be demoted.',
        )
        continue_after_errors: bool = Field(
            False,
            description="Continue compiling other modules and stages after an error, to report "
            "more errors at once (`-continue-after-errors`).",
        )
        # Limits and runtime
        steps_warn_interval: int = Field(
            2_000_000,
            gt=0,
            description="Warn each time elaboration has unfolded this many more function calls "
            "(`-steps-warn-interval`; bsc's own default is 100000).",
            json_schema_extra=count(),
        )
        steps_max_intervals: int = Field(
            6_000_000,
            gt=0,
            description="Give up elaborating after this many `steps_warn_interval` warnings "
            "(`-steps-max-intervals`; bsc's own default is 10). The default only stops runaway "
            "elaborations, since large designs legitimately take many steps.",
            json_schema_extra=count(),
        )
        haskell_runtime_flags: list[str] = Field(
            [],
            description="Flags for the Haskell run-time system bsc runs on, passed as "
            '`+RTS ... -RTS`: e.g. "-H1G" to suggest a 1 GB heap, "-K64M" to limit the stack.',
        )
        extra_flags: list[str] = Field(
            [],
            description="Further bsc flags for every compilation, added after the ones these "
            "settings produce, so they take precedence (see `bsc -help` and `bsc -help-hidden`).",
        )

        @field_validator("reset_prefix", mode="after")
        @classmethod
        def _validate_reset_prefix(cls, value: str | None) -> str | None:
            if value is not None and not _VERILOG_IDENTIFIER.match(value):
                raise ValueError(f"{value!r} is not a legal Verilog port name")
            return value

        @field_validator("promote_warnings", "suppress_warnings", "demote_errors", mode="after")
        @classmethod
        def _validate_message_tags(cls, value: list[str]) -> list[str]:
            bad = [tag for tag in value if not _MESSAGE_TAG.match(tag)]
            if bad:
                raise ValueError(
                    f"not bsc message tags: {', '.join(map(repr, bad))} (expected tags such as "
                    '"G0010", or "ALL" or "NONE")'
                )
            return value

        @field_validator("extra_optimize_flags", "haskell_runtime_flags", mode="after")
        @classmethod
        def _validate_flags(cls, value: list[str]) -> list[str]:
            bad = [flag for flag in value if not flag.startswith("-") or flag == "-RTS"]
            if bad:
                raise ValueError(f"not flags: {', '.join(map(repr, bad))}")
            return value

    @cached_property
    def bsc(self) -> BscTool:
        """The bsc tool; constructing it checks bsc's version against `MIN_BSC_VERSION`."""
        return BscTool()

    def _sources_of(self, *types: SourceType, tb: bool) -> list[DesignSource]:
        """Return the sources of `types` that this flow reads.

        The RTL sources come first, then the testbench's if `tb` is true.
        """
        return [src for src in self.sources_read(rtl=True, tb=tb) if src.type in types]

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design that defines `BSV_POSITIVE_RESET` while `positive_reset` is on.

        bsc defines the macro itself then, and a design's own definition would be overridden
        without a word."""
        super().check_design_supported(design, settings)
        if settings is None or not getattr(settings, "positive_reset", False):
            return
        for part in cls.macro_parts:
            section = getattr(design, part)
            if BSV_POSITIVE_RESET in section.defines or BSV_POSITIVE_RESET in section.parameters:
                raise FlowSettingsException(
                    f"the design defines `{BSV_POSITIVE_RESET}`, which bsc defines itself when "
                    "`positive_reset` is true: remove the define, or set `positive_reset: false`"
                )

    def _bsc_sources(self, *, tb: bool) -> list[DesignSource]:
        """Return the design's Bluespec sources, RTL first, after checking their suffixes."""
        sources = self._sources_of(SourceType.Bluespec, tb=tb)
        bad = [str(src.file) for src in sources if src.file.suffix not in _BSC_SOURCE_SUFFIXES]
        if bad:
            raise FlowSettingsException(
                "bsc compiles BSV from `.bsv` files and BH (Bluespec Classic) from `.bs` files "
                f"only; rename: {', '.join(bad)}"
            )
        return sources

    def _macros(self, *, tb: bool) -> dict[str, Any]:
        """Return the preprocessor macros.

        The macros are the design's `defines` and `parameters`, and `BSV_POSITIVE_RESET`. The
        testbench's macros go over the RTL's. The mapping is a copy, so the flow never changes the
        design.
        """
        assert isinstance(self.settings, BscFlow.Settings)
        macros: dict[str, Any] = {**self.design.rtl.defines, **self.design.rtl.parameters}
        if tb:
            macros.update({**self.design.tb.defines, **self.design.tb.parameters})
        if self.settings.positive_reset:
            macros[BSV_POSITIVE_RESET] = None
        return macros

    def _check_macros(self, sources: list[DesignSource], macros: dict[str, Any]) -> None:
        """Warn when the design's macros reach none of its sources: all are BH and `cpp` is off.

        bsc's own preprocessor reads BSV only, so BH sources see macros only through the C
        preprocessor (`cpp`). A design that mixes BSV and BH may pass its macros to BH through a
        BSV package, so it gets no warning.
        """
        assert isinstance(self.settings, BscFlow.Settings)
        given = [name for name in macros if name != BSV_POSITIVE_RESET]
        bh = [src.file.name for src in sources if src.file.suffix == ".bs"]
        if given and bh and len(bh) == len(sources) and not self.settings.cpp:
            log.warning(
                "The design's macros (%s) reach none of its sources, all of them BH (%s): bsc "
                "preprocesses BSV only. Set the flow setting `cpp: true` to define them for the C "
                "preprocessor, which BH sources then go through (`#ifdef`).",
                ", ".join(given),
                ", ".join(bh),
            )

    def _path_flags(self, backend: str, out_dir: Path, sources: list[DesignSource]) -> list[str]:
        """Return where bsc reads and writes.

        The flags are `-bdir`, `-info-dir`, the output directory, `-p`, `-fdir` and, for Verilog,
        `-vsearch`. The paths are absolute, since bsc runs in the run directory.
        """
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        bdir = self.run_file(ss.bobj_dir)
        info_dir = self.run_file(ss.info_dir) if ss.info_dir else bdir
        flags = ["-bdir", str(bdir), "-info-dir", str(info_dir)]
        flags += ["-simdir" if backend == "sim" else "-vdir", str(self.run_file(out_dir))]
        package_dirs = [src.file.parent for src in sources]
        package_dirs += [self.normalize_path_to_design_root(p) for p in ss.search_paths]
        flags += ["-p", _bsc_path(package_dirs, "search_paths")]
        if backend == "sim":
            # Bluesim reads no Verilog (it rejects imported Verilog modules). Without `-vdir`,
            # bsc warns (S0073) when a `-vsearch` directory is also on `-p`. Its default Verilog
            # path includes the directory of a Bluespec source that has Verilog beside it.
            return flags + self._fdir_flags()
        verilog_dirs: list[str | Path] = [
            self.normalize_path_to_design_root(p) for p in ss.verilog_search_paths
        ]
        verilog_dirs += [src.file.parent for src in self._sources_of(*_VERILOG_TYPES, tb=True)]
        flags += ["-vsearch", _bsc_path(self._vendor_verilog_dirs() + verilog_dirs, "vsearch")]
        return flags + self._fdir_flags()

    def _fdir_flags(self) -> list[str]:
        """Return `-fdir`, where relative file names in the design resolve during elaboration."""
        assert isinstance(self.settings, BscFlow.Settings)
        if not self.settings.fdir:
            return []
        return ["-fdir", str(self.normalize_path_to_design_root(self.settings.fdir))]

    def _vendor_verilog_dirs(self) -> list[str | Path]:
        """Return the directories of bsc's vendor-specific Verilog primitives, searched first."""
        return []

    def _runtime_flags(self) -> list[str]:
        """Return `haskell_runtime_flags` for bsc's own runtime, which reads them between `+RTS`
        and `-RTS`."""
        assert isinstance(self.settings, BscFlow.Settings)
        rts = self.settings.haskell_runtime_flags
        return ["+RTS", *rts, "-RTS"] if rts else []

    def _verbosity_flags(self) -> list[str]:
        """Return `-verbose` for a verbose run and `-quiet` for a quiet one."""
        if self.settings.verbose:
            return ["-verbose"]
        if self.settings.is_quiet:
            return ["-quiet"]
        return []

    def _compile_flags(self, backend: str) -> list[str]:
        """Return every flag that the settings give a compilation for `backend`.

        The backend is "verilog" or "sim".
        """
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        flags = self._verbosity_flags()
        # `-split-if` also switches `-aggressive-conditions`, so the latter comes after it.
        flags += [_toggle("split-if", ss.split_if), _toggle("lift", ss.lift)]
        flags += [
            _toggle("aggressive-conditions", ss.aggressive_conditions),
            _toggle("sched-conditions", ss.sched_conditions),
            _toggle("let-gen", ss.let_gen),
            f"-resource-{ss.resource_scheduling}",
            f"-sat-{ss.sat_solver}",
            _toggle("check-assert", ss.check_assert),
            _toggle("opt-undetermined-vals", ss.opt_undetermined_vals),
            "-unspecified-to",
            ss.unspecified_to,
        ]
        # `-synthesize` also switches `-opt-bit-const`, so it comes before the optimization flags.
        flags.append(_toggle("synthesize", ss.synthesize_to_boolean))
        if ss.optimize:
            flags.append("-O")
        flags += ss.extra_optimize_flags
        flags += [
            _toggle("remove-false-rules", ss.remove_false_rules),
            _toggle("remove-empty-rules", ss.remove_empty_rules),
            _toggle("remove-starved-rules", ss.remove_starved_rules),
            _toggle("remove-unused-modules", ss.remove_unused_modules),
            _toggle("keep-fires", ss.keep_fires),
            _toggle("keep-inlined-boundaries", ss.keep_inlined_boundaries),
            _toggle("keep-method-conds", ss.keep_method_conds),
            _toggle("readable-mux", ss.readable_mux),
            _toggle("v95", ss.v95),
            _toggle("use-dpi", ss.use_dpi),
            _toggle("system-verilog-tasks", ss.system_verilog_tasks),
            _toggle("elab", ss.elab),
        ]
        if backend == "verilog":
            # bsc rejects `-remove-dollar` for Bluesim and for every link.
            flags.append(_toggle("remove-dollar", ss.remove_dollar))
        if ss.reset_prefix:
            flags += ["-reset-prefix", ss.reset_prefix]
        for command in ss.verilog_filters:
            flags += ["-verilog-filter", command]
        flags += [
            _toggle("show-schedule", ss.show_schedule),
            _toggle("sched-dot", ss.sched_dot),
            _toggle("show-method-conf", ss.show_method_conf),
            _toggle("show-method-bvi", ss.show_method_bvi),
            _toggle("show-range-conflict", ss.show_range_conflict),
            _toggle("show-stats", ss.show_stats),
            _toggle("show-elab-progress", ss.show_elab_progress),
            _toggle("show-version", ss.show_version),
            _toggle("show-timestamps", ss.show_timestamps),
            "-show-module-use",
            _toggle("warn-method-urgency", ss.warn_method_urgency),
            _toggle("warn-action-shadowing", ss.warn_action_shadowing),
            _toggle("warn-undet-predicate", ss.warn_undetermined_predicate),
            _toggle("continue-after-errors", ss.continue_after_errors),
        ]
        for r1, r2 in ss.show_rule_rel:
            flags += ["-show-rule-rel", r1, r2]
        # An empty list would be taken as the flag's argument by the next flag.
        if ss.promote_warnings:
            flags += ["-promote-warnings", _message_list(ss.promote_warnings)]
        if ss.suppress_warnings:
            flags += ["-suppress-warnings", _message_list(ss.suppress_warnings)]
        if ss.demote_errors:
            flags += ["-demote-errors", _message_list(ss.demote_errors)]
        flags += [
            "-steps-warn-interval",
            str(ss.steps_warn_interval),
            "-steps-max-intervals",
            str(ss.steps_max_intervals),
        ]
        if ss.cpp:
            flags.append("-cpp")
        for arg in ss.cpp_flags:
            flags += ["-Xcpp", arg]
        return flags

    @classmethod
    def backend_of(cls, settings: "BscFlow.Settings") -> str:
        """Return the code generation backend of bsc for these settings: `verilog` or `sim`."""
        return "verilog"

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse the combinations that bsc refuses, before anything runs, and name the settings."""
        super().check_settings_supported(settings)
        assert isinstance(settings, BscFlow.Settings)
        if settings.unspecified_to in ("X", "Z") and not settings.opt_undetermined_vals:
            raise FlowSettingsException(
                f"{cls.name}: `unspecified_to: {settings.unspecified_to}` requires "
                '`opt_undetermined_vals: true`; otherwise use "0", "1" or "A"'
            )
        if cls.backend_of(settings) == "sim" and settings.unspecified_to in ("X", "Z"):
            raise FlowSettingsException(
                f'{cls.name}: Bluesim is two-valued and cannot tie don\'t-cares to "X" or "Z"; '
                'set unspecified_to to "0", "1" or "A"'
            )

    def _warn_about_environment(self) -> None:
        """Warn that `BSC_OPTIONS` makes the run depend on more than the settings."""
        if "BSC_OPTIONS" in os.environ:
            log.warning(
                "BSC_OPTIONS is set (%r): bsc prepends it to every command line, so the run "
                "does not depend on its settings alone",
                os.environ["BSC_OPTIONS"],
            )

    def _prepare_dirs(self, out_dir: Path) -> None:
        """Create the directories bsc writes to, and remove what an earlier run left in them: the
        Verilog modules it generated always, and its packages with `cleanup_bobjs`. bsc writes
        a module's `.use` file (`-show-module-use`) right after its `.v`, so each `.use` goes
        with the `.v` beside it, whatever a `verilog_filters` command made of that `.v`. bsc
        writes every module again, also from a package it finds up to date, so each generated
        module that the run records is one that this run wrote. Other files stay, and the library
        modules copied in are copied again."""
        assert isinstance(self.settings, BscFlow.Settings)
        if self.settings.verilog_filters and any(c.isspace() for c in str(out_dir)):
            raise FlowSettingsException(
                "bsc runs each `verilog_filters` command on a generated file through a shell, "
                f"without quoting, so the output directory cannot contain whitespace: {out_dir}"
            )
        bdir = self.run_file(self.settings.bobj_dir)
        stale = []
        if self.settings.cleanup_bobjs:
            stale += [obj for pattern in ("*.bo", "*.ba") for obj in bdir.glob(pattern)]
        for use_file in out_dir.glob("*.use"):
            stale += [use_file, use_file.with_suffix(".v")]
        # These are an earlier run's packages and generated modules, all in the run directory.
        self.run_directory.remove(*(path for path in stale if path.is_file()))
        bdir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.info_dir:
            self.run_file(self.settings.info_dir).mkdir(parents=True, exist_ok=True)

    def _compile(
        self,
        backend: str,
        sources: list[DesignSource],
        top: str,
        top_file: Path,
        flags: list[str],
    ) -> None:
        """Compile every Bluespec source in the order given, then generate code for `top` from
        `top_file`. Each compile generates code (`-verilog` or `-sim`) for the `synthesize`d
        modules of its package and, with `-u`, compiles the packages it imports first."""
        backend_flag = f"-{backend}"
        for src in sources:
            if src.file != top_file:
                self._run_bsc(COMPILE_LOG, *flags, "-u", backend_flag, src.file)
        self._run_bsc(COMPILE_LOG, *flags, "-u", backend_flag, "-g", top, top_file)

    def _run_bsc(self, log_name: str, *args: Any, env: dict[str, str] | None = None) -> None:
        """Run bsc with `args` (and `env`), and copy its output to the log `log_name` in the run
        directory.

        bsc compiles and links through the C++ compiler and `make`, whose errors are far from the
        end of the output. A failure of bsc therefore quotes the first error lines of the log
        (`diagnostic_lines`), as the failure of the tool alone does not say what went wrong. A
        timeout keeps its own words."""
        log_file = self.run_directory.writable(log_name)
        try:
            self.bsc.run(*args, env=env, tee=log_file, merge_stderr=True)
        except NonZeroExitCode as error:
            if type(error) is not NonZeroExitCode:
                raise
            report = self.report_file(log_file, optional=True)
            found = (
                diagnostic_lines(report.read_text(encoding="utf-8", errors="replace"))
                if report is not None
                else []
            )
            if not found:
                raise
            shown = found[:DIAGNOSTICS_SHOWN]
            more = (
                f" (and {len(found) - len(shown)} more in {log_name})"
                if len(found) > len(shown)
                else ""
            )
            raise error.with_message(f"bsc reported: {' | '.join(shown)}{more}") from error

    def _top_file(self, sources: list[DesignSource], what: str) -> Path:
        """Return the file that holds the top module: the last Bluespec source."""
        if not sources:
            raise FlowSettingsException(f"{self.name}: the design has no Bluespec {what} sources")
        return sources[-1].file


class Bsc(BscFlow):
    """Compile a Bluespec (BSV or BH) design to Verilog with the Bluespec compiler, bsc.

    The flow compiles `rtl.sources` in order, with the package that defines `rtl.top` last. It
    generates Verilog for `rtl.top` and for every `synthesize`d module on the way.
    The output `design` lists the Verilog files that the top module needs: the generated modules,
    the design's own Verilog sources, and the modules of bsc's Verilog library that it
    instantiates. The flow copies the library modules into `verilog_out_dir`, so a downstream
    synthesis or simulation flow can take the list as it is. If the generated Verilog
    instantiates a module that no Verilog file defines (a vendor primitive, for example), the flow
    leaves the module out and warns: the downstream tool must provide it. `rtl.defines` and
    `rtl.parameters` are passed to bsc as preprocessor macros. BH (`.bs`) sources see them only
    through the C preprocessor, with `cpp`.

    The flow converts the design for the flows that read Verilog: its output `design` is the
    design with the generated Verilog in place of the Bluespec sources. Every other source of
    `rtl` passes through, once and in the order of the design, and the rest of the design is
    carried unchanged.
    """

    results_description = describe_results(
        modules=MetricSpec(
            "Verilog modules of the design's hierarchy, the top first and each module before "
            "those it instantiates: the ones bsc generated, the library modules and the "
            "design's own Verilog modules.",
            "text_list",
        ),
    )

    output_dir_setting = "verilog_out_dir"

    class Outputs(Flow.Outputs):
        design: DerivedSources | None = Out(
            DESIGN,
            description="The design with the Verilog of the Bluespec sources in place of them: "
            "the files that define the reset macro and the copies from bsc's library, then every "
            "source of `rtl` that is not Bluespec as the design lists it, then the modules bsc "
            "generated, the top module last. The rest of the design is carried.",
        )

    @classmethod
    def design_conversion(cls, settings: Flow.Settings) -> DesignConversion | None:
        """Convert the Bluespec sources into Verilog."""
        return DesignConversion(frozenset({SourceType.Bluespec}), frozenset({SourceType.Verilog}))

    class Settings(BscFlow.Settings):
        verilog_out_dir: Path = Field(
            Path("gen_rtl"),
            description="Directory for the generated Verilog and the library modules it uses, "
            "relative to the run directory. It is the flow's: bsc overwrites the modules it "
            "generates, the library modules are copied over, and an earlier run's generated modules "
            "are removed before bsc compiles.",
            json_schema_extra=WORKING,
        )
        verilog_primitives: Literal["generic", "vivado", "quartus"] = Field(
            "generic",
            description="Which versions of bsc's Verilog library modules to use: the generic "
            'ones, or those tuned for "vivado" or "quartus" synthesis (block RAMs and a few '
            "others, from `$BLUESPECDIR/Verilog.Vivado` or `Verilog.Quartus`).",
        )
        positive_reset: bool = Field(
            True,
            description="Make resets active-high: define `BSV_POSITIVE_RESET` for the Bluespec "
            "preprocessor, and at the top of every Verilog file of the derived design that bsc "
            "generated or copied from its library (they take their reset polarity from it). "
            "The design's own Verilog sources are passed on unchanged, preceded by "
            f"`{BSV_DEFINES_FILE}`, the first file, which defines the macro for those "
            "that read it. "
            "Consider naming the port with `reset_prefix`.",
        )
        # synthesis-oriented defaults
        unspecified_to: Literal["X", "0", "1", "Z", "A"] = Field(
            "X",
            description="Value the don't-care bits left in the generated code are tied to: "
            '"X" or "Z" (as in Verilog), "0", "1", or "A" for alternating ones and zeros '
            '(`-unspecified-to`). "X", the default, gives synthesis the most freedom; "X" and '
            '"Z" require `opt_undetermined_vals`.',
        )
        opt_undetermined_vals: bool = Field(
            True,
            description="Let the Verilog backend choose don't-care values from their context "
            "(`-opt-undetermined-vals`), for better hardware.",
        )
        remove_starved_rules: bool = Field(
            True,
            description="Remove rules the generated schedule never fires "
            "(`-remove-starved-rules`). bsc warns about each; they usually point at a scheduling "
            "problem.",
        )
        remove_unused_modules: bool = Field(
            True,
            description="Remove submodules not connected to any output from the generated "
            "Verilog (`-remove-unused-modules`), as synthesis would.",
        )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Require Bluespec RTL and a top module, because bsc generates code for it."""
        super().check_design_supported(design, settings)
        if not any(source.type is SourceType.Bluespec for source in design.rtl.sources):
            raise FlowSettingsException("bsc needs Bluespec RTL sources to convert")
        if not design.rtl.top:
            raise FlowSettingsException("bsc needs the design's top module: set `rtl.top`")

    def init(self) -> None:
        """Reject what bsc would reject, and a bsc that is too old, before anything runs.

        Planning already checks the settings and the design. This method checks them for a flow
        built directly.
        """
        assert isinstance(self.settings, self.Settings)
        self.check_design_supported(self.design, self.settings)
        self.check_settings_supported(self.settings)
        self._warn_about_environment()
        _ = self.bsc  # constructing it checks its version: an old bsc fails before anything runs

    def _vendor_verilog_dirs(self) -> list[str | Path]:
        """Return the `verilog_primitives` directory of bsc's library (`%` stands for bsc's
        installation), which goes ahead of the generic modules on the Verilog search path."""
        assert isinstance(self.settings, self.Settings)
        vendor = self.settings.verilog_primitives
        if vendor == "generic":
            return []
        return ["%/Verilog." + {"vivado": "Vivado", "quartus": "Quartus"}[vendor]]

    def _verilog_path(self, path_flags: list[str]) -> list[Path]:
        """Return the directories that bsc searches for Verilog modules, in its order.

        This is the value that `-print-flags-raw` reports for the same path flags, with `%` and
        `+` expanded.
        """
        out = self.bsc.run_get_stdout(*path_flags, "-print-flags-raw") or ""
        match = re.search(r"^\s*vPath\s*=\s*(\[.*\]),?\s*$", out, re.MULTILINE)
        if not match:
            log.warning("could not read bsc's Verilog search path from `-print-flags-raw`")
            return []
        return [Path(p) for p in _parse_haskell_strings(match.group(1))]

    def run(self) -> None:
        """Compile the Bluespec sources and collect the Verilog files of the top module."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        top = self.design.rtl.top
        assert top
        sources = self._bsc_sources(tb=False)
        top_file = self._top_file(sources, "RTL")
        vout_dir = self.run_file(ss.verilog_out_dir)
        log.info("Verilog output directory: %s", vout_dir)
        self._prepare_dirs(vout_dir)

        path_flags = self._path_flags("verilog", vout_dir, sources)
        macros = self._macros(tb=False)
        self._check_macros(sources, macros)
        flags = [
            *self._runtime_flags(),
            *path_flags,
            *self._compile_flags("verilog"),
            *_macro_flags(macros, "design macros", cpp=ss.cpp),
            *ss.extra_flags,
        ]
        self._compile("verilog", sources, top, top_file, flags)

        # Read the path with the compile's own path flags, because `extra_flags` may add a
        # `-vsearch`.
        verilog_path = self._verilog_path([*path_flags, *ss.extra_flags])
        collected = self._collect_verilog(top, top_file, vout_dir, verilog_path)
        self.results["modules"] = collected.modules
        assert isinstance(self.outputs, self.Outputs)
        self.outputs.design = self._derived_sources(top, collected)

    def _derived_sources(self, top: str, collected: _Collected) -> DerivedSources:
        """Return the sources of the derived design from what the collection found.

        The defining file and the library copies come first (`Generated`). Then every source of
        `rtl` that is not Bluespec (`Passed`: the design's Verilog and SystemVerilog, VHDL,
        headers, constraints, ...), in the order of the design. The modules bsc generated come
        last, each before the module that instantiates it and the top module at the end
        (`Generated`). Each source appears once."""
        entries: list[Generated | Passed] = [
            Generated(path, SourceType.Verilog) for path in collected.defines + collected.library
        ]
        entries.extend(
            Passed(index)
            for index, src in enumerate(self.design.rtl.sources)
            if src.type is not SourceType.Bluespec
        )
        entries.extend(Generated(path, SourceType.Verilog) for path in collected.generated)
        return DerivedSources(top, tuple(unique(entries)))

    def _collect_verilog(
        self, top: str, top_file: Path, vout_dir: Path, verilog_path: list[Path]
    ) -> _Collected:
        """Return the modules under `top` and the Verilog files that define them.

        A module is one bsc generated when its `.use` file (written by `-show-module-use`) and its
        `.v` are in `vout_dir`; its submodules are the lines of the `.use` file. Every other module
        is defined by one of the design's Verilog sources, or is found as `<module>.v` in bsc's
        Verilog search path (never in `vout_dir`, which may hold copies from an earlier run) and
        copied into `vout_dir`. A module a `.use` file names that is found nowhere is left to the
        downstream tool, such as a vendor primitive, with a warning; in the design's or the
        library's own Verilog, only instances of modules found are followed, since what looks
        like one may not be. A module both generated and defined by a design source is an error:
        the file set would define it twice.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        design_verilog = self._sources_of(*_VERILOG_TYPES, tb=False)
        defined_in: dict[str, Path] = {}
        for src in design_verilog:
            for module in _verilog_modules(src.file):
                defined_in.setdefault(module, src.file)
        # A relative entry of the search path is relative to the run directory, where bsc runs.
        library_dirs = [d for d in map(self.run_file, verilog_path) if d != vout_dir]

        modules: list[str] = []  # pre-order, the top first
        generated: list[Path] = []  # post-order, the top last
        library: list[Path] = []
        unresolved: list[str] = []

        def in_library(module: str) -> Path | None:
            return next(
                (d / f"{module}.v" for d in library_dirs if (d / f"{module}.v").is_file()), None
            )

        def bsc_generated(module: str) -> bool:
            return (vout_dir / f"{module}.use").is_file() and (vout_dir / f"{module}.v").is_file()

        def visit_instances(path: Path, module: str) -> None:
            """Visit the submodules of a library or design module, which no `.use` file lists.

            For example, bsc's `MakeResetA` instantiates `SyncResetA`, and a Verilog wrapper that
            the design imports may instantiate a module that bsc generated. Only names that
            resolve to a module count, so a word that looks like an instance but is not one is
            never reported missing.
            """
            for name in _verilog_instances(path, module):
                if name != module and (
                    name in defined_in or bsc_generated(name) or in_library(name)
                ):
                    visit(name)

        def visit(module: str) -> None:
            """Record `module` and then its submodules.

            A module is generated by bsc (its `.use` file lists its submodules), defined by a
            design source, copied from the library, or unresolved. The file of a generated module
            goes after the files of its submodules.
            """
            if module in modules or module in unresolved:
                return
            use_file = vout_dir / f"{module}.use"
            verilog = vout_dir / f"{module}.v"
            if bsc_generated(module):
                if module in defined_in:
                    raise FlowSettingsException(
                        f"{module} is both generated by bsc ({verilog}) and defined by the "
                        f"design's Verilog source {defined_in[module]}"
                    )
                modules.append(module)
                for line in use_file.read_text().splitlines():
                    if line.strip():
                        visit(line.strip())
                generated.append(verilog)
                return
            if module in defined_in:
                modules.append(module)
                visit_instances(defined_in[module], module)
                return
            found = in_library(module)
            if found is None:
                unresolved.append(module)
                return
            modules.append(module)
            copy = self.run_directory.writable(vout_dir / f"{module}.v")
            log.debug("copying %s to %s", found, copy)
            replacing_copy(found, copy)  # a link at the name is replaced, not followed
            visit_instances(copy, module)
            library.append(copy)

        visit(top)
        if top not in modules:
            raise FlowSettingsException(
                f"bsc generated no Verilog for {top} in {vout_dir}: `rtl.top` must be a module "
                f"of the last Bluespec source, {top_file.name}"
            )
        if unresolved:
            log.warning(
                "No Verilog file defines %s (searched the design's Verilog sources and %s); the "
                "tool that reads the generated Verilog must provide %s, e.g. as vendor "
                "primitives.",
                ", ".join(unresolved),
                ", ".join(map(str, library_dirs)) or "no directories",
                "it" if len(unresolved) == 1 else "them",
            )
        defines: list[Path] = []
        if ss.positive_reset:
            for path in library + generated:
                _prepend_define(path, "BSV_POSITIVE_RESET")
            # The design's own Verilog sources stay as they are. The first file defines the macro
            # for them too, wherever they come in a compilation unit.
            defines_file = self.run_directory.writable(vout_dir / BSV_DEFINES_FILE)
            with replacing_file(defines_file) as f:
                f.write(
                    "`define BSV_POSITIVE_RESET\n"
                    "// Written by Xeda's bsc flow, to define it before the design's own Verilog.\n"
                )
            defines.append(defines_file)
        return _Collected(
            modules, defines, library, [src.file for src in design_verilog], generated
        )


def _check_link_paths(args: list[str | Path]) -> None:
    """Reject the paths that bsc's link step cannot take.

    bsc runs the C++ compiler and the Verilog simulator through a shell, unquoted, so a path with
    whitespace falls apart there. bsc's own compilation copes with such a path.
    """
    paths = [part for arg in map(str, args) for part in arg.split(_PATH_SEPARATOR)]
    spaced = unique([p for p in paths if not p.startswith("-") and any(c.isspace() for c in p)])
    if spaced:
        raise FlowSettingsException(
            "bsc's link step runs its tools through a shell without quoting, so it cannot take a "
            f"path with whitespace: {', '.join(spaced)}. Use a run root (`--run-root`) "
            "and design location without whitespace."
        )


def _prepend_define(path: Path, macro: str) -> None:
    """Define `macro` at the top of a Verilog file, once.

    The function works on bytes, because the file of a library or imported module need not be
    UTF-8.
    """
    line = f"`define {macro}\n".encode()
    content = path.read_bytes()
    if not content.startswith(line):
        with replacing_file(path, "wb") as f:  # a link at the name is replaced, not followed
            f.write(line + content)


class BscSim(BscFlow, SimFlow):
    """Simulate a Bluespec testbench compiled by bsc, with Bluesim or a Verilog simulator.

    The flow compiles the design's Bluespec sources in order, RTL first and then the testbench,
    with the package that defines `tb.top` last. It simulates `tb.top`, a module with an `Empty`
    interface (`rtl.top` when the design has no testbench). `simulator` selects Bluesim, the
    cycle-based simulator of bsc, or a Verilog simulator that bsc links with the generated
    Verilog (`bsc -vsim`). The clock and reset come from the `main.v` of bsc.

    Bluesim records the system-task calls in the generated code, and a script of Xeda measures
    the clock cycles and the time. Verilator and Icarus record the system tasks that the linked
    runtime observes. For these three simulators, a run passes only if the testbench finishes
    explicitly (or Bluesim reaches a confirmed `max_cycles` limit) and no event reaches
    `fail_severity`. A fatal error or a nonzero exit status always fails the run. `$finish(n)`
    uses `n` as a verbosity level.

    The design's `defines` and `parameters` are preprocessor macros, and the testbench's go over
    the RTL's. BH (`.bs`) sources see them only through the C preprocessor, with `cpp`.
    The supported simulators are Bluesim, Verilator, Icarus, ModelSim, Questa, VCS, vcsi and
    xsim. The legacy simulators cvc, cver, isim, ncverilog and veriwell are rejected before
    compilation. The Linux CI job checks the real Icarus evidence and the registration of the
    built-in tasks.
    """

    executables = BscFlow.executables + (
        "vsim",
        "vvp",
        "xsim",
        "verilator",
        "iverilog",
        "c++",
        "cc",
        "sh",
        "make",
    )

    artifacts_description = describe_artifacts(
        "log",
        "simulation_log",
        "waveform",
        **program_logs("c++", "cc", when="when the simulator needs the compiler"),
        executable=ArtifactSpec(
            "The simulation program that bsc links.", "when the compilation and link succeed"
        ),
    )

    results_description = describe_results(
        "sim.evidence",
        "sim.ended_by",
        "sim.time",
        "sim.errors",
        "sim.warnings",
        simulator=MetricSpec("The simulator that ran the testbench.", "text"),
    )

    output_dir_setting = "sim_dir"

    cocotb_advice = (
        "bsc_sim runs Bluespec testbenches; for a cocotb testbench, simulate the Verilog that "
        "the `bsc` flow generates with one of these simulators"
    )

    # The link step also compiles in the C and C++ code a testbench imports.
    #: The testbench's macros go over the RTL's (`_macros`).
    macro_parts = ("rtl", "tb")

    reads_sources = frozenset({SourceType.Bluespec, *_VERILOG_TYPES, *_FOREIGN_TYPES})

    @classmethod
    def check_run_directory(cls, settings: Flow.Settings, run_path: Path) -> None:
        """Refuse a run directory whose path has whitespace.

        bsc's link step runs its tools through a shell without quoting, whatever the simulator,
        and every path it is given lies in the run directory. One rule with one message therefore
        serves every backend. The method judges the directory as given and as it resolves,
        because the makefile of the Verilator backend builds in the physical directory.
        `_check_link_paths` stays as the backstop for the design's own paths.
        """
        super().check_run_directory(settings, run_path)
        _check_link_paths([run_path, os.path.realpath(run_path)])

    def has_evidence_adapter(self) -> bool:
        """Return whether the simulator has an evidence adapter for the current run.

        Every accepted simulator has one.
        """
        assert isinstance(self.settings, self.Settings)
        return self.settings.simulator not in _UNCERTIFIED_SIMULATORS

    def simulation_evidence(self) -> SimEvidence | None:
        """Read the end checkpoint and generated system-task observations."""
        assert isinstance(self.settings, self.Settings)
        if self.settings.simulator == "bluesim":
            return bluesim_evidence(self)
        if self.settings.simulator == "verilator":
            return verilator_evidence(self)
        if self.settings.simulator == "iverilog":
            return iverilog_evidence(self)
        if self.settings.simulator in ("modelsim", "questa"):
            return modelsim_evidence(self)
        if self.settings.simulator in ("vcs", "vcsi"):
            return vcs_evidence(self)
        return xsim_evidence(self)

    class Settings(BscFlow.Settings, SimFlow.Settings):
        optimization_flags: list[str] = Field(
            [],
            description="Extra optimization flags passed to the simulator's compiler: the C++ "
            "compiler for Bluesim, the Verilog simulator otherwise.",
        )
        simulator: SimulatorName = Field(
            "bluesim",
            description='The simulator: "bluesim", or a supported Verilog simulator bsc links '
            'the generated Verilog for (`-vsim`): "verilator", "iverilog", "modelsim", '
            '"questa", "vcs", "vcsi" or "xsim". Xeda rejects uncertified legacy engines '
            '"cvc", "cver", "isim", "ncverilog" and "veriwell" before compilation.',
        )
        sim_dir: Path = Field(
            Path("sim_build"),
            description="Directory for the generated simulation code (Bluesim's C++ or the "
            "Verilog) and the simulation executable, relative to the run directory. It is the "
            "flow's: an earlier run's generated modules are removed before bsc compiles.",
            json_schema_extra=WORKING,
        )
        sim_args: list[str] = Field(
            [],
            description='Arguments for the simulation executable, e.g. plusargs ("+seed=3"), or '
            "Bluesim's own options.",
        )
        max_cycles: int | None = Field(
            None,
            gt=0,
            description="Stop a Bluesim simulation after this many measured rising clock edges. "
            "The evidence script of Xeda confirms the count and the final time. A finish before the limit is "
            "also valid.",
            json_schema_extra=count(),
        )
        system_verilog_tasks: bool = Field(
            True,
            description="Keep SystemVerilog system tasks such as `$fatal` and `$error` in the "
            "generated Verilog (`-system-verilog-tasks`), so that a `$fatal` fails a Verilog "
            "simulation. Without it bsc lowers `$fatal` to `$display` and `$finish(1)`, which "
            "exits successfully.",
        )
        elab: bool = Field(
            True,
            description="Also write the elaborated and scheduled modules (`.ba` files, `-elab`). "
            'bsc needs them to link imported C functions (`import "BDPI"`) into a Verilog '
            "simulation; Bluesim writes them regardless.",
        )
        remove_dollar: bool = Field(
            True,
            description="Use `_` rather than `$` between instance and port names in generated "
            "Verilog identifiers (`-remove-dollar`). Verilog simulators only.",
        )
        parallel_sim_link: int | None = Field(
            None,
            gt=0,
            description="Number of C++ files compiled at once when linking a Bluesim model "
            "(`-parallel-sim-link`). Defaults to `ncpus` when that is set, otherwise 1.",
            json_schema_extra=count(),
        )
        c_flags: list[str] = Field(
            [], description="Arguments for the C compiler (`-Xc`), one per item."
        )
        cxx_flags: list[str] = Field(
            [],
            description="Arguments for the C++ compiler (`-Xc++`), one per item: Bluesim models "
            "and imported C functions are compiled with it.",
        )
        link_flags: list[str] = Field(
            [], description="Arguments for the C/C++ linker (`-Xl`), one per item."
        )
        verilog_link_flags: list[str] = Field(
            [],
            description="Arguments for the Verilog simulator's compile step (`-Xv`), one per "
            "item.",
        )
        include_dirs: list[Path] = Field(
            [],
            description="Include directories for the C/C++ sources of imported functions "
            "(`-I`), relative to the design root.",
        )
        library_dirs: list[Path] = Field(
            [],
            description="Directories of the C/C++ libraries to link (`-L`), relative to the "
            "design root.",
        )
        libraries: list[str] = Field([], description='C/C++ libraries to link (`-l`), e.g. ["m"].')

    @classmethod
    def backend_of(cls, settings: BscFlow.Settings) -> str:
        assert isinstance(settings, cls.Settings)
        return "sim" if settings.simulator == "bluesim" else "verilog"

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Reject, before anything runs, what bsc or the evidence protocol cannot take.

        These are the settings that bsc refuses for the backend, the `sim_args` that override
        options the flow owns, `stop_time`, and `max_cycles` outside Bluesim.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if settings.simulator in _UNCERTIFIED_SIMULATORS:
            supported = ", ".join(f'"{name}"' for name in _SUPPORTED_SIMULATORS)
            raise FlowSettingsException(
                f"bsc_sim simulator {settings.simulator!r} is uncertified for simulation "
                f"evidence; choose a supported alternative: {supported}"
            )
        if settings.simulator == "bluesim" and any(
            arg.startswith(("-m", "-c", "-f")) for arg in settings.sim_args
        ):
            raise FlowSettingsException(
                "Bluesim sim_args cannot override the owned -m/-c/-f execution protocol; "
                "use max_cycles for a cycle limit"
            )
        owned_options = {
            "modelsim": {"-do", "-logfile"},
            "questa": {"-do", "-logfile"},
            "vcs": {"-ucli", "-i"},
            "vcsi": {"-ucli", "-i"},
            "xsim": {"-tclbatch", "-onfinish", "-onerror", "-log", "-runall", "-R"},
        }.get(settings.simulator, set())
        conflict = next((arg for arg in settings.sim_args if arg in owned_options), None)
        if conflict:
            raise FlowSettingsException(
                f"{settings.simulator} sim_args cannot override the owned runtime evidence "
                f"protocol with {conflict}; pass runtime data as plusargs instead"
            )
        if settings.stop_time is not None:
            raise FlowSettingsException(
                "bsc_sim cannot stop at a simulated time: the testbench ends the simulation "
                "(`$finish`), or `max_cycles` bounds a Bluesim run"
            )
        if settings.max_cycles is not None and cls.backend_of(settings) != "sim":
            raise FlowSettingsException(
                f"max_cycles bounds Bluesim simulations only, not {settings.simulator}"
            )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Reject a testbench that bsc cannot simulate, before anything runs.

        These are a testbench with no module to simulate or with several, and imported C
        functions that the Verilator backend cannot link without the DPI. `SimFlow` refuses a
        cocotb testbench, with `cocotb_advice`.
        """
        super().check_design_supported(design, settings)
        if not cls._tb_top_of(design):
            raise FlowSettingsException(
                "bsc_sim needs the testbench module: set `tb.top` (or `rtl.top` for a design "
                "without a testbench)"
            )
        if len(design.tb.top) > 1:
            raise FlowSettingsException(
                "bsc_sim simulates one top module; `tb.top` names " + ", ".join(design.tb.top)
            )
        if isinstance(settings, cls.Settings) and not settings.use_dpi:
            foreign = [
                src
                for part in sorted(cls.design_parts)
                for src in getattr(design, part).sources
                if src.type in _FOREIGN_TYPES
            ]
            if settings.simulator == "verilator" and foreign:
                raise FlowSettingsException(
                    "Verilator links imported C functions through the DPI only: set "
                    "`flows.bsc_sim.use_dpi: true`"
                )

    def init(self) -> None:
        """Reject what bsc or the simulator would reject, and a bsc that is too old, before
        anything runs.

        Planning already checks the settings. This method checks them for a flow built directly,
        whose design was checked when it was built.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.check_settings_supported(ss)
        self._warn_about_environment()
        _ = self.bsc  # constructing it checks its version: an old bsc fails before anything runs
        if ss.simulator == "verilator":
            _ = self._verilator()

    def _foreign_sources(self) -> list[Path]:
        """Return the design's C and C++ sources, objects and archives.

        The link step compiles them in for imported C functions (`import "BDPI"`).
        """
        return [src.file for src in self._sources_of(*_FOREIGN_TYPES, tb=True)]

    @staticmethod
    def _tb_top_of(design: Design) -> str | None:
        """Return the module to simulate: `tb.top`, or `rtl.top` if the design has no testbench."""
        if design.tb.top:
            return design.tb.top[0]
        if not design.tb.sources:
            return design.rtl.top
        return None

    def _tb_top(self) -> str | None:
        """Return the module that this run simulates."""
        return self._tb_top_of(self.design)

    def run(self) -> None:
        """Compile the testbench, link a simulation model, and run it."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        tb_top = self._tb_top()
        assert tb_top
        bluesim = ss.simulator == "bluesim"
        backend = "sim" if bluesim else "verilog"
        sources = self._bsc_sources(tb=True)
        tb_sources = [src for src in sources if src in self.design.tb.sources]
        top_file = self._top_file(tb_sources or sources, "testbench")
        sim_dir = self.run_file(ss.sim_dir)
        # bsc's Verilog link searches `sim_dir` first, so a module that an earlier run generated
        # there must not outlive that run.
        self._prepare_dirs(sim_dir)
        if bluesim:
            # bsc otherwise reuses C++ objects built with an earlier configuration's flags.
            self.run_directory.remove(
                *sim_dir.glob("*.o"),
                *sim_dir.glob("*.so"),
                *self.run_file(ss.bobj_dir).glob("*.bo"),
                *self.run_file(ss.bobj_dir).glob("*.ba"),
            )

        path_flags = self._path_flags(backend, sim_dir, sources)
        macros = self._macros(tb=True)
        self._check_macros(sources, macros)
        # The link step takes the macros as Verilog defines: `-D` only.
        macro_flags = _macro_flags(macros, "design macros")
        flags = [
            *self._runtime_flags(),
            *path_flags,
            *self._compile_flags(backend),
            *_macro_flags(macros, "design macros", cpp=ss.cpp),
            *ss.extra_flags,
        ]
        executable = sim_dir / tb_top
        # A Verilog link finds a module on `-vsearch` only in a file named after it. The link
        # therefore names the design's Verilog files, and a file that holds another module, or
        # several, counts too.
        link_files = [*self._foreign_sources()]
        if not bluesim:
            link_files += [src.file for src in self._sources_of(*_VERILOG_TYPES, tb=True)]
        link_flags = self._link_flags(bluesim)
        link_env: dict[str, str] | None = None
        if bluesim:
            self.copy_from_template("sim_record.h", script_filename="sim_record.h")
            header = self.copy_from_template("bluesim_hooks.h")
            # Foreign C++ compilation lacks the model's native header include directory.
            help_text = self.bsc.probe_stdout("-help")
            directory = re.search(r"^Bluespec directory: (.+)$", help_text or "", re.MULTILINE)
            if directory is not None:
                link_flags += ["-Xc++", "-I" + str(Path(directory[1]) / "Bluesim")]
            link_flags += ["-Xc++", "-include", "-Xc++", str(self.run_file(header))]
        elif ss.simulator == "verilator":
            hook_flags, link_env = self._build_verilator_hooks()
            link_flags += hook_flags
        elif ss.simulator == "iverilog":
            self._build_iverilog_monitor()
        _check_link_paths([*path_flags, *link_flags, executable, *link_files])
        self._compile(backend, sources, tb_top, top_file, flags)

        self._run_bsc(
            LINK_LOG,
            *self._runtime_flags(),
            *path_flags,
            *self._verbosity_flags(),
            f"-{backend}",
            "-e",
            tb_top,
            "-o",
            executable,
            *link_flags,
            *macro_flags,
            *link_files,
            env=link_env,
        )
        self.artifacts.executable = str(executable)

        sim_args = list(ss.sim_args)
        vcd = Path(ss.vcd) if ss.vcd else None
        if vcd:
            self.run_file(vcd).parent.mkdir(parents=True, exist_ok=True)  # no simulator creates it
        if bluesim:
            script = self.copy_from_template("bluesim_run.tcl")
            sim_args += ["-f", str(script)]
            if vcd:
                sim_args += ["-V", str(vcd)]
        elif ss.simulator == "verilator" and ss.fail_severity in ("failure", "fatal"):
            sim_args.append("+verilator+error+limit+2147483647")
        if not bluesim and vcd:
            # bsc's Verilator driver writes `dump.vcd` on `+bscvcd`. Its `main.v` takes a name.
            sim_args.append("+bscvcd" if ss.simulator == "verilator" else f"+bscvcd={vcd}")
        # A failing run's waveform is the one most wanted, so the flow records it whether or not
        # the simulation passes.
        dump: Path | None = None
        dumped = False
        if vcd:
            dump = Path("dump.vcd") if ss.simulator == "verilator" else vcd
            # Remove an earlier run's waveform and any `dump.vcd` that bsc's Verilator driver
            # left, so a waveform found is this run's. Both names are in the run directory.
            self.run_directory.remove(*unique([vcd, dump]))
        self.results["simulator"] = ss.simulator
        simulation = self.bsc.derive(
            str(executable), version_flag=None, minimum_version=None, log_key="simulation_log"
        )
        if ss.simulator == "iverilog":
            # Load before the compiled VVP file loads its builtin task definitions.
            simulation = self.bsc.derive(
                "vvp", version_flag=None, minimum_version=None, log_key="simulation_log"
            )
            sim_args = ["-i", "-n", "-m", "./iverilog_evidence.vpi", str(executable), *sim_args]
        simulation.highlight_rules = None
        try:
            env = None
            if bluesim:
                self.run_directory.remove(
                    "bluesim_events.jsonl", "bluesim_end.json", "bluesim_end.json.tmp"
                )
                env = {"XEDA_SIM_EVENTS": "bluesim_events.jsonl"}
            elif ss.simulator in ("verilator", "iverilog"):
                self.run_directory.remove("xeda_end.json", "xeda_end.json.tmp")
                env = {"XEDA_END_RECORD": "xeda_end.json"}
            if ss.simulator in ("modelsim", "questa"):
                self._run_modelsim(tb_top, sim_args)
            elif ss.simulator in ("vcs", "vcsi"):
                self._run_vcs(simulation, sim_args)
            elif ss.simulator == "xsim":
                self._run_xsim(executable, sim_args)
            else:
                simulation.run(
                    *sim_args,
                    timeout=ss.timeout,
                    env=env,
                    tee=self.run_directory.writable("sim.log"),
                    merge_stderr=True,
                )
        finally:
            if vcd and dump and self.wrote_output(dump):
                if dump != vcd:
                    # Copy the dump whole, then remove it. A link at `vcd` is replaced, never
                    # written through.
                    replacing_copy(self.run_file(dump), self.run_directory.writable(vcd))
                    self.run_directory.remove(dump)
                self.artifacts.waveform = str(vcd)
                dumped = True
        if vcd and ss.simulator == "verilator" and not dumped:
            # bsc's Verilator driver dumps on `+bscvcd`. A run that wrote nothing has no waveform.
            raise FlowFatalError(
                f"`vcd` asks for a waveform, and bsc's Verilator driver wrote no {dump} in this "
                "run: the design may end the simulation before it dumps"
            )

    def _run_modelsim(self, top: str, plusargs: list[str]) -> None:
        """Load bsc's compiled work library with Xeda's own ModelSim evidence script."""
        from ..modelsim import ModelsimTool

        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.run_directory.remove(
            "modelsim_runtime.log", "modelsim_end.txt", "modelsim_process.log"
        )
        script = self.run_directory.writable("bsc_modelsim_runtime.tcl")
        severity = {"warning": 1, "error": 2, "failure": 3, "fatal": 3}[ss.fail_severity]
        foreign_args = []
        if self._foreign_sources():
            foreign_args = (
                ["-sv_lib", "directc_" + top]
                if ss.use_dpi
                else ["-pli", "./directc_" + top + ".so"]
            )
        text = "\n".join(
            [
                "onerror {exit -code 1}",
                f"vsim -t ps -onfinish stop -lib work_{top} "
                + " ".join(tcl_word(arg) for arg in [*foreign_args, "-c", "main_opt", *plusargs]),
                "set BreakOnAssertion 4",
                "onbreak {resume}",
                "coverage attribute -name TESTSTATUS -value 0",
                "transcript off",
                'echo "XEDA_MODELSIM_RUNTIME_START"',
                "if {[catch {run -all} error]} {puts $error; transcript file {}; exit -code 1}",
                "set run_state [runStatus -full]",
                "set test_status [lindex [coverage attribute -name TESTSTATUS -concise] 0]",
                'echo "XEDA_MODELSIM_RUN_STATUS=$run_state"',
                "set fd [open modelsim_end.txt {WRONLY CREAT EXCL}]",
                'puts $fd "XEDA_MODELSIM_V1"',
                "puts $fd $now",
                "puts $fd $resolution",
                "puts $fd $test_status",
                "puts $fd $run_state",
                "close $fd",
                "transcript file {}",
                f"if {{$test_status >= {severity}}} {{exit -code 1}}",
                "exit",
                "",
            ]
        )
        with replacing_file(script, encoding="utf-8") as stream:
            stream.write(text)
        tool = ModelsimTool().derive("vsim", redirect_stdout=None, log_key="simulation_log")
        tool.run(
            "-batch",
            "-logfile",
            "modelsim_runtime.log",
            "-do",
            "do " + script.name,
            timeout=ss.timeout,
            tee=self.run_directory.writable("modelsim_process.log"),
            merge_stderr=True,
        )

    def _run_vcs(self, simulation: Tool, sim_args: list[str]) -> None:
        """Run the linked bsc VCS executable under Xeda's own UCLI adapter."""
        from ..vcs_evidence import RUNTIME_LOG, prepare_vcs_runtime

        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.run_directory.remove(RUNTIME_LOG)
        script = prepare_vcs_runtime(self, setup=[], user_script=None, stop_time=None)
        simulation.redirect_stdout = None
        simulation.run(
            *sim_args,
            "-ucli",
            "-i",
            str(script),
            timeout=ss.timeout,
            tee=self.run_directory.writable(RUNTIME_LOG),
            merge_stderr=True,
        )

    def _run_xsim(self, executable: Path, sim_args: list[str]) -> None:
        """Load bsc's retained xsim snapshot through Xeda's own xsim runtime script."""
        from ..vivado.sim_evidence import PROCESS_LOG, RUNTIME_LOG

        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.run_directory.remove(RUNTIME_LOG, PROCESS_LOG)
        script = self.run_directory.writable("bsc_xsim_runtime.tcl")
        with replacing_file(script, encoding="utf-8") as stream:
            stream.write(
                "puts XEDA_XSIM_RUNTIME_START\n"
                "flush stdout\n"
                "proc xedaCheckpoint {} {\n"
                '    puts "XEDA_XSIM_CHECKPOINT=main|[current_time]|[get_property PRECISION [current_sim]]"\n'
                "    flush stdout\n"
                "}\n"
                "if {[catch {run all} error]} {xedaCheckpoint; error $error}\n"
                "xedaCheckpoint\n"
                "puts XEDA_XSIM_RUNTIME_END\n"
                "flush stdout\n"
            )
        self.run_directory.writable(RUNTIME_LOG)
        tool = self.bsc.derive(
            "xsim", version_flag=None, minimum_version=None, log_key="simulation_log"
        )
        tool.redirect_stdout = None
        tool.run(
            str(executable) + ".xsim",
            "-log",
            str(RUNTIME_LOG),
            "-tclbatch",
            script.name,
            *sim_args,
            "-onfinish",
            "stop",
            "-onerror",
            "stop",
            timeout=ss.timeout,
            tee=self.run_directory.writable(PROCESS_LOG),
            merge_stderr=True,
        )

    def _verilator(self) -> Tool:
        """Return the Verilator tool, after requiring the release that Xeda's hooks need."""
        tool = self.bsc.derive(
            "verilator",
            version_flag=["--version"],
            version_regexps=[r"Verilator (?P<version>\d+(?:\.\d+)+)"],
            minimum_version=MIN_BSC_VERILATOR_VERSION,
        )
        # `derive` copies a model. It does not run the constructor or the version check of `Tool`.
        tool.require_minimum_version()
        return tool

    def _build_verilator_hooks(self) -> tuple[list[str], dict[str, str]]:
        """Compile the packaged hooks against the headers in the execution environment.

        Return the flags that link the hooks into the model, and the environment of the link:
        bsc runs make itself, and the environment is the only way to make
        (`verilator.HOOKS_MAKEFILE`).
        """
        from ..verilator import HOOK_MACROS, HOOKS_MAKEFILE

        tool = self._verilator()
        configuration = tool.probe_stdout("-V") or ""
        roots = re.findall(r"^\s*VERILATOR_ROOT\s*=\s*(\S.*?)\s*$", configuration, re.MULTILINE)
        if not roots:
            raise FlowSettingsException("Cannot locate VERILATOR_ROOT from verilator -V")
        for name in ("xeda_hooks.cpp", "xeda_hooks.h", HOOKS_MAKEFILE):
            with replacing_file(self.run_directory.writable(name)) as target:
                target.write(
                    files("xeda.flows.verilator").joinpath("templates/" + name).read_text()
                )
        obj = self.run_directory.writable("bsc_verilator_hooks.o")
        self.run_directory.remove(obj)
        self.bsc.derive("c++", version_flag=None, minimum_version=None, redirect_stdout=None).run(
            "-std=c++14",
            "-I",
            str(Path(roots[-1]) / "include"),
            "-c",
            "xeda_hooks.cpp",
            "-o",
            obj,
            timeout=120,
        )
        flags = ["-Xl", str(obj)]
        for macro in HOOK_MACROS:
            flags += ["-Xv", "-CFLAGS", "-Xv", "-D" + macro]
        # make runs in a directory that bsc names, so the fragment's path is absolute. It takes
        # no whitespace, which make would split at: `_check_link_paths` refuses the run path
        # with any (the hooks' object is in it too).
        return flags, {"MAKEFILES": str(self.run_path / HOOKS_MAKEFILE)}

    def _build_iverilog_monitor(self) -> None:
        """Build the VPI registration adapter, with local headers or the container's own."""
        self.copy_from_template("sim_record.h")
        source = self.copy_from_template("iverilog_evidence.c")
        if self.bsc.dockerized:
            executable = self.bsc.derive(
                "sh", version_flag=None, minimum_version=None
            ).run_get_stdout("-c", 'readlink -f "$(command -v iverilog)"')
            prefix = Path(executable.strip()).parent.parent if executable else None
            platform = "linux"
        else:
            executable_path = self.bsc.derive(
                "iverilog", version_flag=None, minimum_version=None
            ).executable_path()
            prefix = executable_path.parent.parent if executable_path else None
            platform = sys.platform
        flags = (
            ["-bundle", "-undefined", "dynamic_lookup"]
            if platform == "darwin"
            else ["-shared", "-fPIC"]
        )
        if prefix is not None:
            flags += ["-I", str(prefix / "include" / "iverilog")]
        target = self.run_directory.writable("iverilog_evidence.vpi")
        self.run_directory.remove(target)
        self.bsc.derive("cc", version_flag=None, minimum_version=None, redirect_stdout=None).run(
            *flags, source, "-o", target, timeout=120
        )

    def _link_flags(self, bluesim: bool) -> list[str]:
        """Return what the link step needs besides the paths.

        These are the simulator, the C/C++ compilation of the model and of imported functions,
        and the options that must match the compilation.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = [
            _toggle("keep-fires", ss.keep_fires),
            _toggle("use-dpi", ss.use_dpi),
        ]
        if ss.reset_prefix:
            # This also defines `BSV_RESET_NAME`, the port that bsc's clock and reset driver resets.
            flags += ["-reset-prefix", ss.reset_prefix]
        if bluesim:
            jobs = ss.parallel_sim_link or ss.nthreads
            if jobs:
                flags += ["-parallel-sim-link", str(jobs)]
            for flag in ss.optimization_flags:
                flags += ["-Xc++", flag]
        else:
            flags += ["-vsim", ss.simulator]
            for flag in ss.optimization_flags:
                flags += ["-Xv", flag]
        for option, values in (
            ("-Xc", ss.c_flags),
            ("-Xc++", ss.cxx_flags),
            ("-Xl", ss.link_flags),
            ("-Xv", ss.verilog_link_flags),
            ("-l", ss.libraries),
        ):
            for value in values:
                flags += [option, value]
        for option, dirs in (("-I", ss.include_dirs), ("-L", ss.library_dirs)):
            for d in dirs:
                flags += [option, str(self.normalize_path_to_design_root(d))]
        return flags
