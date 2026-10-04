"""Ordered nextpnr constraints and a focused, non-evaluating clock syntax boundary."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..flow import FlowFatalError, PhysicalClock
from ..utils import tcl_word


@dataclass(frozen=True)
class SourceLine:
    """The original file/setting and line corresponding to a merged line."""

    source: str
    line: int

    def __str__(self) -> str:
        return f"{self.source}:{self.line}"


@dataclass(frozen=True)
class Constraints:
    """Constraint text with one origin per physical line, including generated clocks."""

    text: str = ""
    lines: tuple[SourceLine, ...] = ()

    def append(self, text: str, source: str) -> "Constraints":
        if not text:
            return self
        text = text if text.endswith("\n") else text + "\n"
        return Constraints(
            self.text + text,
            self.lines + tuple(SourceLine(source, i) for i in range(1, len(text.splitlines()) + 1)),
        )

    def diagnostic(self, message: str) -> str:
        """Annotate nextpnr's merged-file line references with their original locations."""

        def original(match: re.Match[str]) -> str:
            line = int(match.group(1))
            origin = self.lines[line - 1] if 0 < line <= len(self.lines) else None
            return f"{match.group(0)} ({origin})" if origin else match.group(0)

        return re.sub(r"\bline\s+(\d+)\b", original, message, flags=re.IGNORECASE)


@dataclass(frozen=True)
class ClockUse:
    """One physical clock a launch constrains: its literal target and where it was given."""

    target: str
    kind: str  # "port" or "net"
    origin: str


def merge_constraints(paths: Sequence[Path]) -> Constraints:
    """Concatenate files in supplied order, inserting a newline at each file boundary."""
    merged = Constraints()
    for path in paths:
        try:
            merged = merged.append(path.read_text(), str(path))
        except (OSError, UnicodeError) as e:
            raise FlowFatalError(f"Cannot read constraints {path}: {e}") from e
    return merged


def _commands(text: str):
    """Split commands outside grouped words, retaining physical starting lines.

    Tcl comments begin at a command boundary. LPF also accepts full-line comments.
    Continuations are whitespace, without losing the original line count.
    """
    start = line = 1
    braces = brackets = 0
    quoted = False
    word = ""
    i = 0
    while i < len(text):
        c = text[i]
        if not word.strip() and c == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if c == "\\" and i + 1 < len(text):
            following = text[i + 1]
            word += " " if following == "\n" else c + following
            line += following == "\n"
            i += 2
            continue
        if c == "\n":
            line += 1
        if c == '"' and not braces:
            quoted = not quoted
        elif c == "{" and not quoted:
            braces += 1
        elif c == "}" and not quoted:
            braces -= 1
        elif c == "[" and not braces:
            brackets += 1
        elif c == "]" and not braces:
            brackets -= 1
        if c in ";\n" and not (braces or brackets or quoted):
            if word.strip():
                yield start, word.strip()
            word = ""
            start = line
        else:
            if not word.strip() and not c.isspace():
                start = line
            word += c
        i += 1
    if word.strip():
        yield start, word.strip()


def _words(command: str) -> list[str]:
    """Tokenize grouped literal words, keeping delimiters for selector/list recognition."""
    words = []
    current = ""
    braces = brackets = 0
    quoted = False
    escaped = False
    for c in command:
        if escaped:
            current += c
            escaped = False
            continue
        if c == "\\":
            escaped = True
            continue
        if c == '"' and not braces:
            quoted = not quoted
        elif c == "{" and not quoted:
            braces += 1
        elif c == "}" and not quoted:
            braces -= 1
        elif c == "[" and not braces:
            brackets += 1
        elif c == "]" and not braces:
            brackets -= 1
        if c.isspace() and not (braces or brackets or quoted):
            if current:
                words.append(current)
                current = ""
        else:
            current += c
    if current:
        words.append(current)
    return words


def _literal(word: str) -> str:
    return word[1:-1] if word.startswith(("{", '"')) and word.endswith(("}", '"')) else word


def _net_bits(netlist: Path, top: str) -> dict[str, tuple[int, ...]]:
    try:
        module = json.loads(netlist.read_text()).get("modules", {}).get(top, {})
    except (OSError, ValueError) as e:
        raise FlowFatalError(f"Cannot read clock aliases from netlist {netlist}: {e}") from e
    nets = {}
    for section in ("netnames", "ports"):
        for name, value in module.get(section, {}).items():
            bits = value.get("bits", [])
            if bits and all(isinstance(bit, int) for bit in bits):
                nets[name] = tuple(bits)
                offset = value.get("offset", 0)
                upto = value.get("upto", 0)
                for i, bit in enumerate(bits):
                    index = offset + (len(bits) - 1 - i if upto else i)
                    nets[f"{name}[{index}]"] = (bit,)
    return nets


def reconcile_clocks(
    pins: Constraints,
    sdc: Constraints,
    clocks: Mapping[str, PhysicalClock],
    *,
    family: str,
    netlist: Path,
    top: str,
    main_clock: PhysicalClock | None = None,
    uses: list[ClockUse] | None = None,
) -> tuple[Constraints, float | None, bool]:
    """Reject overlapping clock authorities and generate the selected family's timing form.

    Returns the pin constraints (including Xilinx generated clocks), the ECP5/iCE40/Nexus
    main-clock frequency hint, and whether any physical timing constraint was provided.
    Port declarations without a period do not supply timing. Every clock found, in the
    settings or a file, is appended to ``uses`` when the caller supplies that list.
    """
    nets = _net_bits(netlist, top)
    seen: dict[int | str, str] = {}
    has_timing = False

    def register(target: str, kind: str, origin: str):
        nonlocal has_timing
        # Direct names and indexed ports are literal; dynamic Tcl and wildcard selectors
        # cannot be reconciled without evaluating a different language.
        if not target or any(c in target for c in "*$?") or target.startswith("["):
            raise FlowFatalError(
                f"Ambiguous clock selector {target!r} at {origin}; use a literal unambiguous port constraint."
            )
        bits = nets.get(target)
        if kind == "net" and bits is None:
            setting_clocks = ", ".join(
                f"{name} ({clock.port}, {clock.period} ns)"
                for name, clock in clocks.items()
                if clock.period
            )
            raise FlowFatalError(
                f"Unresolved NET clock {target!r} at {origin}; may overlap clocks {setting_clocks or 'in other files'}. Use a literal unambiguous PORT constraint."
            )
        identities: Sequence[int | str] = bits if bits else (target,)
        for identity in identities:
            if identity in seen:
                raise FlowFatalError(
                    f"Duplicate clock on {target!r}: {seen[identity]} and {origin}."
                )
            seen[identity] = origin
        has_timing = True
        if uses is not None:
            uses.append(ClockUse(target, kind, origin))

    for name, clock in clocks.items():
        if clock.period is not None:
            if not clock.port:
                raise FlowFatalError(
                    f"Clock setting clocks.{name} ({clock.period} ns) needs a port."
                )
            register(
                clock.port,
                "port",
                f"clock setting clocks.{name} (port {clock.port!r}, period {clock.period} ns)",
            )

    for merged in (pins, sdc):
        for line, command in _commands(merged.text):
            words = _words(command)
            if not words:
                continue
            origin = str(merged.lines[line - 1])
            if words[0].upper() == "FREQUENCY" and len(words) >= 5:
                register(_literal(words[2]), words[1].lower(), f"{origin} ({' '.join(words[3:])})")
            elif words[0] == "create_clock":
                period = next(
                    (
                        _literal(words[i + 1])
                        for i, word in enumerate(words[:-1])
                        if word == "-period"
                    ),
                    None,
                )
                selector = words[-1]
                selected = (
                    _words(selector[1:-1])
                    if selector.startswith("[") and selector.endswith("]")
                    else []
                )
                if len(selected) != 2 or selected[0] not in ("get_ports", "get_nets"):
                    raise FlowFatalError(
                        f"Ambiguous create_clock selector at {origin}; use literal [get_ports {{port}}] or [get_nets {{net}}]."
                    )
                for target in _words(_literal(selected[1])):
                    register(
                        _literal(target),
                        "net" if selected[0] == "get_nets" else "port",
                        f"{origin} (create_clock period {period} ns)",
                    )

    if family == "xilinx":
        for name, clock in clocks.items():
            if clock.period is not None:
                assert clock.port is not None
                port = clock.port
                # A braced literal selector keeps bus brackets out of Tcl substitution.
                # An additional list-word group preserves spaces in one port name.
                if any(c.isspace() for c in port):
                    port = "{" + port + "}"
                pins = pins.append(
                    f"create_clock -name {tcl_word(name)} -period {clock.period} [get_ports {{{port}}}]\n",
                    f"clock setting clocks.{name} (period {clock.period} ns)",
                )
        return pins, None, has_timing
    frequency = main_clock.freq_mhz if main_clock is not None and main_clock.period else None
    return pins, frequency, has_timing
