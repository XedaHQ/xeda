"""Read the configuration that a PDK or OpenROAD-flow-scripts already ships for a platform.

Two adapters give leaf keys of the platform model (see `xeda.platforms.model`):

* `read_open_pdks_config` reads the `config.tcl` files of open_pdks (sky130) and IHP;
* `read_orfs_config_mk` reads the `config.mk` file of a platform of OpenROAD-flow-scripts.

Neither adapter runs Tcl or Make. Each has a closed set of keys, listed below, and understands one
plain form of a line. A line with a key of the set that has any other form is an error that names
the file and the line. A line with any other key is ignored, whatever it holds. The adapters
read the text of the files only, so they start no process and write nothing.

The `config.tcl` reader
-----------------------

A line is `set ::env(KEY) VALUE` with VALUE bare, in double quotes or in braces. In a bare or a
quoted VALUE, `$::env(OTHER)` and `${::env(OTHER)}` give the value of a key of the set that an
earlier line or an earlier file read. A trailing `; # comment` is allowed. A backslash at the end
of a line continues it. The line must be at the top level: a `set` in a block (`if`, `proc`, ...)
is an error. Any other command on a key of the set (`if`, `lappend`, `unset`, `dict set`, ...) is
an error, and so is a command substitution (`[...]`) or any other `$`. The files are read in the
order given and a later value of a key wins.

| key | leaf | value |
| --- | --- | --- |
| `PLACE_SITE` | `site` | one word |
| `FP_WELLTAP_CELL` | `tap_cell` | one word |
| `FP_ENDCAP_CELL` | `endcap_cell` | one word |
| `FP_TAPCELL_DIST` | `tapcell.distance` | number |
| `SYNTH_TIEHI_PORT`, `SYNTH_TIELO_PORT` | `tie_high`, `tie_low` | `cell port` |
| `SYNTH_MIN_BUF_PORT` | `min_buffer` | `cell in out` |
| `SYNTH_DRIVING_CELL` | `abc_driver_cell` | one word; of `cell/pin` the cell |
| `FILL_CELL`, `DECAP_CELL` | `fill_cells` | words, those of the fill cells first |
| `DIODE_CELL` and `DIODE_CELL_PIN` | `antenna_cell` | a cell and a pin (`cell/pin` or two keys) |
| `RT_MIN_LAYER`, `RT_MAX_LAYER`, `RT_CLOCK_MIN_LAYER` | `routing.min_layer`, ... | one word |
| `FP_IO_HLAYER`, `FP_IO_VLAYER` | `io_layers.horizontal`, `io_layers.vertical` | one word |
| `DATA_WIRE_RC_LAYER`, `CLOCK_WIRE_RC_LAYER` | `wire_rc.signal_layer`, `wire_rc.clock_layer` | one word |
| `LAYERS_RC` | `wire_rc.layers.<layer>` | `res` and `cap` of the layer, see below |
| `VIAS_R` | `wire_rc.vias.<via>` | `res` of the via, see below |
| `CTS_ROOT_BUFFER`, `CTS_CLK_BUFFER_LIST` | `cts_buffers` | the root, then the list, no repeats |
| `FP_PDN_RAIL_LAYER`, `_RAIL_WIDTH`, `_RAIL_OFFSET` | `pdn.rails` | one rail |
| `FP_PDN_VERTICAL_LAYER`, `_VWIDTH`, `_VPITCH`, `_VOFFSET` | `pdn.straps` | the first strap |
| `FP_PDN_HORIZONTAL_LAYER`, `_HWIDTH`, `_HPITCH`, `_HOFFSET` | `pdn.straps` | the second strap |

IHP and LibreLane spell some keys differently, and the reader takes both spellings as one key:
`FILL_CELLS` and `DECAP_CELLS` for `FILL_CELL` and `DECAP_CELL`, `CTS_CLK_BUFFERS` for
`CTS_CLK_BUFFER_LIST`, and `PDN_...` for every `FP_PDN_...` key.

The wire resistance and capacitance have the form that the IHP files use. The line
`set ::env(LAYERS_RC) [dict create]` starts an empty table, and each value is a line
`dict set ::env(LAYERS_RC) "*" <layer> res <number>` or `... <layer> cap <number>`. The corner
pattern must be `*`, and every layer needs both `res` and `cap`. `VIAS_R` has the same form with
`res` only. The leaf of a layer is the pair `[res, cap]`. The numbers keep the units of the file.

The power grid is the standard-cell grid: one rail and up to two straps. A layer key needs the
width, pitch and offset keys of its own strap (a rail needs the width; its offset is optional).
The reader ignores the keys that the `Pdn` model cannot hold: `*_VSPACING`, `*_HSPACING`, the
`*_CORE_RING_*` keys, the macro blockage keys and every other `PDN` key. It does not give
`pdn.connects` or `pdn.pin_layers`, because no key of these files names them.

The reader ignores `GRT_LAYER_ADJUSTMENTS`. It is a list with one number for each routing layer
of the technology, from its lowest routing layer up. That layer is below `RT_MIN_LAYER` in sky130
(`li1`) and in IHP (`Metal1`), so only the technology LEF can map the numbers to layer names.

The `config.mk` reader
----------------------

A line is `export KEY = VALUE`, with `?=`, `:=`, `::=` or `+=` for `=`, and `export` is optional.
The reader follows Make for these: `?=` sets a key that has no value, and `+=` adds a space and the
new words. A backslash at the end of a line continues it, and `#` starts a comment. In a value,
`$(PLATFORM_DIR)` is the directory of the platform and `$(ADDITIONAL_...)` is empty. A value in
double quotes loses its quotes. Any other `$` in the value of a key of the set is an error.
A file with a conditional (`ifeq`, `ifneq`, `ifdef`, `ifndef`, `else`, `endif`), `include` or
`define` needs Make, so the reader refuses the whole file. An empty value gives an empty list
for a list leaf and no leaf for any other.

| key | leaf | value |
| --- | --- | --- |
| `PLACE_SITE` | `site` | one word |
| `TIEHI_CELL_AND_PORT`, `TIELO_CELL_AND_PORT` | `tie_high`, `tie_low` | `cell port` |
| `MIN_BUF_CELL_AND_PORTS` | `min_buffer` | `cell in out` |
| `ABC_DRIVER_CELL` | `abc_driver_cell` | one word |
| `ABC_LOAD_IN_FF` | `abc_load_in_ff` | number |
| `DONT_USE_CELLS` | `dont_use_cells` | words |
| `FILL_CELLS` | `fill_cells` | words |
| `TAP_CELL_NAME` | `tap_cell` | one word |
| `IO_PLACER_H`, `IO_PLACER_V` | `io_layers.horizontal`, `io_layers.vertical` | one word |
| `MIN_ROUTING_LAYER`, `MIN_CLK_ROUTING_LAYER`, `MAX_ROUTING_LAYER` | `routing.min_layer`, ... | one word |
| `PLACE_DENSITY` | `place_density` | number |
| `MATCH_CELL_FOOTPRINT` | `match_cell_footprint` | `1` or `0` |
| `OPT_POST_GRT_WNS` | `opt_post_grt_wns` | number |
| `SYNTH_MINIMUM_KEEP_SIZE` | `synth_minimum_keep_size` | integer |
| `PWR_NETS_VOLTAGES`, `GND_NETS_VOLTAGES` | `supply.power_nets.<net>`, `supply.ground_nets.<net>` | pairs of net and voltage |
| `IR_DROP_LAYER` | `ir_drop_layer` | one word |
| `ROUTING_LAYER_ADJUSTMENT` | `routing.layer_adjustment` | number |

The keys that name files (`TECH_LEF`, `SC_LEF`, `LIB_FILES`, `GDS_FILES`, `*_MAP_FILE`, `*_TCL`,
`KLAYOUT_*`, `CDL_FILE`, `RCX_RULES`, ...) are ignored: the residue file names the files of the
platform.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

__all__ = ["PlatformConfigError", "read_open_pdks_config", "read_orfs_config_mk"]


class PlatformConfigError(ValueError):
    """A configuration file that the adapter cannot read. The message names the file and, when it
    comes from one line, the number of the line."""

    def __init__(self, path: Path, line: Optional[int], message: str) -> None:
        where = f"{path}" if line is None else f"{path}:{line}"
        super().__init__(f"{where}: {message}")
        self.path = path
        self.line = line


class _Invalid(ValueError):
    """A problem with the text of one line. The caller adds the file and the line number."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PlatformConfigError(path, None, f"cannot read the file ({error})") from None


# ---------------------------------------------------------------------------------------------
# Values: the text of a key as the type of its leaf
# ---------------------------------------------------------------------------------------------

NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
INTEGER = re.compile(r"[+-]?\d+")


def _one_word(text: str) -> Optional[str]:
    words = text.split()
    if len(words) > 1:
        raise _Invalid(f"expects one word, got `{text.strip()}`")
    return words[0] if words else None


def _words(text: str) -> list[str]:
    return text.split()


def _number(text: str) -> Optional[float]:
    text = text.strip()
    if not text:
        return None
    if not NUMBER.fullmatch(text):
        raise _Invalid(f"expects a number, got `{text}`")
    return float(text)


def _integer(text: str) -> Optional[int]:
    text = text.strip()
    if not text:
        return None
    if not INTEGER.fullmatch(text):
        raise _Invalid(f"expects an integer, got `{text}`")
    return int(text)


def _flag(text: str) -> Optional[bool]:
    text = text.strip()
    if not text:
        return None
    if text not in ("0", "1"):
        raise _Invalid(f"expects 1 or 0, got `{text}`")
    return text == "1"


def _cell_port(text: str) -> Optional[dict[str, str]]:
    words = text.split()
    if not words:
        return None
    if len(words) != 2:
        raise _Invalid(f"expects a cell and a port (`cell port`), got `{text.strip()}`")
    return {"cell": words[0], "port": words[1]}


def _cell_ports(text: str) -> Optional[dict[str, str]]:
    words = text.split()
    if not words:
        return None
    if len(words) != 3:
        raise _Invalid(
            f"expects a cell, an input and an output (`cell in out`), got `{text.strip()}`"
        )
    return {"cell": words[0], "input": words[1], "output": words[2]}


def _net_voltages(text: str) -> dict[str, float]:
    words = text.split()
    if len(words) % 2:
        raise _Invalid(f"expects pairs of a net name and a voltage, got `{text.strip()}`")
    nets: dict[str, float] = {}
    for net, voltage in zip(words[::2], words[1::2]):
        if "." in net:
            raise _Invalid(f"the net name `{net}` has a `.`, which a leaf key cannot hold")
        if net in nets:
            raise _Invalid(f"names the net `{net}` twice")
        value = _number(voltage)
        assert value is not None
        nets[net] = value
    return nets


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


@dataclass(frozen=True)
class _Entry:
    """The final value of one key: where it came from, how the file spelled it, the value."""

    path: Path
    line: int
    name: str
    value: Any


# ---------------------------------------------------------------------------------------------
# config.tcl
# ---------------------------------------------------------------------------------------------


def _diode_cell(text: str) -> Optional[tuple[str, Optional[str]]]:
    word = _one_word(text)
    if word is None:
        return None
    cell, slash, pin = word.partition("/")
    if slash and (not cell or not pin or "/" in pin):
        raise _Invalid(f"expects `cell` or `cell/pin`, got `{word}`")
    return cell, (pin if slash else None)


def _driving_cell(text: str) -> Optional[str]:
    word = _one_word(text)
    if word is None:
        return None
    cell, slash, pin = word.partition("/")
    if slash and (not cell or not pin or "/" in pin):
        raise _Invalid(f"expects `cell` or `cell/pin`, got `{word}`")
    return cell


#: Keys that give one leaf each: the key, the leaf and the converter of the text.
TCL_DIRECT: dict[str, tuple[str, Callable[[str], Any]]] = {
    "PLACE_SITE": ("site", _one_word),
    "FP_WELLTAP_CELL": ("tap_cell", _one_word),
    "FP_ENDCAP_CELL": ("endcap_cell", _one_word),
    "FP_TAPCELL_DIST": ("tapcell.distance", _number),
    "SYNTH_TIEHI_PORT": ("tie_high", _cell_port),
    "SYNTH_TIELO_PORT": ("tie_low", _cell_port),
    "SYNTH_MIN_BUF_PORT": ("min_buffer", _cell_ports),
    "SYNTH_DRIVING_CELL": ("abc_driver_cell", _driving_cell),
    "RT_MIN_LAYER": ("routing.min_layer", _one_word),
    "RT_MAX_LAYER": ("routing.max_layer", _one_word),
    "RT_CLOCK_MIN_LAYER": ("routing.clock_min_layer", _one_word),
    "FP_IO_HLAYER": ("io_layers.horizontal", _one_word),
    "FP_IO_VLAYER": ("io_layers.vertical", _one_word),
    "DATA_WIRE_RC_LAYER": ("wire_rc.signal_layer", _one_word),
    "CLOCK_WIRE_RC_LAYER": ("wire_rc.clock_layer", _one_word),
}

#: The keys of the power grid, without their `PDN_` prefix.
PDN_KEYS = {
    "RAIL_LAYER": _one_word,
    "RAIL_WIDTH": _number,
    "RAIL_OFFSET": _number,
    "VERTICAL_LAYER": _one_word,
    "HORIZONTAL_LAYER": _one_word,
    "VWIDTH": _number,
    "VPITCH": _number,
    "VOFFSET": _number,
    "HWIDTH": _number,
    "HPITCH": _number,
    "HOFFSET": _number,
}

#: Every key that is read as one value, by its main name, with the converter of its text.
TCL_CONVERTERS: dict[str, Callable[[str], Any]] = {
    **{key: convert for key, (_, convert) in TCL_DIRECT.items()},
    "FILL_CELL": _words,
    "DECAP_CELL": _words,
    "DIODE_CELL": _diode_cell,
    "DIODE_CELL_PIN": _one_word,
    "CTS_ROOT_BUFFER": _one_word,
    "CTS_CLK_BUFFER_LIST": _words,
    **{f"PDN_{key}": convert for key, convert in PDN_KEYS.items()},
}

#: Other spellings of a key, by the main name.
TCL_ALIASES = {
    "FILL_CELLS": "FILL_CELL",
    "DECAP_CELLS": "DECAP_CELL",
    "CTS_CLK_BUFFERS": "CTS_CLK_BUFFER_LIST",
    **{f"FP_PDN_{key}": f"PDN_{key}" for key in PDN_KEYS},
}

#: The two keys that hold a table, which the IHP files fill with `dict set`.
TCL_TABLES = ("LAYERS_RC", "VIAS_R")

#: Every name of a key that the reader takes.
TCL_NAMES = frozenset((*TCL_CONVERTERS, *TCL_ALIASES, *TCL_TABLES))

TCL_SET = re.compile(r"set\s+::env\((\w+)\)(?:\s+(.*))?")
TCL_DICT_SET = re.compile(r"dict\s+set\s+::env\((LAYERS_RC|VIAS_R)\)\s+(.*)")
TCL_DICT_CREATE = re.compile(r"\[dict create\]\s*(?:;\s*(?:#.*)?)?")
TCL_TABLE_VALUE = re.compile(
    r'("[^"\s]*"|\S+)\s+([A-Za-z0-9_]+)\s+(\w+)\s+([^\s;]+)\s*(?:;\s*(?:#.*)?)?'
)
TCL_TAIL = re.compile(r"\s*(?:;\s*(?:#.*)?)?")
TCL_READ = re.compile(r"\$\{::env\((\w+)\)\}|\$::env\((\w+)\)")
TCL_MENTION = re.compile(r"::env\((\w+)\)")


def _tcl_lines(text: str) -> list[tuple[int, str]]:
    """Return the lines of Tcl text with the number of the first physical line of each. A
    backslash at the end of a line joins it to the next one."""
    physical = text.splitlines()
    lines: list[tuple[int, str]] = []
    index = 0
    while index < len(physical):
        first, current = index + 1, physical[index]
        while len(current) - len(current.rstrip("\\")) & 1:
            index += 1
            following = physical[index].lstrip() if index < len(physical) else ""
            current = current[:-1] + " " + following
        lines.append((first, current))
        index += 1
    return lines


def _brace_delta(line: str) -> int:
    """Return how much a line opens more braces than it closes, outside double quotes and the
    comment at the end."""
    delta, quoted, command_start, index = 0, False, True, 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 1
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "#" and command_start:
                break
            if char == "{":
                delta += 1
            elif char == "}":
                delta -= 1
        if not quoted and not char.isspace() and char != "\\":
            command_start = char == ";"
        index += 1
    return delta


class _TclReader:
    """The state of reading a list of `config.tcl` files in order."""

    def __init__(self) -> None:
        #: The text of each key read so far, by the name that the file used.
        self.env: dict[str, str] = {}
        #: The latest entry of each key, by its main name.
        self.entries: dict[str, _Entry] = {}
        #: The entries of the two tables: table, then layer or via, then field.
        self.tables: dict[str, dict[str, dict[str, _Entry]]] = {name: {} for name in TCL_TABLES}

    def read(self, path: Path) -> None:
        depth = 0
        for number, line in _tcl_lines(_read_text(path)):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                self.statement(path, number, text, depth)
            except _Invalid as error:
                raise PlatformConfigError(path, number, str(error)) from None
            depth = max(0, depth + _brace_delta(text))

    def statement(self, path: Path, number: int, text: str, depth: int) -> None:
        plain = TCL_SET.fullmatch(text)
        if plain and plain.group(1) in TCL_NAMES:
            key = plain.group(1)
            if depth:
                raise _Invalid(
                    f"`set ::env({key})` is inside a block (`if`, `proc`, ...): the reader takes "
                    "only a plain `set` line at the top level"
                )
            self.set_key(path, number, key, plain.group(2))
            return
        table = TCL_DICT_SET.fullmatch(text)
        if table:
            if depth:
                raise _Invalid(
                    f"`dict set ::env({table.group(1)})` is inside a block (`if`, `proc`, ...): "
                    "the reader takes only a plain line at the top level"
                )
            self.set_table_value(path, number, table.group(1), table.group(2))
            return
        for found in TCL_MENTION.finditer(TCL_READ.sub("", text)):
            if found.group(1) in TCL_NAMES:
                raise _Invalid(
                    f"`{text.split()[0]}` writes or tests `{found.group(1)}`; the reader takes "
                    f"only a plain `set ::env({found.group(1)}) value`"
                )

    def set_key(self, path: Path, number: int, key: str, rest: Optional[str]) -> None:
        rest = (rest or "").strip()
        if key in TCL_TABLES:
            if not TCL_DICT_CREATE.fullmatch(rest):
                raise _Invalid(
                    f"`{key}` must start as `set ::env({key}) [dict create]`, then take "
                    f"`dict set ::env({key}) ...` lines"
                )
            self.tables[key] = {}
            return
        if not rest:
            raise _Invalid(f"`set ::env({key})` has no value")
        value = self.value(key, rest)
        self.env[key] = value
        main = TCL_ALIASES.get(key, key)
        try:
            converted = TCL_CONVERTERS[main](value)
        except _Invalid as error:
            raise _Invalid(f"`{key}` {error}") from None
        self.entries[main] = _Entry(path, number, key, converted)

    def value(self, key: str, rest: str) -> str:
        """Return the value that the text after `set ::env(KEY)` stands for."""
        if rest.startswith('"'):
            end = 1
            while end < len(rest) and rest[end] != '"':
                if rest[end] in "\\[":
                    raise _Invalid(
                        f"`{key}` has a `{rest[end]}` in a quoted value: the reader takes neither "
                        "an escape nor a command substitution"
                    )
                end += 1
            if end == len(rest):
                raise _Invalid(f"`{key}` has a value with no closing double quote")
            raw, tail = rest[1:end], rest[end + 1 :]
            text = self.substitute(raw)
        elif rest.startswith("{"):
            end = rest.find("}")
            if end < 0:
                raise _Invalid(f"`{key}` has a value with no closing brace")
            raw, tail = rest[1:end], rest[end + 1 :]
            if "{" in raw:
                raise _Invalid(f"`{key}` has a `{{` inside braces; the reader takes no nesting")
            if "$" in raw or "\\" in raw:
                raise _Invalid(
                    f"`{key}` has a `$` or a `\\` inside braces, which Tcl keeps as text"
                )
            text = raw
        else:
            word = re.match(r"[^\s;]+", rest)
            assert word is not None
            raw, tail = word.group(0), rest[word.end() :]
            for char in '[]"{}\\':
                if char in raw:
                    raise _Invalid(f"`{key}` has a `{char}` in a bare value")
            text = self.substitute(raw)
        if not TCL_TAIL.fullmatch(tail):
            raise _Invalid(
                f"`set ::env({key})` takes one value, but the line goes on: `{tail.strip()}`"
            )
        return text

    def substitute(self, raw: str) -> str:
        def replace(found: "re.Match[str]") -> str:
            name = found.group(1) or found.group(2)
            if name not in self.env:
                raise _Invalid(
                    f"`$::env({name})` refers to a key that no earlier line or file of the keys "
                    "that the reader takes has set"
                )
            return self.env[name]

        text = TCL_READ.sub(replace, raw)
        if "$" in TCL_READ.sub("", raw):
            raise _Invalid(
                f"`{raw}` has a `$` other than `$::env(KEY)` and `${{::env(KEY)}}`: the reader "
                "does not run Tcl"
            )
        return text

    def set_table_value(self, path: Path, number: int, table: str, rest: str) -> None:
        found = TCL_TABLE_VALUE.fullmatch(rest.strip())
        if not found:
            raise _Invalid(
                f'expects `dict set ::env({table}) "*" <name> '
                f"{'res' if table == 'VIAS_R' else 'res|cap'} <number>`"
            )
        corner, name, field_name, text = found.groups()
        if corner.strip('"') != "*":
            raise _Invalid(f"`{table}` takes the corner pattern `*` only, got `{corner}`")
        allowed = ("res",) if table == "VIAS_R" else ("res", "cap")
        if field_name not in allowed:
            raise _Invalid(
                f"`{table}` has the field `res` only"
                if table == "VIAS_R"
                else f"`{table}` has the fields `res` and `cap`, not `{field_name}`"
            )
        value = _number(text)
        self.tables[table].setdefault(name, {})[field_name] = _Entry(path, number, name, value)

    # -- the leaves ----------------------------------------------------------------------

    def get(self, key: str) -> Optional[_Entry]:
        entry = self.entries.get(key)
        return entry if entry is not None and entry.value is not None else None

    def leaves(self) -> dict[str, Any]:
        leaves: dict[str, Any] = {}
        for key, (leaf, _) in TCL_DIRECT.items():
            entry = self.get(key)
            if entry is not None:
                leaves[leaf] = entry.value
        fill = [self.entries.get("FILL_CELL"), self.entries.get("DECAP_CELL")]
        if any(entry is not None for entry in fill):
            leaves["fill_cells"] = [word for entry in fill if entry for word in entry.value]
        self.antenna(leaves)
        buffers = [self.entries.get("CTS_ROOT_BUFFER"), self.entries.get("CTS_CLK_BUFFER_LIST")]
        if any(entry is not None for entry in buffers):
            names: list[str] = []
            for entry in buffers:
                if entry is not None and entry.value is not None:
                    names += entry.value if isinstance(entry.value, list) else [entry.value]
            leaves["cts_buffers"] = _unique(names)
        for layer, fields in self.tables["LAYERS_RC"].items():
            for missing, present in (("cap", "res"), ("res", "cap")):
                if missing not in fields:
                    entry = fields[present]
                    raise PlatformConfigError(
                        entry.path,
                        entry.line,
                        f"the layer `{layer}` has `{present}` but no `{missing}`",
                    )
            leaves[f"wire_rc.layers.{layer}"] = [fields["res"].value, fields["cap"].value]
        for via, fields in self.tables["VIAS_R"].items():
            leaves[f"wire_rc.vias.{via}"] = fields["res"].value
        self.power_grid(leaves)
        return leaves

    def antenna(self, leaves: dict[str, Any]) -> None:
        diode, pin = self.get("DIODE_CELL"), self.get("DIODE_CELL_PIN")
        if diode is None:
            return
        cell, own_pin = diode.value
        if own_pin and pin is not None and pin.value != own_pin:
            raise PlatformConfigError(
                diode.path,
                diode.line,
                f"`{diode.name}` names the pin `{own_pin}`, but `{pin.name}` says `{pin.value}`",
            )
        port = own_pin or (pin.value if pin is not None else None)
        if port is None:
            raise PlatformConfigError(
                diode.path,
                diode.line,
                f"`{diode.name}` needs a pin: write `cell/pin` or set `DIODE_CELL_PIN`",
            )
        leaves["antenna_cell"] = {"cell": cell, "port": port}

    def power_grid(self, leaves: dict[str, Any]) -> None:
        def need(layer: _Entry, *suffixes: str) -> list[Any]:
            prefix = "FP_PDN_" if layer.name.startswith("FP_") else "PDN_"
            entries = [self.get(f"PDN_{suffix}") for suffix in suffixes]
            missing = [f"`{prefix}{suffix}`" for suffix, e in zip(suffixes, entries) if e is None]
            if missing:
                raise PlatformConfigError(
                    layer.path, layer.line, f"`{layer.name}` needs {' and '.join(missing)}"
                )
            return [e.value for e in entries if e is not None]

        rail = self.get("PDN_RAIL_LAYER")
        if rail is not None:
            (width,) = need(rail, "RAIL_WIDTH")
            offset = self.get("PDN_RAIL_OFFSET")
            leaves["pdn.rails"] = [
                {
                    "layer": rail.value,
                    "width": width,
                    **({"offset": offset.value} if offset else {}),
                }
            ]
        straps: list[dict[str, Any]] = []
        for orientation, letter in (("VERTICAL", "V"), ("HORIZONTAL", "H")):
            layer = self.get(f"PDN_{orientation}_LAYER")
            if layer is not None:
                width, pitch, offset = need(
                    layer, f"{letter}WIDTH", f"{letter}PITCH", f"{letter}OFFSET"
                )
                straps.append(
                    {"layer": layer.value, "width": width, "pitch": pitch, "offset": offset}
                )
        if straps:
            leaves["pdn.straps"] = straps


def read_open_pdks_config(paths: Sequence[Path]) -> dict[str, Any]:
    """Read `config.tcl` files of open_pdks or IHP and return the leaves of the platform model.

    `paths` are the files in reading order: the file of the PDK, then the file of the library. A
    later file wins at a key, and its values may refer to a key of an earlier file with
    `$::env(KEY)`. A line that the reader needs but does not understand raises
    `PlatformConfigError`, which names the file and the line.
    """
    reader = _TclReader()
    for path in paths:
        reader.read(Path(path))
    return reader.leaves()


# ---------------------------------------------------------------------------------------------
# config.mk
# ---------------------------------------------------------------------------------------------

#: Keys that give one leaf each: the key, the leaf and the converter of the text.
MK_DIRECT: dict[str, tuple[str, Callable[[str], Any]]] = {
    "PLACE_SITE": ("site", _one_word),
    "TIEHI_CELL_AND_PORT": ("tie_high", _cell_port),
    "TIELO_CELL_AND_PORT": ("tie_low", _cell_port),
    "MIN_BUF_CELL_AND_PORTS": ("min_buffer", _cell_ports),
    "ABC_DRIVER_CELL": ("abc_driver_cell", _one_word),
    "ABC_LOAD_IN_FF": ("abc_load_in_ff", _number),
    "DONT_USE_CELLS": ("dont_use_cells", _words),
    "FILL_CELLS": ("fill_cells", _words),
    "TAP_CELL_NAME": ("tap_cell", _one_word),
    "IO_PLACER_H": ("io_layers.horizontal", _one_word),
    "IO_PLACER_V": ("io_layers.vertical", _one_word),
    "MIN_ROUTING_LAYER": ("routing.min_layer", _one_word),
    "MIN_CLK_ROUTING_LAYER": ("routing.clock_min_layer", _one_word),
    "MAX_ROUTING_LAYER": ("routing.max_layer", _one_word),
    "PLACE_DENSITY": ("place_density", _number),
    "MATCH_CELL_FOOTPRINT": ("match_cell_footprint", _flag),
    "OPT_POST_GRT_WNS": ("opt_post_grt_wns", _number),
    "SYNTH_MINIMUM_KEEP_SIZE": ("synth_minimum_keep_size", _integer),
    "IR_DROP_LAYER": ("ir_drop_layer", _one_word),
    "ROUTING_LAYER_ADJUSTMENT": ("routing.layer_adjustment", _number),
}

#: The keys that give a table of nets: the key and the table of the leaf.
MK_NETS = {"PWR_NETS_VOLTAGES": "supply.power_nets", "GND_NETS_VOLTAGES": "supply.ground_nets"}

MK_CONDITIONAL = re.compile(r"(ifeq|ifneq|ifdef|ifndef|else|endif)\b")
MK_NEEDS_MAKE = re.compile(r"(include|-include|sinclude|define|endef)\b")
MK_ASSIGNMENT = re.compile(
    r"(?:(?:export|override|private)\s+)*([A-Za-z_][\w.\-]*)\s*(\+=|::=|:=|\?=|=)\s*(.*)"
)
MK_REFERENCE = re.compile(r"\$\$|\$\([A-Za-z0-9_]*\)|\$\S*")


def _mk_lines(text: str) -> list[tuple[int, str]]:
    """Return the lines of a makefile with the number of the first physical line of each, without
    comments. A backslash at the end of a line joins it to the next one, also in a comment."""
    physical = text.splitlines()
    lines: list[tuple[int, str]] = []
    index = 0
    while index < len(physical):
        first, current = index + 1, physical[index]
        while len(current) - len(current.rstrip("\\")) & 1:
            index += 1
            following = physical[index].lstrip() if index < len(physical) else ""
            current = current[:-1].rstrip() + " " + following
        hash_at = next(
            (i for i, char in enumerate(current) if char == "#" and current[i - 1 : i] != "\\"),
            None,
        )
        if hash_at is not None:
            current = current[:hash_at]
        lines.append((first, current.replace("\\#", "#")))
        index += 1
    return lines


def _mk_expand(value: str, platform_dir: Path) -> str:
    def replace(found: "re.Match[str]") -> str:
        token = found.group(0)
        name = token[2:-1]
        if token.startswith("$(") and token.endswith(")") and name == "PLATFORM_DIR":
            return str(platform_dir)
        if token.startswith("$(") and token.endswith(")") and name.startswith("ADDITIONAL_"):
            return ""
        raise _Invalid(
            f"the value has `{token}`: the reader knows `$(PLATFORM_DIR)` and `$(ADDITIONAL_...)` "
            "only, and it does not run Make"
        )

    return MK_REFERENCE.sub(replace, value)


def read_orfs_config_mk(path: Path, platform_dir: Path) -> dict[str, Any]:
    """Read the `config.mk` of an OpenROAD-flow-scripts platform and return leaves of the model.

    `platform_dir` is the value of `$(PLATFORM_DIR)`. A file with a conditional needs Make, and the
    reader refuses it whole. A line of a key of the reader that has any other form raises
    `PlatformConfigError`, which names the file and the line.
    """
    path = Path(path)
    lines = _mk_lines(_read_text(path))
    for number, line in lines:
        needs_make = MK_CONDITIONAL.match(line.strip()) or MK_NEEDS_MAKE.match(line.strip())
        if needs_make:
            raise PlatformConfigError(
                path,
                number,
                f"`{needs_make.group(1)}` needs Make: give these values in the residue file",
            )
    state: dict[str, tuple[str, int]] = {}
    for number, line in lines:
        text = line.strip()
        assignment = MK_ASSIGNMENT.fullmatch(text)
        if not assignment:
            continue
        key, operator, value = assignment.groups()
        if key not in MK_DIRECT and key not in MK_NETS:
            continue
        try:
            value = _mk_expand(value.strip(), platform_dir)
        except _Invalid as error:
            raise PlatformConfigError(path, number, f"`{key}`: {error}") from None
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if '"' in value or "'" in value:
            raise PlatformConfigError(path, number, f"`{key}` has a quote in its value: `{value}`")
        if operator == "?=" and key in state:
            continue
        if operator == "+=" and key in state:
            value = " ".join(part for part in (state[key][0], value) if part)
        state[key] = (value, number)

    leaves: dict[str, Any] = {}
    for key, (value, number) in state.items():
        try:
            if key in MK_NETS:
                for net, voltage in _net_voltages(value).items():
                    leaves[f"{MK_NETS[key]}.{net}"] = voltage
            else:
                leaf, convert = MK_DIRECT[key]
                converted = convert(value)
                if converted is not None:
                    leaves[leaf] = converted
        except _Invalid as error:
            raise PlatformConfigError(path, number, f"`{key}` {error}") from None
    return leaves
