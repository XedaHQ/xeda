"""The Bluespec flows, which compile Bluespec (BSV and BH) with the Bluespec compiler, `bsc`.

The `bsc` flow generates Verilog for the design's top module, with every Verilog file that
module needs. The `bsc_sim` flow compiles a Bluespec testbench and simulates it with Bluesim.
It can also simulate with Verilator, Icarus Verilog or another Verilog simulator, through the
Verilog link step of `bsc`.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
from importlib.resources import files
from abc import ABCMeta
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal

import colorama

from ...dataclass import WORKING, Field, NoReplacement, field_validator
from ...design import Design, Source, SourceType
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
from ..iverilog import MONITOR, build_end_monitor
from .sim_evidence import (
    bluesim_evidence,
    iverilog_evidence,
    modelsim_evidence,
    vcs_evidence,
    verilator_evidence,
    xsim_evidence,
)
from ...package_paths import impl_module
from ...tool import Docker, Tool
from ...units import count
from ...listing import VCS_METADATA, directory_files
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
#: The listed sources that the stage folder `bsc_sources` holds.
_STAGED_SOURCE_TYPES = (SourceType.Bluespec, SourceType.BluespecHeader)
#: The listed sources that `bsc_sources` also holds with `cpp`: the C preprocessor looks for a
#: quoted `#include` in the folder of the BH source as bsc names it.
_CPP_STAGED_TYPES = (SourceType.CHeader,)
#: The folder of the run directory that holds a wrapper for each C and C++ source of the link.
_FOREIGN_DIR = "bsc_foreign"

#: The flags that make the C and the C++ compiler write a depfile of the headers a source read,
#: leaving out the system headers, beside the object of the source.
_DEPFILE_FLAGS = ("-Xc", "-MMD", "-Xc++", "-MMD")
#: What separates the entries of a bsc search path, so no entry can contain it.
_PATH_SEPARATOR = ":"
#: The folder of the run directory that holds a link to each listed Bluespec source and Bluespec
#: header. bsc finds the packages and the include files of the design there, and nowhere else.
STAGE_SOURCES = "bsc_sources"
#: The folder of the run directory that holds a link to each listed Verilog source. bsc finds
#: the modules of the design's `import "BVI"` there.
STAGE_VERILOG = "bsc_verilog"
#: The folder of the run directory that holds one folder for each entry of `search_paths`
#: (`bsc_search/0`, `bsc_search/1`, ...). Each holds a link to what lies in the entry, less what
#: would let bsc read a file that the design does not list (`BscFlow._stage_search`).
STAGE_SEARCH = "bsc_search"

#: The stage folders and the folder of the C and C++ wrappers: the folders of the run directory
#: that the flow fills with links and wrappers. No output folder of bsc may be one or lie
#: inside one.
STAGE_FOLDERS = (STAGE_SOURCES, STAGE_VERILOG, STAGE_SEARCH, _FOREIGN_DIR)

#: The variable through which bsc reads flags from the environment. An empty value means unset.
BSC_OPTIONS = "BSC_OPTIONS"

#: The flags of bsc that pass their next argument on, whatever it is, to a program that bsc runs.
_PASS_THROUGH_FLAGS = frozenset({"-Xc", "-Xc++", "-Xcpp", "-Xl", "-Xv"})


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

    def run(self, *args: Any, env: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
        """Run bsc with an empty `BSC_OPTIONS`, so that no flag of the caller's environment
        reaches it. bsc treats an empty value as unset. A program that the flow derives from this
        tool (a simulator, a C++ compiler) gets it too, which does it no harm."""
        return super().run(*args, env={**(env or {}), BSC_OPTIONS: ""}, **kwargs)


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


#: The advice that Xeda adds to a failure of bsc, by the text that identifies the failure.
_MISSING_PACKAGE = re.compile(r"\(S0000\)|Cannot find package")
_MISSING_INCLUDE = re.compile(r"\(S0034\)|Cannot find the file .* to be included")
_MISSING_INCLUDE_NAME = re.compile(r"Cannot find the file `([^']*)' to be included")


_QUOTED_POSITION = re.compile(r'"([^"\r\n]+)", line (?=\d)')
_COMPILER_POSITION = re.compile(r"^(\s*)([^\s:][^:]*?):(?=\d+:\d+:)")
_PROGRESS_PATH = re.compile(r"^((?:re)?compiling )(\S.*?)(\s*)$")


def _real_path(name: str, paths: Mapping[str, str]) -> str | None:
    """Return the path that `paths` gives for `name`: for the name itself, else for its nearest
    parent folder, with the rest of `name` appended."""
    if name in paths:
        return paths[name]
    folder = name
    while "/" in folder:
        folder, _, _ = folder.rpartition("/")
        if folder in paths:
            return paths[folder] + name[len(folder) :]
    return None


def _positioned_path(name: str, paths: Mapping[str, str]) -> str | None:
    """Return the real path for `name`, the file name in a compiler position. The compiler may
    print it with a run-directory prefix and with repeated slashes (`run///bsc_sources/A.bs`),
    so try the name and then each tail of it that starts after a slash."""
    name = re.sub(r"/{2,}", "/", name)
    real = _real_path(name, paths)
    start = name.find("/")
    while real is None and start != -1:
        real = _real_path(name[start + 1 :], paths)
        start = name.find("/", start + 1)
    return real


def rewrite_paths(line: str, paths: Mapping[str, str]) -> str:
    """Return `line`, a line of bsc output, with the staged file names of a message replaced by
    the real paths in `paths`.

    Only three forms change. A quoted name that a position follows (`"A.bsv", line 3`) is the
    file of a message. A line that starts with `compiling ` or `recompiling ` and a name is a
    progress line. A line that starts with a compiler position (`A.bs:3:5:`) names the file of
    a message from the C preprocessor, which may be absolute. Any other text stays as it is. A
    name is in `paths` itself, or lies in a folder that is."""

    def quoted(match: re.Match[str]) -> str:
        real = _real_path(match[1], paths)
        return match[0] if real is None else f'"{real}", line '

    line = _QUOTED_POSITION.sub(quoted, line)
    position = _COMPILER_POSITION.match(line)
    if position is not None:
        real = _positioned_path(position[2], paths)
        if real is not None:
            line = f"{position[1]}{real}:{line[position.end() :]}"
    progress = _PROGRESS_PATH.match(line)
    if progress is not None:
        real = _real_path(progress[2], paths)
        if real is not None:
            return f"{progress[1]}{real}{progress[3]}"
    return line


def _hint(shown: Sequence[str], *, cpp: bool) -> str:
    """Return the advice for the error lines `shown` of a failed bsc run, or an empty text.

    The advice starts with a space, so that it can follow the quoted lines. It names the setting
    or the source entry that gives bsc the file it did not find."""
    text = "\n".join(shown)
    if _MISSING_PACKAGE.search(text):
        return (
            " (bsc reads only the listed sources and the folders of the path settings: list the"
            " file of the package, or name its folder in `search_paths`, or its folder in bsc's"
            " installation in `installed_libraries`)"
        )
    if _MISSING_INCLUDE.search(text):
        named = _MISSING_INCLUDE_NAME.search(text)
        if named is not None and "/" in named.group(1):
            return (
                " (the name has a folder part, and a listed `BluespecHeader` is linked by its"
                " file name alone, so it cannot serve this name: name the folder that holds"
                f" `{named.group(1).split('/')[0]}` in `search_paths`)"
            )
        return " (list the file with `type: BluespecHeader`, or name its folder in `search_paths`)"
    if cpp and any("file not found" in line or "No such file" in line for line in shown):
        return (
            f" (with `cpp`, a BH source finds a quoted `#include` only among the listed "
            f"sources, which are links in `{STAGE_SOURCES}`: list the header in the "
            "design's sources)"
        )
    return ""


def _message_list(tags: Sequence[str]) -> str:
    """Return bsc's `:`-separated message list."""
    return ":".join(tags)


def _installed_library_problem(entry: str) -> str | None:
    """Return why `entry` is no folder below bsc's `Libraries` folder, or `None` if it is one."""
    if not entry:
        return "is empty"
    if ":" in entry:
        return 'has a ":", which separates the entries of a bsc path'
    if "%" in entry:
        return 'has a "%", which bsc expands'
    if any(ch.isspace() for ch in entry):
        return "has whitespace"
    if entry.startswith("/"):
        return "is absolute; give a folder below the `Libraries` folder of bsc"
    if any(part in ("", ".", "..") for part in entry.split("/")):
        return 'has an empty, "." or ".." component'
    return None


def staged_name(src: Source) -> Path | None:
    """Return the name, relative to the run directory, of the link that stands for the listed
    source `src` in a stage folder, or `None` if no stage folder holds the source.

    The name depends only on the source: its type and its file name."""
    if src.type in _STAGED_SOURCE_TYPES or src.type in _CPP_STAGED_TYPES:
        return Path(STAGE_SOURCES) / src.file.name
    if src.type in _VERILOG_TYPES:
        return Path(STAGE_VERILOG) / src.file.name
    return None


def owned_flag_words(flags: Sequence[str], owned: Iterable[str]) -> list[str]:
    """Return the words of `flags` that name a flag of `owned`, in order.

    `owned` holds flag names without the dash (`p`, `bdir`). A word names a flag with one dash
    (`-bdir`), or with two when the name has two letters or more (`--bdir`), as bsc reads it.
    This holds for the pass-through flags too (`--Xc`). A word right after a pass-through flag
    (`-Xc -p`) is an argument for another program, so it names no flag. Any other
    flag of bsc refuses an argument that starts with `-`, so the rule holds for every word of
    `flags`."""
    names = set(owned)
    found: list[str] = []
    argument = False  # the word is the argument of a pass-through flag
    for word in flags:
        if argument:
            argument = False
            continue
        # bsc reads `--name` as `-name` when the name has two letters or more.
        single = word[1:] if word.startswith("--") and len(word) > 3 and word[2] != "-" else word
        if single in _PASS_THROUGH_FLAGS:
            argument = True
        elif single.startswith("-") and single[1:] in names:
            found.append(word)
    return found


def _stage_folder_conflict(path: Path) -> str | None:
    """Return how `path`, a working folder relative to the run directory, meets the stage folders
    (`STAGE_FOLDERS`), or `None` if it does not. The judgment is lexical. The settings already
    refuse a working folder that is absolute or has a `..` part."""
    if not path.parts:
        return "is the run directory itself"
    if path.parts[0].casefold() in {folder.casefold() for folder in STAGE_FOLDERS}:
        return f"is or lies inside the stage folder `{path.parts[0]}`"
    return None


def _bsc_path(paths: Iterable[str | Path], what: str, tail: str) -> str:
    """Return a bsc search path: directories separated by `:`, then `tail`, the entries that
    bsc itself adds. bsc expands a `%` in an entry to its installation. So an entry may hold
    one only as the flow's own `%/...` entry, and a user directory with `%` is refused."""
    entries = []
    for path in paths:
        text = str(path)
        if "%" in text and not text.startswith("%/"):
            raise FlowSettingsException(
                f"{what}: bsc expands '%' in a search-path entry to its installation, so it "
                f"cannot use the directory {text!r}"
            )
        if _PATH_SEPARATOR in text:
            raise FlowSettingsException(
                f"{what}: bsc separates search-path entries with ':', so it cannot use the "
                f"directory {text!r}"
            )
        entries.append(text)
    return ":".join(unique(entries) + [tail])


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


#: A row of the port table in the header comment that bsc writes at the top of a Verilog module:
#: `// <name> <I|O|IO> <size> [props]`.
_PORT_ROW = re.compile(r"^//\s+(\S+)\s+(IO|I|O)\s+\d+(?:\s+(.*?))?\s*$")
#: The ports that bsc names after the clock, the gate of a clock and the reset of the module.
#: A clock or a reset that the module does not use carries no property, so the name tells it.
_CLOCK_OR_RESET_PORT = re.compile(r"^(?:CLK|CLK_GATE|RST_N)(?:_\w+)?$")
#: A member function of the C++ class that bsc writes for a Bluesim module, for one method.
_BLUESIM_METHOD = re.compile(r"\bMETH_(\w+)\s*\(")


def _verilog_method_ports(text: str, reset_prefix: str | None = None) -> list[str] | None:
    """Return the method ports in the port table of a generated Verilog module.

    `text` is the Verilog that bsc generated. The port table is in its header comment, after the
    line `// Ports:`. Each row has a name, the direction (`I`, `O` or `IO`), a size and props.
    Every output and every inout port belongs to a method, whatever its props: an `Empty`
    interface has none. An input port belongs to the clock or the reset if it has the property
    `clock`, `reset`, `gate` or `unused`, or if it has the name that bsc gives a clock, a clock
    gate or a reset. That name is `CLK`, `CLK_GATE` or `RST_N` by default, and `reset_prefix`
    replaces `RST_N` (`-reset-prefix`). An input port that has none of these belongs to a method.
    An input that nothing uses can have a name that the design chose, so `unused` leaves it out.
    Return None if `text` has no port table.
    """
    named = _CLOCK_OR_RESET_PORT
    if reset_prefix:
        named = re.compile(named.pattern + "|" + rf"^{re.escape(reset_prefix)}(?:_\w+)?$")
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if line.strip() == "// Ports:":
            break
    else:
        return None
    rows = lines[number + 1 :]
    if not rows or not re.match(r"^//\s+Name\s+I/O\s+size\s+props\b", rows[0]):
        return None
    ports: list[str] = []
    for row in rows[1:]:
        if not row.startswith("//"):
            break
        found = _PORT_ROW.match(row)
        if found is None:
            continue
        name, direction = found.group(1), found.group(2)
        props = (found.group(3) or "").split()
        if direction == "I" and (
            named.match(name) or {"clock", "reset", "gate", "unused"} & set(props)
        ):
            continue
        ports.append(name)
    return ports


def _bluesim_methods(text: str, module: str) -> list[str] | None:
    """Return the names of the methods in the C++ header that bsc wrote for a Bluesim module.

    Every method has a member function `METH_<name>`, and a method with a ready signal has
    `METH_RDY_<name>` as well. A module with an `Empty` interface has none. Return None if
    `text` does not declare the class of `module`.
    """
    if re.search(rf"\bclass\s+MOD_{re.escape(module)}\b", text) is None:
        return None
    names = unique(_BLUESIM_METHOD.findall(text))
    return [name for name in names if not name.startswith("RDY_")] or names


class BscFlow(Flow, metaclass=ABCMeta):
    """Compile Bluespec with bsc: the settings and the compilation `bsc` and `bsc_sim` share."""

    executables: ClassVar[tuple[str, ...]] = ("bsc",)

    #: The absolute path that each staged name (`bsc_sources/A.bsv`) or folder (`bsc_search/0`)
    #: stands for. `_stage` sets it for the run. The failure text of Xeda shows these paths in
    #: place of bsc's names (`rewrite_paths`). The console and the logs keep bsc's own text.
    _real_paths: Mapping[str, str] = MappingProxyType({})

    artifacts_description = describe_artifacts("log")

    #: The name of the setting for the output directory, which every run empties of generated
    #: modules before it compiles.
    output_dir_setting: ClassVar[str]

    #: With the Verilog backend, `_path_flags` puts the testbench's Verilog directories on
    #: `-vsearch`, so `bsc` reads `tb` as `bsc_sim` does. The Bluesim backend reads no Verilog.
    #: `tb` stays declared there, a safe over-approximation.
    design_parts = frozenset({"rtl", "tb"})

    reads_sources = frozenset(
        {SourceType.Bluespec, SourceType.BluespecHeader, SourceType.CHeader, *_VERILOG_TYPES}
    )

    #: Whether the flow compiles the Bluespec sources of the testbench, besides those of `rtl`.
    #: Both flows stage the Verilog sources of `rtl` and `tb`.
    compiles_tb: ClassVar[bool]

    #: The parts of the design whose `defines` and `parameters` become preprocessor macros
    #: (`_macros`).
    macro_parts: ClassVar[tuple[str, ...]] = ("rtl",)

    class Settings(Flow.Settings):
        removed_settings = {
            **Flow.Settings.removed_settings,
            "cleanup_bobjs": NoReplacement(
                "bsc now compiles every package again in every run, so no stale `.bo` or `.ba` "
                "file is ever reused"
            ),
        }

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
        search_paths: list[Path] = Field(
            [],
            description="Additional directories bsc searches for imported packages (`-p`), after "
            "the design's listed Bluespec sources and before bsc's own libraries, e.g. for "
            "libraries such as bsc-contrib's. Relative to the design root. bsc reads a package "
            "of the design only from a listed source or from one of these directories. bsc "
            "does not see a stray compiled `.bo` file that lies beside its source, or any file "
            "(`.bsv`, `.bs` or `.bo`) named like a package that the design lists. bsc takes "
            "every `.bsv` candidate of a package in all directories before any `.bs` one, and "
            "within one kind, the first directory that holds the package.",
        )
        installed_libraries: list[str] = Field(
            [],
            description="Folders of bsc's own installation that bsc searches for imported "
            "packages (`-p`), given below its `Libraries` folder, e.g. `FPGA/Xilinx` for "
            "bsc-contrib installed into bsc. bsc searches them after `search_paths` and before "
            "its own libraries. A folder that does not exist fails the run, whatever the warning "
            "settings. These files belong to bsc's installation, so a run does not trace them.",
        )
        verilog_search_paths: list[Path] = Field(
            [],
            description="Additional directories searched for the Verilog files of imported "
            '(`import "BVI"`) and library modules (`-vsearch`), before bsc\'s own Verilog '
            "library. bsc searches the design's listed Verilog sources first, then these. "
            "Relative to the design root. A folder that does not exist fails the run. Not used by "
            "Bluesim, which reads no Verilog.",
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

        @field_validator("installed_libraries", mode="after")
        @classmethod
        def _validate_installed_libraries(cls, value: list[str]) -> list[str]:
            for entry in value:
                problem = _installed_library_problem(entry)
                if problem:
                    raise ValueError(f"installed_libraries entry {entry!r} {problem}")
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

    def _sources_of(self, *types: SourceType, tb: bool) -> list[Source]:
        """Return the sources of `types` that this flow reads.

        The RTL sources come first, then the testbench's if `tb` is true.
        """
        return [src for src in self.sources_read(rtl=True, tb=tb) if src.type in types]

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design that bsc would read wrongly.

        Two different listed files must not share a file name in one stage folder, and a package
        must not have both a `.bsv` and a `.bs` file. A Bluespec source needs a `.bsv` or `.bs`
        suffix (`_stage_links` checks all three). A design must not define `BSV_POSITIVE_RESET`
        while `positive_reset` is on: bsc defines the macro itself then, and a design's own
        definition would be overridden without a word."""
        super().check_design_supported(design, settings)
        cls._stage_links(design, tb=cls.compiles_tb, cpp=bool(getattr(settings, "cpp", False)))
        if settings is None or not getattr(settings, "positive_reset", False):
            return
        for part in cls.macro_parts:
            section = getattr(design, part)
            if BSV_POSITIVE_RESET in section.defines or BSV_POSITIVE_RESET in section.parameters:
                raise FlowSettingsException(
                    f"the design defines `{BSV_POSITIVE_RESET}`, which bsc defines itself when "
                    "`positive_reset` is true: remove the define, or set `positive_reset: false`"
                )

    def _bsc_sources(self, *, tb: bool) -> list[Source]:
        """Return the design's Bluespec sources, RTL first. Planning has checked their suffixes
        (`check_design_supported`)."""
        return self._sources_of(SourceType.Bluespec, tb=tb)

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

    def _check_macros(self, sources: list[Source], macros: dict[str, Any]) -> None:
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

    def _warn_inherited_bsc_options(self) -> None:
        """Warn once if the environment of the caller sets `BSC_OPTIONS`. Every bsc process of
        the flow runs with it empty (`BscTool.run`), so bsc ignores the value. The inherited
        environment is freshness evidence, so a change of the value still runs the flow again."""
        if self.env_view.get(BSC_OPTIONS):
            log.warning(
                "The environment sets %s. bsc runs without it, but a change of it still runs the "
                "flow again. Put the flags in the `extra_flags` setting.",
                BSC_OPTIONS,
            )

    def _rel(self, path: str | os.PathLike) -> str:
        """Return `path` as bsc takes it: relative to the run directory, bsc's working directory,
        when `path` lies inside it, else absolute."""
        located = self.run_file(path)
        try:
            return str(located.relative_to(self.run_path))
        except ValueError:
            return str(located)

    def _stage_entries(self, *, tb: bool) -> dict[Path, Path]:
        """Return the links of the stage folders: the name of each link, relative to the run
        directory, and the absolute file it stands for (`_stage_links`).

        Planning already refuses a design whose links collide. This check stays, because the
        design of a run holds the generated sources, which planning only declares."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        return self._stage_links(self.design, tb=tb, cpp=ss.cpp)

    @classmethod
    def _stage_links(cls, design: Design, *, tb: bool, cpp: bool) -> dict[Path, Path]:
        """Return the links of the stage folders for `design`: the name of each link, relative to
        the run directory, and the absolute file it stands for.

        The links are the listed Bluespec sources and Bluespec headers of the parts the flow
        reads (`tb` selects the testbench's) and the listed Verilog sources of both parts. With
        `cpp`, the listed C headers are links too: the C preprocessor reads `#include "x.h"` of
        a BH source in the folder of the source, which is `bsc_sources`. A file
        listed twice is one link. Two different files with one name cannot share a folder, and
        neither can two names that differ only by letter case (on a file system that ignores
        case they would be one file). A package cannot have two files (`P.bsv` and `P.bs`): bsc
        could not tell which to read.
        A Bluespec source needs a `.bsv` or `.bs` suffix. A generated source that is only declared
        (its generator has not run) names no file yet. Its declared name has the suffix checked,
        unless the name is a glob pattern. The check of `run()`, on the design with the generated
        files, covers the rest.
        """
        entries: dict[Path, Path] = {}
        by_case: dict[Path, Path] = {}  # the link, folded to lower case, and the link itself
        packages: dict[str, Path] = {}
        sources = [
            src
            for src in design._sources_of_type("*", rtl=True, tb=tb)
            if src.type in _STAGED_SOURCE_TYPES or (cpp and src.type in _CPP_STAGED_TYPES)
        ]
        sources += [
            src
            for src in design._sources_of_type("*", rtl=True, tb=True)
            if src.type in _VERILOG_TYPES
        ]
        bad = [
            str(src.path)
            for src in sources
            if src.type is SourceType.Bluespec
            # A declared generated source has no file yet: its declared name has the suffix, but
            # a pattern names no one file. The check of `run()` covers the files of a pattern.
            and (src.file is not None or not glob.has_magic(str(src.path)))
            and (src.file or src.path).suffix not in _BSC_SOURCE_SUFFIXES
        ]
        if bad:
            raise FlowSettingsException(
                "bsc compiles BSV from `.bsv` files and BH (Bluespec Classic) from `.bs` files "
                f"only; rename: {', '.join(bad)}"
            )
        for src in sources:
            if src.file is None:
                continue
            link = staged_name(src)
            assert link is not None
            original = Path(os.path.abspath(src.file))
            folded = link.parent / link.name.casefold()
            first = by_case.setdefault(folded, link)
            if first != link:
                raise FlowSettingsException(
                    f"two listed sources have file names that differ only by letter case: "
                    f"{entries[first]} and {original}. They would share the stage folder "
                    f"`{link.parent}`, and a file system that ignores case would make them one "
                    "file: rename one of them"
                )
            if entries.setdefault(link, original) != original:
                raise FlowSettingsException(
                    f"two listed sources have the file name {src.file.name}: "
                    f"{entries[link]} and {original}. They would share the stage folder "
                    f"`{link.parent}`: rename one of them"
                )
            # bsc reads every `.bsv` and `.bs` file of `bsc_sources` as a package, a header too.
            if link.parent == Path(STAGE_SOURCES) and src.file.suffix in _BSC_SOURCE_SUFFIXES:
                other = packages.setdefault(src.file.stem, original)
                if other != original:
                    raise FlowSettingsException(
                        f"two listed sources define the package {src.file.stem}: {other} and "
                        f"{original}. bsc could not tell which one to read: list only one"
                    )
        return entries

    def _stage(self, *, tb: bool) -> None:
        """Replace the stage folders with a link to each listed source (`_stage_entries`).

        bsc reads the design's packages, includes and Verilog modules only through these links
        and the search paths, so it never finds a file that sits beside a source but is not listed.
        """
        entries = self._stage_entries(tb=tb)
        self.run_directory.remove(STAGE_SOURCES, STAGE_VERILOG, STAGE_SEARCH)
        # bsc warns about a search path folder that does not exist, so both folders always do.
        for folder in (STAGE_SOURCES, STAGE_VERILOG):
            self.run_file(folder).mkdir(parents=True, exist_ok=True)
        for link, original in entries.items():
            self.run_file(link).symlink_to(original)
        paths = {str(link): str(original) for link, original in entries.items()}
        paths.update(self._stage_search(tb=tb))
        self._real_paths = paths

    def _stage_search(self, *, tb: bool) -> dict[str, str]:
        """Fill `bsc_search/<i>` with a link to each file of the folder `search_paths[i]`.

        The listing is the one of the trace (`directory_files`): it leaves out the metadata of
        version control at every depth, and the run directory and the run root, however a folder
        reaches them. A subfolder becomes a real folder in the stage, and each file in it a
        link with an absolute target, so bsc finds a package, an include file and a compiled
        `.bo` of a search folder only through these links. In the top level of each folder, left
        out are:

        - a `.bo` file with a `.bsv` or `.bs` file of the same name beside it, because bsc takes
          the newer of the two, and a stray `.bo` can win;
        - every `.bsv`, `.bs` and `.bo` file named like a listed Bluespec source (`Pkg`): the
          listed source in `bsc_sources` is the package.

        These rules apply to the top level only. bsc looks up a package only there, because a
        package name has no folder part, and its check of a source compares the source only with
        the `.bo` beside it. A file in a subfolder is reachable only by an include name with a
        folder part, so it is linked as it is.

        Names compare with letter case ignored (`casefold`), so the result does not depend on
        the file system.

        One INFO line for each folder says what was left out. A folder that does not exist fails
        the run.

        Return the folder that each `bsc_search/<i>` stands for, as an absolute path.
        """
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        listed = {src.file.stem.casefold() for src in self._sources_of(SourceType.Bluespec, tb=tb)}
        prune = [self.run_path]
        if self.run_directory.run_root is not None:
            prune.append(self.run_directory.run_root)
        folders: dict[str, str] = {}
        for index, entry in enumerate(ss.search_paths):
            target = self.run_file(f"{STAGE_SEARCH}/{index}")
            target.mkdir(parents=True, exist_ok=True)
            folder = Path(os.path.abspath(self.normalize_path_to_design_root(entry)))
            if not folder.is_dir():
                raise FlowSettingsException(
                    f"`search_paths` names {str(entry)!r} ({folder}), which is no folder"
                )
            folders[f"{STAGE_SEARCH}/{index}"] = str(folder)
            top = folder.resolve()
            entries = [
                path
                for path in directory_files(folder, prune, VCS_METADATA, follow_links=True)
                if path != top
            ]
            top_names = {
                path.name.casefold() for path in entries if path.parent == top and not path.is_dir()
            }
            hidden: list[str] = []
            for path in entries:
                relative = path.relative_to(top)
                if path.is_dir():
                    (target / relative).mkdir(parents=True, exist_ok=True)
                    continue
                stem = path.stem.casefold()
                suffix = path.suffix.casefold()
                if path.parent == top and (
                    (suffix == ".bo" and {f"{stem}.bsv", f"{stem}.bs"} & top_names)
                    or (suffix in (".bsv", ".bs", ".bo") and stem in listed)
                ):
                    hidden.append(str(relative))
                elif path.is_file():
                    (target / relative).symlink_to(path)
            if hidden:
                log.info(
                    "search_paths[%d] (%s): bsc does not see %s",
                    index,
                    folder,
                    ", ".join(hidden),
                )
        return folders

    def _path_flags(self, backend: str, out_dir: Path, sources: list[Source]) -> list[str]:
        """Return where bsc reads and writes.

        The flags are `-bdir`, `-info-dir`, the output directory, `-p`, `-fdir` and, for Verilog,
        `-vsearch`. A path inside the run directory is relative to it, since bsc runs there. bsc
        finds the design's packages and Verilog modules only in the stage folders (`_stage`)
        and in the search paths of the settings.
        """
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        bdir = self._rel(ss.bobj_dir)
        info_dir = self._rel(ss.info_dir) if ss.info_dir else bdir
        flags = ["-bdir", bdir, "-info-dir", info_dir]
        flags += ["-simdir" if backend == "sim" else "-vdir", self._rel(out_dir)]
        package_dirs = [STAGE_SOURCES] + [
            f"{STAGE_SEARCH}/{i}" for i in range(len(ss.search_paths))
        ]
        installed = [f"%/Libraries/{entry}" for entry in ss.installed_libraries]
        flags += ["-p", ":".join([*package_dirs, *installed, "%/Libraries"])]
        if backend == "sim":
            # Bluesim reads no Verilog (it rejects imported Verilog modules), so it gets no
            # `-vsearch`.
            return flags + self._fdir_flags()
        verilog_dirs: list[str | Path] = [STAGE_VERILOG]
        for entry in ss.verilog_search_paths:
            folder = self.normalize_path_to_design_root(entry)
            if not Path(os.path.abspath(folder)).is_dir():
                raise FlowSettingsException(
                    f"`verilog_search_paths` names {str(entry)!r} ({folder}), which is no folder"
                )
            verilog_dirs.append(folder)
        flags += [
            "-vsearch",
            _bsc_path(
                self._vendor_verilog_dirs() + verilog_dirs, "vsearch", tail="%/Libraries:%/Verilog"
            ),
        ]
        return flags + self._fdir_flags()

    def _resolved_path(self, path_flags: list[str], field: str) -> list[str] | None:
        """Return the search path that bsc resolves for `path_flags`: the entries of the field
        `field` of `-print-flags-raw` (`ifcPath` for `-p`, `vPath` for `-vsearch`), with `%` and
        `+` expanded and the folders that do not exist left out. Return `None` if the output has
        no such field."""
        out = self.bsc.run_get_stdout(*path_flags, "-print-flags-raw") or ""
        match = re.search(rf"^\s*{field}\s*=\s*(\[.*\]),?\s*$", out, re.MULTILINE)
        return _parse_haskell_strings(match.group(1)) if match else None

    def _require_installed_libraries(self, path_flags: list[str]) -> None:
        """Fail the run if bsc left an entry of `installed_libraries` out of its package path.

        bsc drops a `-p` folder that does not exist, with a warning that the warning settings can
        turn off. So the check reads the path that bsc resolved, and no setting can hide the
        failure. The last entry of the resolved path is bsc's own `Libraries` folder."""
        ss = self.settings
        assert isinstance(ss, BscFlow.Settings)
        if not ss.installed_libraries:
            return
        resolved = self._resolved_path(path_flags, "ifcPath")
        if not resolved:
            raise FlowFatalError(
                "cannot read the package search path of bsc from `bsc -print-flags-raw`, so Xeda "
                "cannot check `installed_libraries`"
            )
        libraries = resolved[-1]
        missing = [
            entry for entry in ss.installed_libraries if f"{libraries}/{entry}" not in resolved
        ]
        if missing:
            raise FlowSettingsException(
                f"`installed_libraries` names {', '.join(map(repr, missing))}, which does not "
                f"exist below the `Libraries` folder of bsc ({libraries})"
            )

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
        if self.settings.verbosity == "verbose":
            return ["-verbose"]
        if self.settings.verbosity == "quiet":
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
    def _owned_path_flags(cls) -> dict[str, tuple[str, str]]:
        """Return the path flags of bsc that `extra_flags` must not hold, by the name in bsc's
        flag table: what the flag sets, and how to set it instead."""
        no_setting = "the flow has no setting for it: remove the flag"
        return {
            "p": (
                "the package search path",
                "use `search_paths` for folders of the design, or `installed_libraries` for "
                "folders of bsc's installation",
            ),
            "vsearch": ("the Verilog search path", "use `verilog_search_paths`"),
            "bdir": ("the folder of the compiled packages", "use `bobj_dir`"),
            "info-dir": ("the folder of the informational files", "use `info_dir`"),
            "fdir": ("the folder that file names resolve in", "use `fdir`"),
            "vdir": (
                "the folder of the generated Verilog",
                f"use `{cls.output_dir_setting}`",
            ),
            "simdir": ("the folder of the Bluesim output", no_setting),
            "i": ("the installation folder of bsc", no_setting),
        }

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse the combinations that bsc refuses, before anything runs, and name the settings.

        Also refuse what would let bsc read or write where the flow does not expect it: a path
        flag among `extra_flags` (`_owned_path_flags`), `BSC_OPTIONS` in `env`, and an output
        folder that is the run directory or lies inside a stage folder (`STAGE_FOLDERS`)."""
        super().check_settings_supported(settings)
        assert isinstance(settings, BscFlow.Settings)
        if BSC_OPTIONS in settings.env:
            raise FlowSettingsException(
                f"{cls.name}: `env` sets {BSC_OPTIONS}, which the flow overrides: bsc always runs "
                f"with an empty {BSC_OPTIONS}. Put the flags in `extra_flags`"
            )
        owned = cls._owned_path_flags()
        for word in owned_flag_words(settings.extra_flags, owned):
            meaning, fix = owned[word.lstrip("-")]
            raise FlowSettingsException(
                f"{cls.name}: `extra_flags` has `{word}`, which sets {meaning}. The flow "
                f"owns it: {fix}"
            )
        for setting in ("bobj_dir", "info_dir", cls.output_dir_setting):
            folder = getattr(settings, setting)
            problem = None if folder is None else _stage_folder_conflict(Path(folder))
            if problem:
                raise FlowSettingsException(
                    f"{cls.name}: `{setting}` is {str(folder)!r}, which {problem}. The flow "
                    "fills the stage folders with links, and bsc writes its output elsewhere: "
                    f"give a folder that is none of {', '.join(STAGE_FOLDERS)}"
                )
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

    def _prepare_dirs(self, out_dir: Path) -> None:
        """Create the directories bsc writes to, and remove what an earlier run left in them:
        everything in `bobj_dir`, and every Verilog module (`.v`), `.use` file and VPI wrapper in
        the output directory. bsc then reads and records nothing that an earlier run left, so each
        generated module that the run records is one that this run wrote. Other files stay, and
        the library modules copied in are copied again."""
        assert isinstance(self.settings, BscFlow.Settings)
        if self.settings.verilog_filters and any(c.isspace() for c in str(out_dir)):
            raise FlowSettingsException(
                "bsc runs each `verilog_filters` command on a generated file through a shell, "
                f"without quoting, so the output directory cannot contain whitespace: {out_dir}"
            )
        bdir = self.run_file(self.settings.bobj_dir)
        stale: list[Path] = sorted(bdir.iterdir()) if bdir.is_dir() else []
        for pattern in ("*.v", "*.use", "vpi_wrapper_*"):
            stale += [
                path for path in out_dir.glob(pattern) if not path.is_dir() or path.is_symlink()
            ]
        # These are an earlier run's packages and generated files, all in the run directory.
        self.run_directory.remove(*stale)
        bdir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.info_dir:
            self.run_file(self.settings.info_dir).mkdir(parents=True, exist_ok=True)

    def _compile(
        self,
        backend: str,
        sources: list[Source],
        top: str,
        top_file: Path,
        flags: list[str],
    ) -> None:
        """Compile every Bluespec source in the order given, then generate code for `top` from
        `top_file`, the staged name of its file. Each compile generates code (`-verilog` or
        `-sim`) for the `synthesize`d modules of its package and, with `-u`, compiles the
        packages it imports first."""
        backend_flag = f"-{backend}"
        for src in sources:
            staged = self._staged_source(src)
            if staged != top_file:
                self._run_bsc(COMPILE_LOG, *flags, "-u", backend_flag, staged)
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
            shown = [rewrite_paths(line, self._real_paths) for line in found[:DIAGNOSTICS_SHOWN]]
            more = (
                f" (and {len(found) - len(shown)} more in {log_name})"
                if len(found) > len(shown)
                else ""
            )
            hint = _hint(shown, cpp=bool(getattr(self.settings, "cpp", False)))
            raise error.with_message(f"bsc reported: {' | '.join(shown)}{more}{hint}") from error

    def _top_file(self, sources: list[Source], what: str) -> Path:
        """Return the staged name of the file that holds the top module: the last Bluespec
        source."""
        if not sources:
            raise FlowSettingsException(f"{self.name}: the design has no Bluespec {what} sources")
        return self._staged_source(sources[-1])

    @staticmethod
    def _staged_source(src: Source) -> Path:
        """Return the name of the link to the Bluespec source `src` in `bsc_sources`."""
        staged = staged_name(src)
        assert staged is not None
        return staged


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
    compiles_tb = False

    class Outputs(Flow.Outputs):
        design: DerivedSources | None = Out(
            DESIGN,
            description="The design with the Verilog of the Bluespec sources in place of them: "
            "the files that define the reset macro and the copies from bsc's library, then every "
            "source of `rtl` that is not Bluespec as the design lists it (a Bluespec header is not "
            "handed on: the conversion absorbs it), then the modules bsc "
            "generated, the top module last. The rest of the design is carried.",
        )

    @classmethod
    def design_conversion(cls, settings: Flow.Settings) -> DesignConversion | None:
        """Convert the Bluespec sources into Verilog.

        A Bluespec include file is read by bsc and never handed on."""
        return DesignConversion(
            frozenset({SourceType.Bluespec}),
            frozenset({SourceType.Verilog}),
            absorbs=frozenset({SourceType.BluespecHeader}),
        )

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
        found = self._resolved_path(path_flags, "vPath")
        if found is None:
            log.warning("could not read bsc's Verilog search path from `-print-flags-raw`")
            return []
        return [Path(p) for p in found]

    def run(self) -> None:
        """Compile the Bluespec sources and collect the Verilog files of the top module."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        top = self.design.rtl.top
        assert top
        self._warn_inherited_bsc_options()
        sources = self._bsc_sources(tb=False)
        top_file = self._top_file(sources, "RTL")
        vout_dir = self.run_file(ss.verilog_out_dir)
        log.info("Verilog output directory: %s", vout_dir)
        self._stage(tb=False)
        self._prepare_dirs(vout_dir)

        path_flags = self._path_flags("verilog", vout_dir, sources)
        self._require_installed_libraries(path_flags)
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
        `rtl` that the conversion hands on (`Passed`: neither Bluespec nor a Bluespec header, so
        the design's Verilog and SystemVerilog, VHDL, other headers, constraints, ...), in the
        order of the design. The modules bsc generated come last, each before the module that
        instantiates it and the top module at the end (`Generated`). Each source appears once."""
        conversion = self.design_conversion(self.settings)
        assert conversion is not None
        entries: list[Generated | Passed] = [
            Generated(path, SourceType.Verilog) for path in collected.defines + collected.library
        ]
        entries.extend(
            Passed(index)
            for index, src in enumerate(self.design.rtl.sources)
            if conversion.passes_through(src)
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
    interface (`rtl.top` when the design has no testbench). The run fails before the simulation
    if that module has methods, since nothing calls them and the simulation would not end (a
    Bluesim run with `max_cycles` is bounded and is not checked).
    `simulator` selects Bluesim, the cycle-based simulator of bsc, or a Verilog simulator that
    bsc links with the generated Verilog (`bsc -vsim`). The clock and reset come from the
    `main.v` of bsc.

    Bluesim records the system-task calls in the generated code, and a script of Xeda measures
    the clock cycles and the time. Verilator and Icarus record the system tasks that the linked
    runtime observes. For these three simulators, a run passes only if the testbench finishes
    explicitly (or Bluesim reaches a confirmed `max_cycles` limit) and no event reaches
    `fail_severity`. A fatal error or a nonzero exit status always fails the run. `$finish(n)`
    uses `n` as a verbosity level.

    The design's `defines` and `parameters` are preprocessor macros, and the testbench's go over
    the RTL's. BH (`.bs`) sources see them only through the C preprocessor, with `cpp`.

    The C compiler writes a depfile for each C or C++ source that bsc compiles
    (`import "BDPI"`). Every header it read, nested ones too, is an input of the run, so an edit
    of one runs the flow again. The files of the compiler SDK and of the bsc installation are
    not inputs. With `--remote`, list the headers as `CHeader` sources. The
    console shows the names that bsc uses for the staged sources
    (`bsc_sources/Tb.bsv`), and the failure text of Xeda names the real files.

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
    compiles_tb = True

    cocotb_advice = (
        "bsc_sim runs Bluespec testbenches; for a cocotb testbench, simulate the Verilog that "
        "the `bsc` flow generates with one of these simulators"
    )

    # The link step also compiles in the C and C++ code a testbench imports.
    #: The testbench's macros go over the RTL's (`_macros`).
    macro_parts = ("rtl", "tb")

    reads_sources = frozenset(
        {
            SourceType.Bluespec,
            SourceType.BluespecHeader,
            SourceType.CHeader,
            *_VERILOG_TYPES,
            *_FOREIGN_TYPES,
        }
    )

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

    def ticks_have_no_unit(self) -> bool:
        """Bluesim counts unitless kernel ticks; the Verilog simulators have a time unit."""
        assert isinstance(self.settings, self.Settings)
        return self.settings.simulator == "bluesim"

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
            "also valid. The limit also lets a top module with methods run.",
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
            "(`-parallel-sim-link`). Defaults to `nthreads` when that is set, otherwise 1.",
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
    def _owned_path_flags(cls) -> dict[str, tuple[str, str]]:
        flags = super()._owned_path_flags()
        flags["simdir"] = ("the folder of the Bluesim output", "use `sim_dir`")
        flags["I"] = ("the include path for the C and C++ sources", "use `include_dirs`")
        flags["L"] = ("the library path for the link", "use `library_dirs`")
        return flags

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
        _ = self.bsc  # constructing it checks its version: an old bsc fails before anything runs
        if ss.simulator == "verilator":
            _ = self._verilator()

    def _foreign_sources(self) -> list[Path]:
        """Return the design's C and C++ sources, objects and archives.

        The link step compiles them in for imported C functions (`import "BDPI"`).
        """
        return [src.file for src in self._sources_of(*_FOREIGN_TYPES, tb=True)]

    def _stage_foreign_sources(self) -> list[Path]:
        """Return the files that the link step takes for the design's C and C++ sources,
        objects and archives, after writing a wrapper for each source.

        bsc compiles a C or C++ source to an object beside the name it is given. So the link names
        a wrapper in the run directory: `bsc_foreign/<digest of the source's path>/<the source's
        name>`. The wrapper includes the source by its absolute path, and bsc writes the object
        beside the wrapper, never beside the design's file. Each source has a folder of its own,
        so the objects of `ops.c` and `ops.cpp`, or of two `ops.c`, never share a name. The
        compiler looks for a quoted `#include` first in the folder of the file that holds it, so
        the source finds its headers as before. The link names objects and archives as they are:
        bsc only reads them.
        """
        self.run_directory.remove(_FOREIGN_DIR)
        files = []
        for src in self._sources_of(*_FOREIGN_TYPES, tb=True):
            if src.type is SourceType.ObjectFile:
                files.append(src.file)
                continue
            source = Path(os.path.abspath(src.file))
            digest = hashlib.sha256(str(source).encode()).hexdigest()[:12]
            wrapper = self.run_directory.writable(Path(_FOREIGN_DIR, digest, source.name))
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            with replacing_file(wrapper, encoding="utf-8") as f:
                f.write(f'#include "{source}"\n')
            files.append(wrapper)
        return files

    def _record_foreign_depfiles(self, link_files: Sequence[Path]) -> None:
        """Register the depfile of each C and C++ source that the link compiled.

        The compiler writes the depfile beside the object (`ops.o`, `ops.d`) and lists every
        header that it read, including a header that another header includes. The launcher
        reads the depfiles after the run, so an edit of any of these headers runs the flow
        again. bsc compiles a source only if the design imports a function from it, so a source
        with no object has no depfile and needs none. An object with no depfile from this run
        fails the run: the headers of that source would go unrecorded.
        """
        foreign = self.run_path / _FOREIGN_DIR
        for wrapper in link_files:
            if not wrapper.is_relative_to(foreign) or wrapper.suffix == ".o":
                continue
            if not wrapper.with_suffix(".o").is_file():
                continue
            depfile = self.report_file(wrapper.with_suffix(".d"))
            if depfile is None:
                raise FlowFatalError(
                    f"bsc compiled {wrapper.name} but its compiler wrote no depfile "
                    f"{self.run_file(wrapper.with_suffix('.d'))} in this run, so the headers that "
                    "the source includes cannot be tracked. An option in `c_flags` or `cxx_flags` "
                    "(for example `-MF`) may have written the depfile elsewhere."
                )
            self.depfiles.append(depfile)

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
        self._warn_inherited_bsc_options()
        bluesim = ss.simulator == "bluesim"
        backend = "sim" if bluesim else "verilog"
        sources = self._bsc_sources(tb=True)
        tb_sources = [src for src in sources if src in self.design.tb.sources]
        top_file = self._top_file(tb_sources or sources, "testbench")
        self._stage(tb=True)
        sim_dir = self.run_file(ss.sim_dir)
        # bsc's Verilog link searches `sim_dir` first, so a module that an earlier run generated
        # there must not outlive that run.
        self._prepare_dirs(sim_dir)
        if bluesim:
            # bsc otherwise reuses C++ objects built with an earlier configuration's flags.
            self.run_directory.remove(
                *sim_dir.glob("*.o"),
                *sim_dir.glob("*.so"),
            )

        path_flags = self._path_flags(backend, sim_dir, sources)
        self._require_installed_libraries(path_flags)
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
        link_files = self._stage_foreign_sources()
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
            build_end_monitor(self, self.bsc)
        _check_link_paths([*path_flags, *link_flags, executable, *link_files])
        self._compile(backend, sources, tb_top, top_file, flags)
        if not bluesim:
            self._require_empty_interface(tb_top, sim_dir, bluesim)

        self._run_bsc(
            LINK_LOG,
            *self._runtime_flags(),
            *path_flags,
            *self._verbosity_flags(),
            f"-{backend}",
            "-e",
            tb_top,
            "-o",
            self._rel(executable),
            *link_flags,
            *macro_flags,
            *link_files,
            env=link_env,
        )
        self._record_foreign_depfiles(link_files)
        self.artifacts.executable = str(executable)
        if bluesim:
            self._require_empty_interface(tb_top, sim_dir, bluesim)

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
            sim_args = ["-i", "-n", "-m", MONITOR, str(executable), *sim_args]
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
                    timeout=ss.timeout_in_seconds(),
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

    def _require_empty_interface(self, top: str, sim_dir: Path, bluesim: bool) -> None:
        """Fail the run if the module to simulate has methods, before the simulation starts.

        The clock and the reset come from the harness of bsc, which runs until the module calls
        `$finish`. Nothing calls the methods of the simulated module, so the simulation would
        not end. The check reads what this run's bsc wrote for the module: the C++ header of
        Bluesim, or the header comment of the generated Verilog. A missing file or a missing
        section fails the run, as the output of bsc may have changed. Two cases skip the check:
        a Bluesim run bounded by `max_cycles` ends anyway, and a `verilog_filters` command may
        have removed the port table.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if bluesim and ss.max_cycles is not None:
            return
        path = sim_dir / (f"{top}.h" if bluesim else f"{top}.v")
        report = self.report_file(path)
        if report is None:
            raise FlowFatalError(
                f"bsc_sim cannot check that `{top}` has an `Empty` interface: this run's bsc "
                f"did not write {self.run_file(path)}."
            )
        text = report.read_text(encoding="utf-8", errors="replace")
        found = (
            _bluesim_methods(text, top) if bluesim else _verilog_method_ports(text, ss.reset_prefix)
        )
        if found is None:
            if not bluesim and ss.verilog_filters:
                log.warning(
                    "The Empty-interface check is skipped: the generated Verilog of `%s` has no "
                    "port table, which a `verilog_filters` command may have removed.",
                    top,
                )
                return
            raise FlowFatalError(
                f"bsc_sim cannot check that `{top}` has an `Empty` interface: bsc wrote no "
                f"{'class' if bluesim else 'port table'} for it in {self.run_file(path)}."
            )
        if not found:
            return
        fallback = (
            ""
            if self.design.tb.top
            else " The design has no testbench, so `rtl.top` is the simulated module."
        )
        advice = "Simulate a testbench module with an `Empty` interface (set `tb.top`)"
        advice += ", or set `max_cycles` to bound the Bluesim run." if bluesim else "."
        raise FlowFatalError(
            f"bsc_sim cannot simulate `{top}`: it has {'methods' if bluesim else 'method ports'} "
            f"({', '.join(found)}).{fallback} bsc drives the clock and the reset of the module "
            "until the module calls `$finish`, and nothing calls its methods, so the simulation "
            f"would not end. {advice}"
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
        tool = ModelsimTool().derive("vsim", log_key="simulation_log")
        tool.run(
            "-batch",
            "-logfile",
            "modelsim_runtime.log",
            "-do",
            "do " + script.name,
            timeout=ss.timeout_in_seconds(),
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
        simulation.run(
            *sim_args,
            "-ucli",
            "-i",
            str(script),
            timeout=ss.timeout_in_seconds(),
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
            timeout=ss.timeout_in_seconds(),
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
                    files(impl_module("flows.verilator")).joinpath("templates/" + name).read_text()
                )
        obj = self.run_directory.writable("bsc_verilator_hooks.o")
        self.run_directory.remove(obj)
        self.bsc.derive("c++", version_flag=None, minimum_version=None).run(
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
            # Each C and C++ source of the design then gets a depfile of the headers it read
            # (`_record_foreign_depfiles`).
            *_DEPFILE_FLAGS,
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
