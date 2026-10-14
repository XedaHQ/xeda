"""Ordered nextpnr constraints, and a reader for their clock commands that evaluates no Tcl."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..flow import FlowFatalError, PhysicalClock


@dataclass(frozen=True)
class SourceLine:
    """The file or setting, and the line in it, that a line of the merged text came from."""

    source: str
    line: int

    def __str__(self) -> str:
        return f"{self.source}:{self.line}"


@dataclass(frozen=True)
class Constraints:
    """Constraint text with the origin of each line, generated clock lines included."""

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
        """Add the original location to each line number that nextpnr names in the merged text."""

        def original(match: re.Match[str]) -> str:
            line = int(match.group(1))
            origin = self.lines[line - 1] if 0 < line <= len(self.lines) else None
            return f"{match.group(0)} ({origin})" if origin else match.group(0)

        return re.sub(r"\bline\s+(\d+)\b", original, message, flags=re.IGNORECASE)


@dataclass(frozen=True)
class ClockUse:
    """A physical clock that a launch constrains: its port or net name and its origin."""

    target: str
    kind: str  # "port" or "net"
    origin: str


def merge_constraints(paths: Sequence[Path]) -> Constraints:
    """Join the files in the order given, adding a newline after a file that lacks one."""
    merged = Constraints()
    for path in paths:
        try:
            merged = merged.append(path.read_text(), str(path))
        except (OSError, UnicodeError) as e:
            raise FlowFatalError(f"Cannot read constraints {path}: {e}") from e
    return merged


def _commands(text: str):
    """Yield each command of the text with the line where it starts.

    A command ends at a newline or a semicolon outside braces, brackets and quotes. A `#` at the
    start of a command begins a comment (a Tcl comment, or a full-line comment of an LPF file).
    A backslash before a newline joins two lines and keeps the line count right.
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
    """Split a command into words. Braces, brackets and quotes group a word and stay in it."""
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
    """Refuse a clock that is constrained twice, and add constraints for the clocks of the settings.

    Return the pin constraints, the frequency hint, and whether any physical timing constraint
    exists. For Xilinx, the pin constraints get a `create_clock` line for each clock of the
    settings. For ECP5, iCE40 and Nexus, the hint is the frequency of the main clock, in MHz. A
    clock setting without a port is refused. Each clock found, in the settings or in a file, is
    appended to `uses` when the caller gives that list.
    """
    nets = _net_bits(netlist, top)
    seen: dict[int | str, str] = {}
    has_timing = False

    def register(target: str, kind: str, origin: str):
        nonlocal has_timing
        # A name or an indexed port is literal. A Tcl substitution or a wildcard needs a Tcl
        # interpreter to resolve, so the selector is refused.
        if not target or any(c in target for c in "*$?") or target.startswith("["):
            raise FlowFatalError(
                f"Ambiguous clock selector {target!r} at {origin}; use a literal unambiguous port constraint."
            )
        bits = nets.get(target)
        if kind == "net" and bits is None:
            setting_clocks = ", ".join(
                f"{name} ({clock.port}, {clock.period})" for name, clock in clocks.items()
            )
            raise FlowFatalError(
                f"Unresolved NET clock {target!r} at {origin}. It may overlap the clocks {setting_clocks or 'in other files'}. Use a literal unambiguous PORT constraint."
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
        if not clock.port:
            raise FlowFatalError(f"Clock setting clocks.{name} ({clock.period}) needs a port.")
        register(
            clock.port,
            "port",
            f"clock setting clocks.{name} (port {clock.port!r}, period {clock.period})",
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
            assert clock.port is not None
            port = clock.port
            # The braces around the port name keep bus brackets out of Tcl substitution.
            # When the name has a space, an inner pair of braces keeps it one list element.
            if any(c.isspace() for c in port):
                port = "{" + port + "}"
            pins = pins.append(
                # no `-name`: the backend ignores it, with a warning
                f"create_clock -period {clock.period.to('ns')} [get_ports {{{port}}}]\n",
                f"clock setting clocks.{name} (period {clock.period})",
            )
        return pins, None, has_timing
    frequency = main_clock.freq.to("MHz") if main_clock is not None else None
    return pins, frequency, has_timing
