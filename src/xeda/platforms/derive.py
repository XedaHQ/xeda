"""Facts about a platform that the LEF and Liberty files of its PDK state.

The files of a PDK say which site the core cells use, which cells tie a net to 1 or to 0, which
cell is the antenna diode, which pins carry the supply, which cells a library marks `dont_use`,
where the routing tracks are, and which units the library uses. `derive` collects these facts so
that a residue file need not repeat them.

The derivation is pure Python. It reads each file once as text (or through `gzip` when the name
ends in `.gz`), writes nothing and starts no process. It reads a file in one pass with no
dependence on the tools that write it. A byte that is not UTF-8 reads as the replacement
character, since such bytes occur in the comments of PDK files only. A fact that the files do not
state gives no key, and the derivation never guesses.
"""

import gzip
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

__all__ = [
    "LefLayer",
    "LefMacro",
    "LefPin",
    "LefSummary",
    "LibertyCell",
    "LibertyPin",
    "LibertySummary",
    "derive",
    "read_lef_summary",
    "read_liberty_summary",
]

_INFINITY = float("inf")


def _read_text(path: Path) -> str:
    """Return the text of a file, which `gzip` unpacks when the name ends in `.gz`."""
    try:
        if path.suffix.lower() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
                return stream.read()
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError(f"cannot read the PDK file {path}: {error}") from None


def _number(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# LEF
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LefPin:
    """A pin of a macro."""

    direction: Optional[str]
    use: Optional[str]
    #: The largest `ANTENNADIFFAREA` of the pin; `None` when the pin states none.
    antenna_diff_area: Optional[float]


@dataclass(frozen=True)
class LefMacro:
    """A macro (a cell) of a LEF file."""

    name: str
    #: The words after `CLASS`, for example `("CORE", "ANTENNACELL")`.
    cls: tuple[str, ...]
    #: Width and height in micrometers.
    size: Optional[tuple[float, float]]
    site: Optional[str]
    pins: dict[str, LefPin]

    @property
    def area(self) -> float:
        return self.size[0] * self.size[1] if self.size else _INFINITY


@dataclass(frozen=True)
class LefLayer:
    """A routing layer. `pitch` and `offset` are the `(x, y)` values: a single value in the file
    gives both directions."""

    name: str
    direction: Optional[str]
    pitch: Optional[tuple[float, float]]
    offset: Optional[tuple[float, float]]


@dataclass
class LefSummary:
    """What the derivation needs from the LEF files."""

    #: Each site that a file declares or a macro uses, with the number of macros on it.
    sites: dict[str, int] = field(default_factory=dict)
    macros: dict[str, LefMacro] = field(default_factory=dict)
    layers: list[LefLayer] = field(default_factory=list)


# Blocks of the top level that the summary skips, with the word that ends each: a name for the
# blocks that have one, else the keyword.
_NAMED_BLOCKS = ("VIA", "VIARULE", "NONDEFAULTRULE")
_KEYWORD_BLOCKS = ("UNITS", "PROPERTYDEFINITIONS", "SPACING")


def _pair(words: list[str]) -> Optional[tuple[float, float]]:
    """The `(x, y)` of one or two numbers: one number stands for both."""
    values = [_number(word) for word in words]
    if any(value is None for value in values) or len(values) not in (1, 2):
        return None
    first = values[0]
    second = values[-1]
    assert first is not None and second is not None
    return (first, second)


class _LefReader:
    """A line-based reader of LEF text. It keeps the macros, the routing layers and the sites."""

    def __init__(self, summary: LefSummary) -> None:
        self.summary = summary
        self.declared_sites: list[str] = []
        self.where = "top"  # top, skip, layer, site, macro, pin, port, obs
        self.skip_end: tuple[str, ...] = ()
        self.layer: dict[str, Any] = {}
        self.layer_name = ""
        self.site_name = ""
        self.macro: dict[str, Any] = {}
        self.pin_name = ""
        self.pin: dict[str, Any] = {}

    #: The words that open or close a block in each state. A line that starts with one of them
    #: holds a whole statement, since such a line has no `;`.
    _BLOCK_WORDS = {
        "top": (
            "LAYER",
            "MACRO",
            "SITE",
            "VIA",
            "VIARULE",
            "NONDEFAULTRULE",
            "UNITS",
            "PROPERTYDEFINITIONS",
            "SPACING",
            "END",
        ),
        "macro": ("PIN", "OBS", "END"),
        "pin": ("PORT", "END"),
        "layer": ("END",),
        "site": ("END",),
        "skip": ("END",),
    }

    def read(self, text: str) -> None:
        """Read the statements. One ends with `;` and can span lines. A line that opens or closes
        a block (`MACRO <name>`, `END <name>`) has no `;` and stands for itself."""
        pending: list[str] = []
        for line in text.splitlines():
            hash_at = line.find("#")
            if hash_at >= 0:
                line = line[:hash_at]
            if self.where in ("port", "obs") and not pending:
                if "END" in line and line.split() == ["END"]:
                    self.where = "pin" if self.where == "port" else "macro"
                continue
            fragments = line.split(";")
            for index, fragment in enumerate(fragments):
                words = fragment.split()
                if index < len(fragments) - 1:
                    pending.extend(words)
                    if pending:
                        self.statement(pending)
                        pending = []
                elif words and not pending and words[0] in self._BLOCK_WORDS.get(self.where, ()):
                    self.statement(words)
                else:
                    pending.extend(words)

    def statement(self, words: list[str]) -> None:
        handler = getattr(self, f"in_{self.where}")
        handler(words[0], words)

    # -- the top level and the blocks the summary skips ----------------------------------------

    def in_top(self, key: str, words: list[str]) -> None:
        name = words[1] if len(words) > 1 else ""
        if key == "LAYER" and name:
            self.where, self.layer_name, self.layer = "layer", name, {}
        elif key == "MACRO" and name:
            self.where, self.macro = "macro", {"name": name, "pins": {}}
        elif key == "SITE" and name:
            self.where = "site"
            self.declared_sites.append(name)
            self.site_name = name
        elif key in _NAMED_BLOCKS and name:
            self.where, self.skip_end = "skip", ("END", name)
        elif key in _KEYWORD_BLOCKS and len(words) == 1:
            self.where, self.skip_end = "skip", ("END", key)

    def in_skip(self, key: str, words: list[str]) -> None:
        if tuple(words[:2]) == self.skip_end:
            self.where = "top"

    def in_site(self, key: str, words: list[str]) -> None:
        if key == "END" and words[1:2] == [self.site_name]:
            self.where = "top"

    # -- a layer -------------------------------------------------------------------------------

    def in_layer(self, key: str, words: list[str]) -> None:
        if key == "END" and words[1:2] == [self.layer_name]:
            self.where = "top"
            if self.layer.get("type") == "ROUTING":
                layer = LefLayer(
                    self.layer_name,
                    self.layer.get("direction"),
                    self.layer.get("pitch"),
                    self.layer.get("offset"),
                )
                layers = self.summary.layers
                for index, known in enumerate(layers):
                    if known.name == layer.name:
                        layers[index] = layer
                        break
                else:
                    layers.append(layer)
        elif key == "TYPE" and len(words) > 1:
            self.layer["type"] = words[1]
        elif key == "DIRECTION" and len(words) > 1:
            self.layer["direction"] = words[1]
        elif key in ("PITCH", "OFFSET"):
            pair = _pair(words[1:])
            if pair is not None:
                self.layer[key.lower()] = pair

    # -- a macro -------------------------------------------------------------------------------

    def in_macro(self, key: str, words: list[str]) -> None:
        macro = self.macro
        if key == "CLASS":
            macro["cls"] = tuple(words[1:])
        elif key == "SIZE" and len(words) == 4 and words[2] == "BY":
            width, height = _number(words[1]), _number(words[3])
            if width is not None and height is not None:
                macro["size"] = (width, height)
        elif key == "SITE" and len(words) > 1:
            macro["site"] = words[1]
        elif key == "PIN" and len(words) > 1:
            self.where, self.pin_name = "pin", words[1]
            self.pin = {}
        elif key == "OBS":
            self.where = "obs"
        elif key == "END" and words[1:2] == [macro["name"]]:
            self.where = "top"
            self.summary.macros[macro["name"]] = LefMacro(
                name=macro["name"],
                cls=macro.get("cls", ()),
                size=macro.get("size"),
                site=macro.get("site"),
                pins=macro["pins"],
            )

    def in_pin(self, key: str, words: list[str]) -> None:
        if key == "DIRECTION" and len(words) > 1:
            self.pin["direction"] = words[1]
        elif key == "USE" and len(words) > 1:
            self.pin["use"] = words[1]
        elif key == "ANTENNADIFFAREA" and len(words) > 1:
            area = _number(words[1])
            if area is not None:
                self.pin["area"] = max(area, self.pin.get("area", area))
        elif key == "PORT":
            self.where = "port"
        elif key == "END" and words[1:2] == [self.pin_name]:
            self.where = "macro"
            self.macro["pins"][self.pin_name] = LefPin(
                self.pin.get("direction"), self.pin.get("use"), self.pin.get("area")
            )

    def finish(self) -> None:
        for name in self.declared_sites:
            self.summary.sites.setdefault(name, 0)


def read_lef_summary(paths: Iterable[Path]) -> LefSummary:
    """Read LEF files and return their sites, macros and routing layers.

    A tech LEF and a cell LEF are both fine, and so is one file that holds both. Layers and
    macros of the later file replace those of the same name from an earlier file. The count of a
    site is the number of macros on it."""
    summary = LefSummary()
    for path in paths:
        reader = _LefReader(summary)
        reader.read(_read_text(path))
        reader.finish()
    counts = Counter(macro.site for macro in summary.macros.values() if macro.site is not None)
    summary.sites.update(counts)
    return summary


# ---------------------------------------------------------------------------------------------
# Liberty
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibertyPin:
    """A pin (or a bus) of a cell."""

    name: str
    direction: Optional[str]
    function: Optional[str]


@dataclass(frozen=True)
class LibertyCell:
    """A cell of a Liberty file."""

    name: str
    area: Optional[float]
    dont_use: bool
    pins: tuple[LibertyPin, ...]


@dataclass
class LibertySummary:
    """What the derivation needs from a Liberty file."""

    cells: dict[str, LibertyCell] = field(default_factory=dict)
    nom_voltage: Optional[float] = None
    #: The time unit as the file writes it, for example `1ns`.
    time_unit: Optional[str] = None
    #: The capacitance unit, written as the number and the unit, for example `1ff`.
    capacitive_load_unit: Optional[str] = None


# One statement or group header of Liberty: the text up to the next `{`, `}` or `;` outside a
# quoted string. Possessive quantifiers keep the match linear.
_STATEMENT = re.compile(r'(?:[^{};"]++|"[^"]*+")*+[{};]')
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CALL = re.compile(r"\s*([A-Za-z_][\w.$]*)\s*\((.*)\)\s*", re.DOTALL)
_ATTRIBUTE = re.compile(r"\s*([A-Za-z_][\w.$]*)\s*:\s*(.*?)\s*", re.DOTALL)
_PIN_GROUPS = ("pin", "bus", "bundle")
_NAME = re.compile(r'"([^"]*)"|([^,"]+)')


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].strip()
    return text


def _names(arguments: str) -> list[str]:
    """The names in the arguments of a group, quoted or not: `A, "B"` gives `A` and `B`. A group
    without a name gives one empty name."""
    names = [(quoted or bare).strip() for quoted, bare in _NAME.findall(arguments)]
    return [name for name in names if name] or [""]


def _capacitance_unit(arguments: str) -> Optional[str]:
    """Return `1ff` for the arguments `(1,ff)` of `capacitive_load_unit`."""
    parts = [_unquote(part) for part in arguments.split(",")]
    if len(parts) != 2 or not parts[1]:
        return None
    value = _number(parts[0])
    return None if value is None else f"{value:g}{parts[1]}"


def _liberty_statements(text: str) -> Iterator[tuple[str, str]]:
    for match in _STATEMENT.finditer(text):
        chunk = match.group()
        yield chunk[:-1], chunk[-1]


def read_liberty_summary(path: Path) -> LibertySummary:
    """Read a Liberty file (plain or `.gz`) and return its cells and header values.

    The reader goes through the groups by depth and looks only at the attributes of the library,
    of its cells and of the pins of the cells. It skips the timing data of a cell."""
    text = _COMMENT.sub(" ", _read_text(path)).replace("\\\n", " ")
    summary = LibertySummary()
    stack: list[str] = []
    cell: dict[str, Any] = {}
    pin: dict[str, Any] = {}

    def attribute(body: str) -> None:
        """Handle one statement `name : value` or `name (arguments)` at its depth."""
        depth = len(stack)
        match = _ATTRIBUTE.fullmatch(body)
        if match is None:
            call = _CALL.fullmatch(body)
            if call and depth == 1 and call.group(1) == "capacitive_load_unit":
                summary.capacitive_load_unit = _capacitance_unit(call.group(2))
            return
        name, value = match.group(1), _unquote(match.group(2))
        if depth == 1:
            if name == "nom_voltage":
                summary.nom_voltage = _number(value)
            elif name == "time_unit":
                summary.time_unit = value
        elif depth == 2 and stack[1] == "cell":
            if name == "area":
                cell["area"] = _number(value)
            elif name == "dont_use":
                cell["dont_use"] = value.lower() == "true"
        elif depth == 3 and stack[1] == "cell" and stack[2] in _PIN_GROUPS:
            if name == "direction":
                pin["direction"] = value.lower()
            elif name == "function":
                pin["function"] = value

    for body, end in _liberty_statements(text):
        depth = len(stack)
        if end == ";":
            if depth <= 3 and body.strip():
                attribute(body)
            continue
        if end == "}":
            if depth <= 3 and body.strip():
                attribute(body)
            if not stack:
                continue
            closed = stack.pop()
            if depth == 2 and closed == "cell" and stack[0] == "library":
                summary.cells.setdefault(
                    cell["name"],
                    LibertyCell(
                        cell["name"],
                        cell.get("area"),
                        cell.get("dont_use", False),
                        tuple(cell["pins"]),
                    ),
                )
            elif depth == 3 and closed in _PIN_GROUPS and stack[1] == "cell":
                cell["pins"].extend(
                    LibertyPin(name, pin.get("direction"), pin.get("function"))
                    for name in pin["names"]
                )
            continue
        call = _CALL.fullmatch(body)
        group = call.group(1) if call else ""
        stack.append(group)
        if depth == 1 and group == "cell" and stack[0] == "library":
            cell = {"name": _unquote(call.group(2)) if call else "", "pins": []}
        elif depth == 2 and group in _PIN_GROUPS and stack[1] == "cell":
            pin = {"names": _names(call.group(2)) if call else [""]}
    return summary


# ---------------------------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------------------------


def _most_common(names: Iterable[str]) -> Optional[str]:
    """The name that occurs most often; a tie goes to the name that sorts first."""
    counts = Counter(names)
    return min(counts, key=lambda name: (-counts[name], name)) if counts else None


def _site(lef: LefSummary) -> Optional[str]:
    return _most_common(macro.site for macro in lef.macros.values() if macro.site is not None)


def _supply_pins(lef: LefSummary) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for use, key in (("POWER", "supply.power_pin"), ("GROUND", "supply.ground_pin")):
        name = _most_common(
            name
            for macro in lef.macros.values()
            for name in {name for name, pin in macro.pins.items() if pin.use == use}
        )
        if name is not None:
            facts[key] = name
    return facts


def _antenna(lef: LefSummary) -> dict[str, Any]:
    """The antenna cell: the smallest macro of class `CORE ANTENNACELL` with a clear port."""
    candidates = sorted(
        (macro for macro in lef.macros.values() if {"CORE", "ANTENNACELL"} <= set(macro.cls)),
        key=lambda macro: (macro.area, macro.name),
    )
    for macro in candidates:
        signals = {
            name: pin for name, pin in macro.pins.items() if pin.use not in ("POWER", "GROUND")
        }
        sized = {name: pin for name, pin in signals.items() if pin.antenna_diff_area is not None}
        chosen = sized or signals
        if len(chosen) != 1:
            continue
        ((port, pin),) = chosen.items()
        facts: dict[str, Any] = {"antenna_cell": {"cell": macro.name, "port": port}}
        if pin.antenna_diff_area is not None:
            facts["diode_usable"] = pin.antenna_diff_area > 0
        return facts
    return {}


def _tracks(lef: LefSummary) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for layer in lef.layers:
        if layer.pitch is None or layer.offset is None or "." in layer.name:
            continue
        facts[f"tracks.{layer.name}"] = {
            "x": [layer.offset[0], layer.pitch[0]],
            "y": [layer.offset[1], layer.pitch[1]],
        }
    return facts


def _ties(cells: Iterable[LibertyCell]) -> dict[str, Any]:
    """The tie cells: cells with no input pin and an output of the constant function. The
    smallest wins; a tie of area goes to the name."""
    best: dict[str, tuple[tuple[float, str], dict[str, str]]] = {}
    for cell in cells:
        if not cell.pins or any(pin.direction != "output" for pin in cell.pins):
            continue
        for key, function in (("tie_high", "1"), ("tie_low", "0")):
            ports = [
                pin.name
                for pin in cell.pins
                if pin.function is not None and pin.function.replace(" ", "") == function
            ]
            order = (cell.area if cell.area is not None else _INFINITY, cell.name)
            if ports and (key not in best or order < best[key][0]):
                best[key] = (order, {"cell": cell.name, "port": ports[0]})
    return {key: value for key, (_, value) in best.items()}


def derive(files: Mapping[str, list[Path]], corner: str) -> dict[str, Any]:
    """Return the facts that the LEF and Liberty files state, by leaf key.

    `files` maps a role to absolute paths, as `Located.files` does: `tech_lef` and `cell_lef` hold
    the LEF files, and `liberty` holds the files of the corner `corner`. A role that is missing
    gives none of its facts. The values are plain data that validate as the leaves of
    `PlatformModel`. A fact that the files do not state has no key.

    Raises `ValueError` naming the file when a file cannot be read."""
    facts: dict[str, Any] = {}

    lef_paths = [*files.get("tech_lef", []), *files.get("cell_lef", [])]
    if lef_paths:
        lef = read_lef_summary(lef_paths)
        site = _site(lef)
        if site is not None:
            facts["site"] = site
        facts.update(_antenna(lef))
        facts.update(_supply_pins(lef))
        facts.update(_tracks(lef))

    try:
        libraries = [read_liberty_summary(path) for path in files.get("liberty", [])]
    except ValueError as error:
        raise ValueError(f"{error} (corner '{corner}')") from None
    for key, attribute in (
        ("supply.voltage", "nom_voltage"),
        ("time_unit", "time_unit"),
        ("capacitance_unit", "capacitive_load_unit"),
    ):
        values = [getattr(library, attribute) for library in libraries]
        known = [value for value in values if value is not None]
        if known:
            facts[key] = known[0]
    cells: dict[str, LibertyCell] = {}
    for library in libraries:
        for name, cell in library.cells.items():
            cells.setdefault(name, cell)
    facts.update(_ties(cells.values()))
    dont_use = [name for name, cell in cells.items() if cell.dont_use]
    if dont_use:
        facts["dont_use_cells"] = dont_use
    return facts
