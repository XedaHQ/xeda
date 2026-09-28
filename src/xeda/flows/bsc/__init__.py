"""Bluespec flows: compile Bluespec (BSV and BH) with the Bluespec compiler, `bsc`.

`bsc` generates Verilog for the design's top module, together with every Verilog file that
module needs; `bsc_sim` compiles a Bluespec testbench and simulates it with Bluesim or, through
bsc's own Verilog link step, with Verilator, Icarus Verilog or another Verilog simulator.
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABCMeta
from collections.abc import Iterable, Sequence
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import colorama

from ...dataclass import Field, field_validator
from ...design import DesignSource, SourceType
from ...flow import Flow, FlowSettingsException, SimFlow, describe_results
from ...tool import Docker, Tool
from ...utils import replacing_copy, replacing_file, unique

log = logging.getLogger(__name__)

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
#: With `positive_reset`, the first file of `artifacts.verilog`: it defines `BSV_POSITIVE_RESET`.
BSV_DEFINES_FILE = "bsv_defines.v"
#: bsc compiles BSV from `.bsv` and BH (Bluespec Classic) from `.bs`; it rejects other suffixes.
_BSC_SOURCE_SUFFIXES = (".bsv", ".bs")
#: What bsc links in for imported C functions: C and C++ sources, objects and archives.
_FOREIGN_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".o", ".a")
#: What separates the entries of a bsc search path, so no entry can contain it.
_PATH_SEPARATOR = ":"


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


def _message_list(tags: Sequence[str]) -> str:
    """bsc's `:`-separated message list."""
    return ":".join(tags)


def _bsc_path(paths: Iterable[str | Path], what: str) -> str:
    """A bsc search path: directories separated by `:`, ending with `+`, the current value."""
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
    """`-D NAME` or `-D NAME=value` per macro, for bsc's own (BSV) preprocessor and a Verilog
    link; with `cpp`, also `-Xcpp -DNAME...` for the C preprocessor, the only one BH sources go
    through. `True` (or no value) defines a bare macro and `False` leaves it undefined, so a
    design can switch an `ifdef` on and off."""
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
    """bsc's `-flag` / `-no-flag`."""
    return f"-{flag}" if value else f"-no-{flag}"


def _parse_haskell_strings(text: str) -> list[str]:
    """The strings of a Haskell `show`n `[String]`, as `bsc -print-flags-raw` prints paths."""
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
    """The module types `module` instantiates in the Verilog file at `path`: `Type [#(...)]
    name (`. Only the module's own body counts: bsc's library files also hold test modules."""
    try:
        text = _VERILOG_COMMENT.sub(" ", path.read_text(errors="replace"))
    except OSError:
        return []
    body = re.search(rf"\bmodule\s+{re.escape(module)}\b(.*?)\bendmodule\b", text, re.DOTALL)
    return unique(_VERILOG_INSTANCE.findall(body.group(1) if body else text))


def _verilog_modules(path: Path) -> list[str]:
    """Names of the modules a Verilog file declares."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return _VERILOG_MODULE.findall(_VERILOG_COMMENT.sub(" ", text))


class BscFlow(Flow, metaclass=ABCMeta):
    """Compile Bluespec with bsc: the settings and the compilation `bsc` and `bsc_sim` share."""

    class Settings(Flow.Settings):
        # ------------------------------------------------------------------ directories and paths
        bobj_dir: Path = Field(
            Path("bobjs"),
            description="Directory for the packages bsc compiles (`.bo`/`.ba` files, `-bdir`), "
            "relative to the run directory.",
        )
        info_dir: Path | None = Field(
            None,
            description="Directory for bsc's informational files (`-info-dir`), such as the "
            "schedules of `show_schedule` and the graphs of `sched_dot`. Defaults to `bobj_dir`.",
        )
        cleanup_bobjs: bool = Field(
            True,
            description="Before compiling, remove what an earlier run compiled: the packages "
            "(`.bo`/`.ba`) in `bobj_dir`, and the Verilog modules bsc generated (each `.use` file "
            "and the `.v` bsc wrote beside it) in the output directory. bsc recompiles a package "
            "(`-u`) only when its source is newer than its `.bo`, so without this a change of "
            "flags or macros alone is not compiled, and a module the design no longer generates "
            "is still found. Disable only to reuse the output of a run with identical settings "
            "and sources.",
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
        # ------------------------------------------------------------------ preprocessing
        cpp: bool = Field(
            False,
            description="Run the C preprocessor on the sources before bsc's own preprocessor "
            "(`-cpp`), and define the design's macros for it too. BH (`.bs`) sources go through "
            "no other preprocessor, so this is how they see `rtl.defines`: `#ifdef`, not "
            "`` `ifdef ``.",
        )
        cpp_flags: list[str] = Field(
            [],
            description="Arguments passed to the C preprocessor (`-Xcpp`), one per item, "
            "with `cpp`.",
        )
        # ------------------------------------------------------------------ semantics
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
        # ------------------------------------------------------------------ code generation
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
        # ------------------------------------------------------------------ diagnostics
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
        # ------------------------------------------------------------------ messages
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
        # ------------------------------------------------------------------ limits and runtime
        steps_warn_interval: int = Field(
            2_000_000,
            gt=0,
            description="Warn each time elaboration has unfolded this many more function calls "
            "(`-steps-warn-interval`; bsc's own default is 100000).",
        )
        steps_max_intervals: int = Field(
            6_000_000,
            gt=0,
            description="Give up elaborating after this many `steps_warn_interval` warnings "
            "(`-steps-max-intervals`; bsc's own default is 10). The default only stops runaway "
            "elaborations, since large designs legitimately take many steps.",
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

    # ---------------------------------------------------------------------------------- helpers

    def _bsc_sources(self, *, tb: bool) -> list[DesignSource]:
        """The design's Bluespec sources, RTL first, checked for suffixes bsc accepts."""
        sources = self.design.sources_of_type(SourceType.Bluespec, rtl=True, tb=tb)
        bad = [str(src.file) for src in sources if src.file.suffix not in _BSC_SOURCE_SUFFIXES]
        if bad:
            raise FlowSettingsException(
                "bsc compiles BSV from `.bsv` files and BH (Bluespec Classic) from `.bs` files "
                f"only; rename: {', '.join(bad)}"
            )
        return sources

    def _macros(self, *, tb: bool) -> dict[str, Any]:
        """Preprocessor macros: the design's `defines` and `parameters` (the testbench's on top
        of the RTL's), and `BSV_POSITIVE_RESET`. A fresh mapping: the design is not ours."""
        assert isinstance(self.settings, BscFlow.Settings)
        macros: dict[str, Any] = {**self.design.rtl.defines, **self.design.rtl.parameters}
        if tb:
            macros.update({**self.design.tb.defines, **self.design.tb.parameters})
        if self.settings.positive_reset:
            macros["BSV_POSITIVE_RESET"] = None
        return macros

    def _check_macros(self, sources: list[DesignSource], macros: dict[str, Any]) -> None:
        """Warn when the design's macros reach none of its sources: bsc's own preprocessor reads
        BSV only, so BH sees macros only through the C preprocessor (`cpp`). A design mixing
        the two may well hand its macros to BH through a BSV package, so it is not warned."""
        assert isinstance(self.settings, BscFlow.Settings)
        given = [name for name in macros if name != "BSV_POSITIVE_RESET"]
        bh = [src.file.name for src in sources if src.file.suffix == ".bs"]
        if given and bh and len(bh) == len(sources) and not self.settings.cpp:
            log.warning(
                "The design's macros (%s) reach none of its sources, all of them BH (%s): bsc "
                "preprocesses BSV only. Set `cpp = true` to define them for the C preprocessor, "
                "which BH sources then go through (`#ifdef`).",
                ", ".join(given),
                ", ".join(bh),
            )

    def _path_flags(self, backend: str, out_dir: Path, sources: list[DesignSource]) -> list[str]:
        """Where bsc reads and writes: `-bdir`, `-info-dir`, the output directory, `-p` and,
        for Verilog, `-vsearch`. Absolute, since bsc runs in the run directory."""
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        bdir = Path(ss.bobj_dir).absolute()
        info_dir = Path(ss.info_dir).absolute() if ss.info_dir else bdir
        flags = ["-bdir", str(bdir), "-info-dir", str(info_dir)]
        flags += ["-simdir" if backend == "sim" else "-vdir", str(out_dir.absolute())]
        package_dirs = [src.file.parent for src in sources]
        package_dirs += [self.normalize_path_to_design_root(p) for p in ss.search_paths]
        flags += ["-p", _bsc_path(package_dirs, "search_paths")]
        if backend == "sim":
            # Bluesim reads no Verilog (it rejects imported Verilog modules). And without
            # `-vdir`, bsc warns (S0073) when a `-vsearch` directory is also on `-p`, which its
            # default Verilog path includes: that of a Bluespec source with Verilog beside it.
            return flags + self._fdir_flags()
        verilog_dirs: list[str | Path] = [
            self.normalize_path_to_design_root(p) for p in ss.verilog_search_paths
        ]
        verilog_dirs += [
            src.file.parent
            for src in self.design.sources_of_type(
                SourceType.Verilog, SourceType.SystemVerilog, rtl=True, tb=True
            )
        ]
        flags += ["-vsearch", _bsc_path(self._vendor_verilog_dirs() + verilog_dirs, "vsearch")]
        return flags + self._fdir_flags()

    def _fdir_flags(self) -> list[str]:
        """`-fdir`, where relative file names in the design resolve during elaboration."""
        assert isinstance(self.settings, BscFlow.Settings)
        if not self.settings.fdir:
            return []
        return ["-fdir", str(self.normalize_path_to_design_root(self.settings.fdir))]

    def _vendor_verilog_dirs(self) -> list[str | Path]:
        """Directories of bsc's vendor-specific Verilog primitives, searched first."""
        return []

    def _runtime_flags(self) -> list[str]:
        """`haskell_runtime_flags` for bsc's own runtime, which reads them between `+RTS` and
        `-RTS`."""
        assert isinstance(self.settings, BscFlow.Settings)
        rts = self.settings.haskell_runtime_flags
        return ["+RTS", *rts, "-RTS"] if rts else []

    def _verbosity_flags(self) -> list[str]:
        """`-verbose` for a verbose run, `-quiet` for a quiet one."""
        if self.settings.verbose:
            return ["-verbose"]
        if self.settings.is_quiet:
            return ["-quiet"]
        return []

    def _compile_flags(self, backend: str) -> list[str]:
        """Every flag the settings give a compilation for `backend` ("verilog" or "sim")."""
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        flags = self._verbosity_flags()
        # `-split-if` also switches `-aggressive-conditions`, so the latter comes after it
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
        # `-synthesize` also switches `-opt-bit-const`, so it comes before the optimization flags
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
            # bsc rejects `-remove-dollar` for Bluesim, and for every link
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
        # an empty list would be taken as the flag's argument by the next flag
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

    def _check_settings(self, backend: str) -> None:
        """Reject combinations bsc refuses, before anything runs, naming the settings."""
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        if ss.unspecified_to in ("X", "Z") and not ss.opt_undetermined_vals:
            raise FlowSettingsException(
                f'{self.name}: unspecified_to = "{ss.unspecified_to}" requires '
                'opt_undetermined_vals = true; otherwise use "0", "1" or "A"'
            )
        if backend == "sim" and ss.unspecified_to in ("X", "Z"):
            raise FlowSettingsException(
                f'{self.name}: Bluesim is two-valued and cannot tie don\'t-cares to "X" or "Z"; '
                'set unspecified_to to "0", "1" or "A"'
            )
        if "BSC_OPTIONS" in os.environ:
            log.warning(
                "BSC_OPTIONS is set (%r): bsc prepends it to every command line, so the run "
                "does not depend on its settings alone",
                os.environ["BSC_OPTIONS"],
            )

    def _prepare_dirs(self, out_dir: Path, out_setting: str) -> None:
        """Create the directories bsc writes to and, with `cleanup_bobjs`, remove what an
        earlier run left in them: its packages, and the Verilog modules it generated. bsc writes
        a module's `.use` file (`-show-module-use`) right after its `.v`, so each `.use` goes
        with the `.v` beside it, whatever a `verilog_filters` command made of that `.v`. Other
        files stay, and the library modules copied in are copied again. Both directories must
        lie inside the run directory (`Flow.removable_work_dir`, naming `bobj_dir` or
        `out_setting`): xeda removes nothing outside it."""
        assert isinstance(self.settings, BscFlow.Settings)
        if self.settings.verilog_filters and any(c.isspace() for c in str(out_dir)):
            raise FlowSettingsException(
                "bsc runs each `verilog_filters` command on a generated file through a shell, "
                f"without quoting, so the output directory cannot contain whitespace: {out_dir}"
            )
        bdir = Path(self.settings.bobj_dir)
        if self.settings.cleanup_bobjs:
            bdir = self.removable_work_dir(bdir, "bobj_dir")
            out_dir = self.removable_work_dir(out_dir, out_setting)
            stale = [obj for pattern in ("*.bo", "*.ba") for obj in bdir.glob(pattern)]
            for use_file in out_dir.glob("*.use"):
                stale += [use_file, use_file.with_suffix(".v")]
            for path in stale:
                if path.is_file():
                    path.unlink()
        bdir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.info_dir:
            Path(self.settings.info_dir).mkdir(parents=True, exist_ok=True)

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
                self.bsc.run(*flags, "-u", backend_flag, src.file)
        self.bsc.run(*flags, "-u", backend_flag, "-g", top, top_file)

    def _top_file(self, sources: list[DesignSource], what: str) -> Path:
        """The file holding the top module: the last Bluespec source."""
        if not sources:
            raise FlowSettingsException(f"{self.name}: the design has no Bluespec {what} sources")
        return sources[-1].file


class Bsc(BscFlow):
    """Compile a Bluespec (BSV or BH) design to Verilog with the Bluespec compiler, bsc.

    Compiles `rtl.sources` in order -- the package defining `rtl.top` last -- and generates
    Verilog for `rtl.top` and for every `synthesize`d module on the way. `artifacts.verilog` lists
    the Verilog files the top module needs: the generated modules, the design's own Verilog
    sources, and the modules of bsc's Verilog library it instantiates, which are copied into
    `verilog_out_dir` so any downstream synthesis or simulation flow can take the list as is. A
    module the generated Verilog instantiates that no Verilog file defines (a vendor primitive,
    say) is left out with a warning: the downstream tool must provide it. `rtl.defines` and
    `rtl.parameters` are passed to bsc as preprocessor macros; BH (`.bs`) sources see them only
    through the C preprocessor, with `cpp`.
    """

    results_description = describe_results(
        modules="Verilog modules of the design's hierarchy, the top first and each module "
        "before those it instantiates: the ones bsc generated, the library modules and the "
        "design's own Verilog modules.",
    )

    class Settings(BscFlow.Settings):
        verilog_out_dir: Path = Field(
            Path("gen_rtl"),
            description="Directory for the generated Verilog and the library modules it uses, "
            "relative to the run directory. It is the flow's: bsc overwrites the modules it "
            "generates, the library modules are copied over, and `cleanup_bobjs` removes an "
            "earlier run's generated modules.",
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
            "preprocessor, and at the top of every Verilog file in `artifacts.verilog` that bsc "
            "generated or copied from its library (they take their reset polarity from it). "
            "The design's own Verilog sources are passed on unchanged, preceded by "
            f"`{BSV_DEFINES_FILE}`, the first artifact, which defines the macro for those "
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

    def init(self) -> None:
        """Reject what bsc would reject, and an old bsc, before anything runs."""
        assert isinstance(self.settings, self.Settings)
        if not self.design.rtl.top:
            raise FlowSettingsException("bsc needs the design's top module: set `rtl.top`")
        self._check_settings("verilog")
        _ = self.bsc  # constructing it checks its version: an old bsc fails before anything runs

    def _vendor_verilog_dirs(self) -> list[str | Path]:
        """The `verilog_primitives` directory of bsc's library (`%` is bsc's install), which
        goes ahead of the generic modules on the Verilog search path."""
        assert isinstance(self.settings, self.Settings)
        vendor = self.settings.verilog_primitives
        if vendor == "generic":
            return []
        return ["%/Verilog." + {"vivado": "Vivado", "quartus": "Quartus"}[vendor]]

    def _verilog_path(self, path_flags: list[str]) -> list[Path]:
        """The directories bsc searches for Verilog modules, in its order: the value
        `-print-flags-raw` reports for the same path flags (`%` and `+` expanded)."""
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
        vout_dir = Path(ss.verilog_out_dir).absolute()
        log.info("Verilog output directory: %s", vout_dir)
        self._prepare_dirs(vout_dir, "verilog_out_dir")

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

        # read with the compile's own path flags: `extra_flags` may add a `-vsearch`
        verilog_path = self._verilog_path([*path_flags, *ss.extra_flags])
        modules, files = self._collect_verilog(top, top_file, vout_dir, verilog_path)
        self.results["modules"] = modules
        self.artifacts.verilog = [str(f) for f in files]

    def _collect_verilog(
        self, top: str, top_file: Path, vout_dir: Path, verilog_path: list[Path]
    ) -> tuple[list[str], list[Path]]:
        """The modules under `top` and the Verilog files that define them.

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
        design_verilog = self.design.sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, rtl=True, tb=False
        )
        defined_in: dict[str, Path] = {}
        for src in design_verilog:
            for module in _verilog_modules(src.file):
                defined_in.setdefault(module, src.file)
        library_dirs = [d for d in verilog_path if d.absolute() != vout_dir]

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
            """A library or design module's own submodules, which no `.use` file lists: bsc's
            `MakeResetA` instantiates `SyncResetA`, and a Verilog wrapper the design imports may
            instantiate a module bsc generated. Only names that resolve to a module count, so a
            word that merely looks like an instance is never reported missing."""
            for name in _verilog_instances(path, module):
                if name != module and (
                    name in defined_in or bsc_generated(name) or in_library(name)
                ):
                    visit(name)

        def visit(module: str) -> None:
            """Record `module` -- generated (its `.use` lists its submodules), a design
            source's, a library copy, or unresolved -- then its submodules; a generated module's
            file goes after theirs."""
            if module in modules or module in unresolved:
                return
            use_file = vout_dir / f"{module}.use"
            verilog = vout_dir / f"{module}.v"
            if bsc_generated(module):
                if module in defined_in:
                    raise FlowSettingsException(
                        f"{module} is both generated by bsc ({verilog}) and defined by the "
                        f"design's Verilog source {defined_in[module]}"
                        + (
                            ""
                            if ss.cleanup_bobjs
                            else "; with cleanup_bobjs = false the generated file may be from an "
                            "earlier run"
                        )
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
            copy = vout_dir / f"{module}.v"
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
            # the design's own Verilog sources are left as they are: a first file defines the
            # macro for them too, wherever they come in a compilation unit
            defines_file = vout_dir / BSV_DEFINES_FILE
            with replacing_file(defines_file) as f:
                f.write(
                    "`define BSV_POSITIVE_RESET\n"
                    "// Written by xeda's bsc flow, to define it before the design's own Verilog.\n"
                )
            defines.append(defines_file)
        files = defines + library + [src.file for src in design_verilog] + generated
        return modules, unique(files)


def _check_link_paths(args: list[str | Path]) -> None:
    """Reject the paths bsc's link step cannot take: bsc runs the C++ compiler and the Verilog
    simulator through a shell, unquoted, so a path with whitespace falls apart there (bsc's own
    compilation copes)."""
    paths = [part for arg in map(str, args) for part in arg.split(_PATH_SEPARATOR)]
    spaced = unique([p for p in paths if not p.startswith("-") and any(c.isspace() for c in p)])
    if spaced:
        raise FlowSettingsException(
            "bsc's link step runs its tools through a shell without quoting, so it cannot take a "
            f"path with whitespace: {', '.join(spaced)}. Use a run directory (`--xeda-run-dir`) "
            "and design location without whitespace, or set `sim_dir`/`bobj_dir` elsewhere."
        )


def _prepend_define(path: Path, macro: str) -> None:
    """Define `macro` at the top of a Verilog file, once. As bytes: a library or imported
    module's file need not be UTF-8."""
    line = f"`define {macro}\n".encode()
    content = path.read_bytes()
    if not content.startswith(line):
        with replacing_file(path, "wb") as f:  # a link at the name is replaced, not followed
            f.write(line + content)


class BscSim(BscFlow, SimFlow):
    """Simulate a Bluespec testbench compiled by bsc, with Bluesim or a Verilog simulator.

    Compiles the design's Bluespec sources, RTL then testbench, in order -- the package defining
    `tb.top` last -- and simulates `tb.top`, a module with an `Empty` interface (`rtl.top` when
    the design has no testbench). `simulator` picks Bluesim, bsc's own cycle-based simulator, or
    a Verilog simulator that bsc links the generated Verilog with (`bsc -vsim`), driving clock
    and reset from its `main.v`. The run fails when the simulation exits with an error status:
    a testbench fails with `$fatal` or a failing `dynamicAssert`. `$finish(n)` does not fail it
    (`n` is a verbosity level), and of Bluesim, Verilator and Icarus Verilog, `$error` fails it
    only under Verilator. The design's
    `defines` and `parameters`, the testbench's over the RTL's, are preprocessor macros, which
    BH (`.bs`) sources see only through the C preprocessor, with `cpp`.
    """

    results_description = describe_results(
        simulator="The simulator that ran the testbench.",
    )

    class Settings(BscFlow.Settings, SimFlow.Settings):
        simulator: SimulatorName = Field(
            "bluesim",
            description='The simulator: "bluesim", or a Verilog simulator bsc links the '
            'generated Verilog for (`-vsim`): "verilator", "iverilog", "cvc", "cver", "isim", '
            '"modelsim", "ncverilog", "questa", "vcs", "vcsi", "veriwell" or "xsim". Xeda is '
            "tested with bluesim, verilator and iverilog.",
        )
        sim_dir: Path = Field(
            Path("sim_build"),
            description="Directory for the generated simulation code (Bluesim's C++ or the "
            "Verilog) and the simulation executable, relative to the run directory. It is the "
            "flow's: `cleanup_bobjs` removes an earlier run's generated modules.",
        )
        sim_args: list[str] = Field(
            [],
            description='Arguments for the simulation executable, e.g. plusargs ("+seed=3"), or '
            "Bluesim's own options.",
        )
        max_cycles: int | None = Field(
            None,
            gt=0,
            description="Stop a Bluesim simulation after this many clock cycles (`-m`), even if "
            "the testbench has not finished. Bluesim only. A run stopped there passes, since "
            "Bluesim exits with status 0: a testbench must report its own success.",
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

    def init(self) -> None:
        """Reject what bsc or the simulator would reject, and an old bsc, before anything
        runs: settings bsc refuses for the backend, a testbench it cannot simulate, and options
        the chosen simulator lacks."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        backend = "sim" if ss.simulator == "bluesim" else "verilog"
        self._check_settings(backend)
        if self.design.tb.cocotb:
            raise FlowSettingsException(
                "bsc_sim runs Bluespec testbenches; for a cocotb testbench, simulate the Verilog "
                "that the `bsc` flow generates"
            )
        if not self._tb_top():
            raise FlowSettingsException(
                "bsc_sim needs the testbench module: set `tb.top` (or `rtl.top` for a design "
                "without a testbench)"
            )
        if len(self.design.tb.top) > 1:
            raise FlowSettingsException(
                "bsc_sim simulates one top module; `tb.top` names " + ", ".join(self.design.tb.top)
            )
        if ss.stop_time is not None:
            raise FlowSettingsException(
                "bsc_sim cannot stop at a simulated time: the testbench ends the simulation "
                "(`$finish`), or `max_cycles` bounds a Bluesim run"
            )
        if ss.max_cycles is not None and backend != "sim":
            raise FlowSettingsException(
                f"max_cycles bounds Bluesim simulations only, not {ss.simulator}"
            )
        if ss.simulator == "verilator" and self._foreign_sources() and not ss.use_dpi:
            raise FlowSettingsException(
                "Verilator links imported C functions through the DPI only: set use_dpi = true"
            )
        _ = self.bsc  # constructing it checks its version: an old bsc fails before anything runs

    def _foreign_sources(self) -> list[Path]:
        """The design's C/C++ sources, objects and archives, which the link step compiles in
        for imported C functions (`import "BDPI"`): the files bsc accepts at link, whatever
        type the design gave them (xeda types `.c` as nothing)."""
        return [
            src.file
            for src in self.design.sources_of_type("*", rtl=True, tb=True)
            if src.file.suffix in _FOREIGN_SUFFIXES
        ]

    def _tb_top(self) -> str | None:
        """The module to simulate: `tb.top`, or `rtl.top` for a design without a testbench."""
        if self.design.tb.top:
            return self.design.tb.top[0]
        if not self.design.tb.sources:
            return self.design.rtl.top
        return None

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
        sim_dir = Path(ss.sim_dir).absolute()
        # bsc's Verilog link searches `sim_dir` first, so a module generated there by an earlier
        # run must not outlive it
        self._prepare_dirs(sim_dir, "sim_dir")

        path_flags = self._path_flags(backend, sim_dir, sources)
        macros = self._macros(tb=True)
        self._check_macros(sources, macros)
        # the link step takes the macros as Verilog defines: `-D` only
        macro_flags = _macro_flags(macros, "design macros")
        flags = [
            *self._runtime_flags(),
            *path_flags,
            *self._compile_flags(backend),
            *_macro_flags(macros, "design macros", cpp=ss.cpp),
            *ss.extra_flags,
        ]
        executable = sim_dir / tb_top
        # a Verilog link finds a module on `-vsearch` only in a file named after it, so the
        # design's Verilog is named: a file holding another module, or several, counts too
        link_files = [*self._foreign_sources()]
        if not bluesim:
            link_files += [
                src.file
                for src in self.design.sources_of_type(
                    SourceType.Verilog, SourceType.SystemVerilog, rtl=True, tb=True
                )
            ]
        link_flags = self._link_flags(bluesim)
        _check_link_paths([*path_flags, *link_flags, executable, *link_files])
        self._compile(backend, sources, tb_top, top_file, flags)

        self.bsc.run(
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
        )
        self.artifacts.executable = str(executable)

        sim_args = list(ss.sim_args)
        vcd = Path(ss.vcd) if ss.vcd else None
        if vcd:
            vcd.parent.mkdir(parents=True, exist_ok=True)  # no simulator creates it
        if bluesim:
            if ss.max_cycles is not None:
                sim_args += ["-m", str(ss.max_cycles)]
            if vcd:
                sim_args += ["-V", str(vcd)]
        elif vcd:
            # bsc's Verilator driver writes `dump.vcd` on `+bscvcd`; its `main.v` takes a name
            sim_args.append("+bscvcd" if ss.simulator == "verilator" else f"+bscvcd={vcd}")
        # A failing run's waveform is the one most wanted, so it is recorded whether or not the
        # simulation passes -- and an earlier run's goes first, so a waveform found is this run's.
        dump: Path | None = None
        if vcd:
            dump = Path("dump.vcd") if ss.simulator == "verilator" else vcd
            for old in {vcd, dump}:
                self.remove_stale_output(old)  # inside the run directory only
        self.results["simulator"] = ss.simulator
        simulation = self.bsc.derive(str(executable), version_flag=None, minimum_version=None)
        simulation.highlight_rules = None
        try:
            simulation.run(*sim_args)
        finally:
            if vcd and dump and self.wrote_output(dump):
                if dump != vcd:
                    # copied, not moved: a link at `vcd` is replaced, never written through
                    replacing_copy(dump, vcd)
                    self.remove_stale_output(dump)
                self.artifacts.vcd = str(vcd)

    def _link_flags(self, bluesim: bool) -> list[str]:
        """What the link step needs besides the paths: the simulator, the C/C++ compilation of
        the model and of imported functions, and what must match the compilation."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = [
            _toggle("keep-fires", ss.keep_fires),
            _toggle("use-dpi", ss.use_dpi),
        ]
        if ss.reset_prefix:
            # also defines `BSV_RESET_NAME`, the port bsc's clock and reset driver resets
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
