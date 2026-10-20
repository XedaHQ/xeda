"""Find every flag that the OpenROAD flows print, to check them against the installed tool.

`tests/test_openroad_real.py` runs one OpenROAD process that prints the `help` of every command
the templates use, and compares it with the pairs of a command and a flag that this module
collects without running OpenROAD:

* `rendered_pairs` renders the drivers of the four flows for many settings: the defaults, and the
  defaults with one setting changed. Each switch is flipped, each value of a choice is taken, and
  each number, quantity, name and list is set. A group that is off is switched on when it has to
  be.
* `template_pairs` reads the text of the templates, so that a flag in a branch no setting reaches
  is checked too.
* `extra_flag_pairs` gives each flag of an `EXTRA_FLAGS` list with its command.

The collector reads Tcl line by line. It knows the commands of `OPENROAD_COMMANDS`; the words of
a command that start with `-` and stand outside quotes, braces and brackets are its flags.
"""

import re
import subprocess
import typing
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import UnionType
from typing import Any, Optional

import yaml

from xeda import Design
from xeda.flow.settings_group import SettingsGroup
from xeda.flows.openroad import Openroad, OpenroadCts, OpenroadGrt, OpenroadPlace, base
from xeda.units import Quantity

from .openroad_utils import openroad_design, render_driver
from .test_openroad_final import GDS_FILE, KLAYOUT_TECH, RCX_RULES, SUPPLY, variant_residue
from .tiny_platform import tiny_settings, tiny_store
from .tool_utils import OPENROAD_COMMANDS

TEMPLATES = Path(base.__file__).parent / "templates"

#: The drivers of each flow.
DRIVERS = {
    "openroad_place": ("openroad_place",),
    "openroad_cts": ("openroad_cts",),
    "openroad_grt": ("openroad_grt",),
    "openroad": ("openroad_route", "openroad_final", "openroad_images"),
}
FLOWS = {flow.name: flow for flow in (OpenroadPlace, OpenroadCts, OpenroadGrt, Openroad)}

#: Settings of the base classes of the flows. They name no OpenROAD flag.
BASE_SETTINGS = frozenset(
    {
        "reports_dir",
        "checkpoints_dir",
        "outputs_dir",
        "lib_paths",
        "docker",
        "platform",
        "platform_file",
        "corner",
        "pdk_version",
        "pdk_root",
        "clocks",
    }
)

#: The values that a number may take, in the order a variant tries them: the first that the
#: settings accept is used.
NUMBERS = (2, 1, 3, 10, 50, 100, 0.5, 2.0)
#: The values that a name may take: a layer of the `tiny` platform, a cell of it, any name.
NAMES = ("m2", "BUF_X1", "xeda_name")


# ------------------------------------------------------------------------------ the Tcl reader

COMMAND = re.compile(
    r"(?<![^\s\[{;])("
    + "|".join(re.escape(name) for name in sorted(OPENROAD_COMMANDS, key=len, reverse=True))
    + r")(?![\w:])"
)
FLAG_WORD = re.compile(r"-[A-Za-z_]\w*")
#: `set name [list ...`: a list of arguments that a later command takes as `{*}$name`.
LIST_ASSIGNMENT = re.compile(r"set\s+(\w+)\s+\[list\s")
OPENING, CLOSING = "[{", "]}"


def logical_lines(text: str) -> Iterator[str]:
    """Yield the lines of a Tcl script without comment lines, with each continuation line
    (`\\` at the end) joined to the line before it."""
    held = ""
    for raw in text.splitlines():
        line = held + raw.strip()
        held = ""
        if line.endswith("\\"):
            held = line[:-1] + " "
        elif line and not line.startswith("#"):
            yield line
    if held.strip():
        yield held


def flags_of(rest: str, lists: Optional[dict[str, list[str]]] = None) -> list[str]:
    """Return the flags in `rest`, the text after a command name: the words that start with `-`
    and stand outside quotes, braces and brackets. The text ends at a `;` or a closing bracket or
    brace that nothing in `rest` opened. A word `$name` (or `{*}$name`) stands for the flags of the
    variable `name` in `lists`."""
    flags: list[str] = []
    depth, word, quoted, i = 0, "", False, 0

    def end_word() -> None:
        nonlocal word
        if FLAG_WORD.fullmatch(word):
            flags.append(word)
        elif word.startswith("$") and lists and word[1:] in lists:
            flags.extend(lists[word[1:]])
        word = ""

    while i < len(rest):
        char = rest[i]
        if char == "\\":
            i += 2
            continue
        if quoted:
            quoted = char != '"'
        elif char == '"' and not word:
            quoted = True
        elif char in OPENING:
            depth += 1
        elif char in CLOSING:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char == ";":
            break
        elif depth == 0 and char.isspace():
            end_word()
        elif depth == 0:
            word += char
        i += 1
    else:
        end_word()
        return flags
    end_word()
    return flags


def pairs_of_script(text: str) -> set[tuple[str, str]]:
    """Return the `(command, flag)` pairs of a Tcl script."""
    found: set[tuple[str, str]] = set()
    lines = list(logical_lines(text))
    lists: dict[str, list[str]] = {}
    for line in lines:
        assigned = LIST_ASSIGNMENT.match(line)
        if assigned:
            lists[assigned.group(1)] = flags_of(line[assigned.end() :], lists)
    for line in lines:
        for match in COMMAND.finditer(line):
            for flag in flags_of(line[match.end() :], lists):
                found.add((match.group(1), flag))
    return found


def pairs_of_template(text: str) -> set[tuple[str, str]]:
    """Return the `(command, flag)` pairs of the text of a template: the Tcl around the Jinja
    statements. A comment and a statement are dropped, and an expression is a word."""
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.S)
    text = re.sub(r"\{\{.*?\}\}", "X", text, flags=re.S)
    return pairs_of_script(text)


def template_pairs() -> set[tuple[str, str]]:
    """Return the pairs in the text of every template of the OpenROAD flows."""
    found: set[tuple[str, str]] = set()
    for path in sorted(TEMPLATES.rglob("*.j2")):
        found |= pairs_of_template(path.read_text())
    return found


# ------------------------------------------------------------------------------ the help text


def help_flags(output: str) -> dict[str, set[str]]:
    """Return the flags of each command in the `help` output of OpenROAD.

    A usage starts at the start of a line with the name of the command; the lines that follow it
    are indented. `help <name>` can print the usage of more than one command, so the usages are
    kept apart."""
    flags: dict[str, set[str]] = {}
    current = None
    for line in output.splitlines():
        start = re.match(r"([\w:]+)(?=\s|$)", line)
        if start and not line.startswith("["):
            current = start.group(1)
            flags.setdefault(current, set())
        if current is not None:
            flags[current] |= set(re.findall(r"(?<![\w-])(-[A-Za-z_]\w*)", line))
    return flags


@dataclass
class Installed:
    """What the installed OpenROAD says about the commands of `OPENROAD_COMMANDS`."""

    #: The commands that `info commands` finds.
    exists: set[str] = field(default_factory=set)
    #: The flags that `help` lists, for each command that has a usage.
    flags: dict[str, set[str]] = field(default_factory=dict)
    #: The flags that the Tcl body of a command names, for each command that is a Tcl procedure
    #: and has no usage in `help`. The procedure reads its flags with `parse_key_args`.
    body_flags: dict[str, set[str]] = field(default_factory=dict)


def read_installed(openroad: Path, work: Path) -> Installed:
    """Run one OpenROAD process in `work` that prints `help` and `info commands` for each command
    of `OPENROAD_COMMANDS`, and read what it wrote."""
    script = work / "help.tcl"
    script.write_text(
        "set out [open help.txt w]\n"
        "foreach c {" + " ".join(OPENROAD_COMMANDS) + "} {\n"
        "    with_output_to_variable text [list help $c]\n"
        '    puts $out "@@@ $c [llength [info commands $c]]"\n'
        "    puts $out $text\n"
        '    puts $out "@@body"\n'
        "    if {![catch {info body $c} body]} {puts $out $body}\n"
        "}\n"
        "close $out\n"
    )
    done = subprocess.run(
        [str(openroad), "-no_init", "-no_splash", "-exit", script.name],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    installed = Installed()
    for block in (work / "help.txt").read_text().split("@@@ ")[1:]:
        heading, _, text = block.partition("\n")
        text, _, body = text.partition("@@body\n")
        name, count = heading.split()
        if int(count):
            installed.exists.add(name)
        usage = help_flags(text)
        if name in usage:
            installed.flags[name] = usage[name]
        elif body.strip():
            installed.body_flags[name] = set(re.findall(r"(?<![\w-])(-[A-Za-z_]\w*)", body))
    return installed


# ------------------------------------------------------------------------------ the settings


def unwrap(annotation: Any) -> Any:
    """Return `annotation` without `Annotated` and, when it is `Optional`, without `None`."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if origin in (typing.Union, UnionType):
            members = [a for a in typing.get_args(annotation) if a is not type(None)]
            if len(members) == 1:
                annotation = members[0]
                continue
        return annotation


def is_group(annotation: Any) -> bool:
    inner = unwrap(annotation)
    return isinstance(inner, type) and issubclass(inner, SettingsGroup)


def leaves(cls: type, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    """Yield `(path, annotation, default)` for each setting of `cls` that is no group, and enter
    each group. A setting of the base classes of the flows is left out."""
    for name, info in cls.model_fields.items():  # type: ignore[attr-defined]
        if not path and name in BASE_SETTINGS:
            continue
        if is_group(info.annotation):
            yield from leaves(unwrap(info.annotation), (*path, name))
        else:
            yield (*path, name), info.annotation, info.default


def groups_of(cls: type, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], type]]:
    """Yield the path and the class of each group of `cls`, at any depth."""
    for name, info in cls.model_fields.items():  # type: ignore[attr-defined]
        if is_group(info.annotation):
            group = unwrap(info.annotation)
            yield (*path, name), group
            yield from groups_of(group, (*path, name))


def candidates(annotation: Any, default: Any, key: str) -> tuple[list[Any], bool]:
    """Return the values to try for a setting, and whether every accepted value is a variant.

    A switch and a choice take every value. A number, a quantity, a name or a list takes the
    first value that the settings accept."""
    inner = unwrap(annotation)
    origin = typing.get_origin(inner)
    if inner is bool:
        return [not default if isinstance(default, bool) else True], True
    if origin is typing.Literal:
        return list(typing.get_args(inner)), True
    if origin in (typing.Union, UnionType):
        found: list[Any] = []
        for member in typing.get_args(inner):
            if member is not type(None):
                found += candidates(member, default, key)[0]
        return found, False
    if origin is list:
        (member,) = typing.get_args(inner)
        values, every = candidates(member, None, key)
        if every:
            return [[value] for value in values], True
        return [[value] * length for value in values for length in (1, 2, 4)], False
    if origin is tuple:
        members = typing.get_args(inner)
        return [[candidates(m, None, key)[0][0] for m in members]], False
    if inner is int or inner is float:
        return list(NUMBERS), False
    if inner is str:
        return list(NAMES), False
    if isinstance(inner, type) and issubclass(inner, Quantity):
        unit = inner.canonical_unit
        return [inner.example, f"1 {unit}", f"100 {unit}", f"0.5 {unit}"], False
    return [], False


def nested(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Return the settings that give `value` to the setting at `path`."""
    for name in reversed(path):
        value = {name: value}
    return value


def merge(into: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Return `into` with the nested settings `given` merged in."""
    merged = dict(into)
    for key, value in given.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


#: The settings that make a setting take effect: a setting that a choice or a switch turns off is
#: refused, so a variant of it needs the choice. Each entry is a flow, the first part of the key of
#: the setting, and the settings (dotted keys) to give with it.
COMPANIONS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "openroad_place",
        "floorplan.netlist_cleanup.repair.hold_margin",
        {"floorplan.netlist_cleanup.repair.fix": "setup_and_hold"},
    ),
    (
        "openroad_place",
        "floorplan.netlist_cleanup.repair.allow_setup_violations",
        {"floorplan.netlist_cleanup.repair.fix": "setup_and_hold"},
    ),
    (
        "openroad_place",
        "floorplan.netlist_cleanup.repair.max_buffer_percent",
        {"floorplan.netlist_cleanup.repair.fix": "setup_and_hold"},
    ),
    (
        "openroad_place",
        "floorplan.netlist_cleanup.repair.moves",
        {"floorplan.netlist_cleanup.repair.sequence": ["sizeup"]},
    ),
    ("openroad_place", "pdn.check.require_terminals", {"pdn.check.mode": "warn"}),
    ("openroad_place", "io.annealing", {"io.algorithm": "annealing"}),
    (
        "openroad_place",
        "global_placement.density.headroom",
        {"global_placement.density.mode": "auto"},
    ),
    (
        "openroad_place",
        "global_placement.timing_driven.repair_tns_end_percent",
        {"global_placement.timing_driven.repair_timing": True},
    ),
    (
        "openroad_place",
        "global_placement.timing_driven.virtual_cts_max_skew_fraction",
        {"global_placement.timing_driven.virtual_cts": True},
    ),
    ("openroad_place", "arith_swap", {"hierarchical": True}),
    ("openroad_cts", "buffers.cells", {"buffers.select": "list"}),
    ("openroad_cts", "buffers.library", {"buffers.select": "library"}),
    ("openroad_cts", "buffers.footprint", {"buffers.select": "footprint"}),
    ("openroad_cts", "buffers.name_pattern", {"buffers.select": "name"}),
    ("openroad_cts", "sink_clustering.size", {"sink_clustering.mode": "explicit"}),
    ("openroad_cts", "sink_clustering.max_diameter", {"sink_clustering.mode": "explicit"}),
    ("openroad_cts", "tree.buffer_distance", {"tree.style": "simple_segments"}),
    ("openroad_cts", "tree.branch_buffer_distance", {"tree.style": "simple_segments"}),
    ("openroad_cts", "repair_clock_nets.max_wire_length", {"repair_clock_nets.when": "after"}),
    ("openroad_grt", "router.res_aware_nets_percent", {"router.resistance_aware": True}),
    ("openroad_grt", "router.capacity_perturbation_percent", {"router.seed": 1}),
    ("openroad_grt", "router.perturbation_amount", {"router.seed": 1}),
    ("openroad", "detailed_route.reroute_swap_factor", {"detailed_route.reroute_seed": 1}),
)


#: Whole settings that render flags no single leaf reaches with its own variants: a leaf renders
#: with its first value that a flow accepts, and the concurrent placement of the pins gives the
#: same leaves of `io` to `set_place_config` instead of `place_pins`.
VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "openroad_place",
        {
            "io.flow": "concurrent",
            "io.min_distance": "3 um",
            "io.corner_avoidance": "1 um",
            "io.exclude": [{"edge": "top"}],
        },
    ),
    ("openroad_place", {"io.flow": "concurrent", "io.min_distance_tracks": 3}),
)


def companion(flow: str, key: str) -> dict[str, Any]:
    """Return the nested settings that `COMPANIONS` gives for the setting `key` of `flow`."""
    given: dict[str, Any] = {}
    for name, prefix, settings in COMPANIONS:
        if name == flow and (key == prefix or key.startswith(prefix + ".")):
            for dotted, value in settings.items():
                given = merge(given, nested(tuple(dotted.split(".")), value))
    return given


@dataclass
class Rendered:
    """What the variants of the flows rendered."""

    pairs: set[tuple[str, str]] = field(default_factory=set)
    #: Settings no value of which any flow accepted.
    unreached: list[str] = field(default_factory=list)
    #: The number of variants that rendered.
    variants: int = 0
    #: A line that holds a command with a flag, by pair: for a message.
    where: dict[tuple[str, str], str] = field(default_factory=dict)


class Bench:
    """The `tiny` platform with every file the flows may read, a design and the files of a run,
    under `root`; and the flows built over them. `polish` is the value of the platform's
    `opt_post_grt_wns`."""

    def __init__(self, root: Path, polish: int = 1) -> None:
        self.root = root
        store = tiny_store(root, "store")
        (store / "tiny" / RCX_RULES).write_text("rules\n")
        (store / "tiny" / GDS_FILE).write_bytes(b"gds\n")
        from .test_openroad_final import KLAYOUT_TECH_TEXT

        (store / "tiny" / KLAYOUT_TECH).write_text(KLAYOUT_TECH_TEXT)
        self.store = store
        residue = variant_residue(root, rules=True, supply=SUPPLY, layout=True)
        data = yaml.safe_load(residue.read_text())
        data["values"]["opt_post_grt_wns"] = polish
        residue.write_text(yaml.safe_dump(data, sort_keys=False))
        self.residue = residue
        self.design = Design.from_file(openroad_design(root / "design"))
        self.inputs = root / "inputs"
        self.inputs.mkdir()
        for name in ("place", "cts", "grt"):
            (self.inputs / f"{name}.odb").write_text("odb\n")
            (self.inputs / f"{name}.sdc").write_text(
                "create_clock -name clk -period 5 [get_ports clk]\n"
            )
        self.work = root / "work"
        self.work.mkdir()

    def base_settings(self, flow: type) -> dict[str, Any]:
        """The settings of the platform for `flow`, and what the flow needs besides."""
        given = tiny_settings(self.store, residue=self.residue)
        if flow is OpenroadPlace:
            given["clocks"] = {"clk": {"period": "5 ns"}}
        if flow is Openroad:
            given["save_images"] = True
        return given

    def flow(self, cls: type, given: dict[str, Any]) -> Any:
        """Build `cls` with the settings `given` (nested), ready to render. Raise whatever the
        settings or the platform refuse with."""
        settings = cls.Settings.from_input(  # type: ignore[attr-defined]
            given, design_root=self.design.design_root, runner_cwd=self.root
        )
        cls.check_settings_supported(settings)  # type: ignore[attr-defined]
        flow = cls(settings, self.design, self.work)
        flow.init()
        if cls is OpenroadPlace:
            flow.inputs.netlist = self.design.design_root / "counter_netlist.v"
            flow.outputs.odb = self.work / "place.odb"
            flow.outputs.sdc = self.work / "place.sdc"
        else:
            previous = {"openroad_cts": "place", "openroad_grt": "cts", "openroad": "grt"}[cls.name]
            flow.inputs.odb = self.inputs / f"{previous}.odb"
            flow.inputs.sdc = self.inputs / f"{previous}.sdc"
            if cls is OpenroadCts:
                flow.outputs.odb = self.work / "cts.odb"
                flow.outputs.sdc = self.work / "cts.sdc"
            if cls is OpenroadGrt:
                flow.outputs.odb = self.work / "grt.odb"
                flow.outputs.sdc = self.work / "grt.sdc"
                flow.outputs.guides = self.work / "grt.guide"
        flow.prepare_inputs()
        return flow

    def scripts(self, cls: type, given: dict[str, Any]) -> dict[str, str]:
        """Render the drivers of `cls` with the settings `given`, by driver name."""
        flow = self.flow(cls, given)
        scripts = {}
        for driver in DRIVERS[cls.name]:
            if driver in ("openroad_final", "openroad_images"):
                flow.set_final_outputs()
            scripts[driver] = render_driver(flow, driver)
        return scripts


def enabling(cls: type, path: tuple[str, ...]) -> dict[str, Any]:
    """Return the settings that switch on each group on the way to the setting at `path`."""
    given: dict[str, Any] = {}
    group: Any = cls.Settings  # type: ignore[attr-defined]
    for depth, name in enumerate(path[:-1]):
        group = unwrap(group.model_fields[name].annotation)
        if "enabled" in group.model_fields:
            given = merge(given, nested((*path[: depth + 1], "enabled"), True))
    return given


def render_variants(root: Path) -> Rendered:
    """Render the drivers of the four flows for the defaults and for the variants of each
    setting, and collect the pairs. The flows run over a platform in `root`; `openroad_grt` runs
    over a second one too, which does not ask for the polish of the worst slack."""
    result = Rendered()
    reached_keys: set[str] = set()
    all_keys: set[str] = set()
    benches = {1: Bench(root / "polish", 1), 0: Bench(root / "no_polish", 0)}

    def collect(bench: Bench, cls: type, given: dict[str, Any]) -> bool:
        try:
            scripts = bench.scripts(cls, given)
        except Exception:  # a value the settings or the platform refuse is no variant
            return False
        result.variants += 1
        for driver, text in scripts.items():
            for pair in pairs_of_script(text):
                result.pairs.add(pair)
                result.where.setdefault(pair, f"{cls.name} {driver}")
        return True

    for polish, bench in benches.items():
        for cls in FLOWS.values():
            if polish == 0 and cls is not OpenroadGrt:
                continue
            base_given = bench.base_settings(cls)
            if not collect(bench, cls, base_given):
                raise AssertionError(f"{cls.name} does not render with its defaults")
            for name, settings in VARIANTS:
                if name != cls.name:
                    continue
                variant = base_given
                for dotted, value in settings.items():
                    variant = merge(variant, nested(tuple(dotted.split(".")), value))
                if not collect(bench, cls, variant):
                    raise AssertionError(f"{cls.name} does not render with {settings}")
            for path, annotation, default in leaves(cls.Settings):
                if path[-1] == "extra_args":
                    continue
                key = ".".join(path)
                all_keys.add(f"{cls.name}.{key}")
                values, every = candidates(annotation, default, key)
                reached = False
                for value in values:
                    for helper in (
                        {},
                        enabling(cls, path),
                        merge(enabling(cls, path), companion(cls.name, key)),
                    ):
                        given = merge(merge(base_given, helper), nested(path, value))
                        if collect(bench, cls, given):
                            reached = True
                            break
                    if reached and not every:
                        break
                if reached:
                    reached_keys.add(f"{cls.name}.{key}")
            if polish == 0:
                continue
            for path, group in groups_of(cls.Settings):
                for flag in sorted(group.EXTRA_FLAGS):
                    given = merge(
                        merge(base_given, enabling(cls, (*path, "extra_args"))),
                        nested((*path, "extra_args"), [flag, "1"]),
                    )
                    collect(bench, cls, given)
    result.unreached = sorted(all_keys - reached_keys)
    return result


def group_flags() -> dict[str, set[str]]:
    """Return, for each flow, the flags in the `FLAGS` tables of its groups."""
    return {
        name: {flag.flag for _, group in groups_of(cls.Settings) for flag in group.FLAGS}
        for name, cls in FLOWS.items()
    }
