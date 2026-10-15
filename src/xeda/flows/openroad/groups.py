"""The settings groups that more than one OpenROAD flow holds.

`repair_timing`, `repair_design` and the legalization after a repair run in several flows. Each
flow holds its own instance with its own defaults. A flow that needs other defaults than these
uses a small subclass in its groups module, so that the defaults of the class are the defaults of
the field.

A leaf whose default is `None` and that the platform can supply is not in `FLAGS`. The flow
resolves it first, and the template prints the result.
"""

from typing import Annotated, Any, ClassVar, Literal, Optional, Sequence, Tuple

from ...dataclass import Field, XedaBaseModel, extras, tier
from ...flow.settings_group import Flag, SettingsGroup
from ...units import Bounds, Length, Time, count, percent
from ...utils import tcl_word

__all__ = [
    "FIX_FLAGS",
    "Legalize",
    "RepairDesign",
    "RepairFix",
    "RepairMoves",
    "RepairPhase",
    "RepairTiming",
    "SEQUENCE_PHASES",
    "SetupMove",
    "ViaInPin",
    "comma_word",
    "differing_leaves",
    "whole_micrometers",
]

#: The moves that `repair_timing -sequence` takes, in the spelling of the tool. A name that the
#: tool does not know stops OpenROAD with an uncaught C++ exception, so the list is closed.
SetupMove = Literal[
    "buffer",
    "unbuffer",
    "swap",
    "sizeup",
    "size_down_fanout",
    "size",
    "clone",
    "split",
    "sizeup_match",
    "vt_swap",
    "reroute",
    "buffer_to_inverters",
]

#: The phases that `repair_timing -phases` takes, in the spelling of the tool (upper case).
RepairPhase = Literal[
    "LEGACY",
    "WNS",
    "WNS_PATH",
    "WNS_CONE",
    "TNS",
    "ENDPOINT_FANIN",
    "STARTPOINT_FANOUT",
    "LAST_GASP",
    "CRIT_VT_SWAP",
    "REROUTE",
    "GLOBAL_SIZING",
]

#: The checks that `repair_timing` repairs.
RepairFix = Literal["setup_and_hold", "setup", "hold"]

#: The words of `repair_timing` that stand for each value of `RepairFix`. With neither word, the
#: command repairs setup and hold.
FIX_FLAGS: dict[str, str] = {"setup_and_hold": "", "setup": "-setup", "hold": "-hold"}

#: The phases that build their moves from `-sequence`. The other phases choose their own moves.
SEQUENCE_PHASES = frozenset(
    {"LEGACY", "WNS", "WNS_PATH", "WNS_CONE", "TNS", "ENDPOINT_FANIN", "STARTPOINT_FANOUT"}
)

#: The moves of a sequence that a `-skip_*` flag removes, by the leaf of `RepairMoves` that
#: keeps the move. The other moves (`sizeup`, `sizeup_match` and `reroute`) have no such flag.
#: `size` stands for `sizeup` and `size_down_fanout`, so its switch drops half of it.
_MOVE_SWITCHES = {
    "buffer": "buffering",
    "unbuffer": "buffer_removal",
    "swap": "pin_swap",
    "size_down_fanout": "size_down_fanout",
    "size": "size_down_fanout",
    "clone": "gate_cloning",
    "split": "buffering",
    "vt_swap": "vt_swap",
    "buffer_to_inverters": "buffer_to_inverters",
}

#: The phases that a switch of `RepairTiming` drops from `-phases`, by the dotted names of the
#: leaves that keep the phase. The critical swap of the threshold voltage needs the swap too.
_PHASE_SWITCHES = {
    "LAST_GASP": ("last_gasp",),
    "CRIT_VT_SWAP": ("moves.crit_vt_swap", "moves.vt_swap"),
}

#: The leaves of `RepairTiming` that a power recovery reads. The tool ignores every other leaf of
#: the call when `recover_power_percent` is set.
_RECOVERY_LEAVES = frozenset({"enabled", "match_cell_footprint", "recover_power_percent"})


def whole_micrometers(value: Length) -> Length:
    """Return `value` if it is a whole number of micrometers, else raise `ValueError`. A few
    options of OpenROAD take integers of micrometers."""
    if not value.to("um").is_integer():
        raise ValueError(
            f"must be a whole number of micrometers, got {value}: the tool takes an integer"
        )
    return value


def comma_word(items: Sequence[Any]) -> str:
    """Return the items as one Tcl word with commas between them: `"unbuffer,sizeup"`.

    `repair_timing` reads `-sequence` in this form, and in the form of `space_word` too. It reads
    `-phases` only in the form of `space_word`: the installed OpenROAD rejects `"WNS,TNS"`. Pass
    it as the `list_word` of `render_flags`."""
    return tcl_word(",".join(str(item) for item in items))


def differing_leaves(group: SettingsGroup, key: str) -> list[str]:
    """Return the dotted keys of the leaves of `group` that differ from the leaves of a group of
    the same class with its defaults, sub-groups included. `key` is the dotted key of `group`.

    The leaves come in declaration order, depth first."""
    default = type(group)()
    found: list[str] = []
    for name in type(group).model_fields:
        value, original = getattr(group, name), getattr(default, name)
        if isinstance(value, SettingsGroup):
            found += differing_leaves(value, f"{key}.{name}")
        elif value != original:
            found.append(f"{key}.{name}")
    return found


class ViaInPin(XedaBaseModel):
    """The range of layers where a via may sit inside a pin. `pin_access` and `detailed_route`
    must get the same range."""

    bottom: str = Field(
        min_length=1,
        description="The lowest layer where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`).",
    )
    top: str = Field(
        min_length=1,
        description="The highest layer where a via may sit inside a pin "
        "(`-via_in_pin_top_layer`).",
    )


class RepairMoves(SettingsGroup):
    """The moves of the setup repair that the group switches off, one flag each. A move that is
    on is allowed to run. It does not run unless the sequence of `repair_timing` names it, or the
    default sequence of the tool has it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("pin_swap", "-skip_pin_swap", "skip"),
        Flag("gate_cloning", "-skip_gate_cloning", "skip"),
        Flag("size_down_fanout", "-skip_size_down", "skip"),
        Flag("buffering", "-skip_buffering", "skip"),
        Flag("buffer_removal", "-skip_buffer_removal", "skip"),
        Flag("buffer_to_inverters", "-skip_buffer_to_inverters", "skip"),
        Flag("vt_swap", "-skip_vt_swap", "skip"),
        Flag("crit_vt_swap", "-skip_crit_vt_swap", "skip"),
    )

    pin_swap: bool = Field(
        True,
        description="Allow the move that swaps the pins of a gate. False adds "
        "`-skip_pin_swap`, since a pin swap changes the connections of the gate.",
        json_schema_extra=tier("advanced"),
    )
    gate_cloning: bool = Field(
        True,
        description="Allow the move that clones a gate to split its load. False adds "
        "`-skip_gate_cloning`, since cloning adds cells.",
        json_schema_extra=tier("advanced"),
    )
    size_down_fanout: bool = Field(
        True,
        description="Allow the move that sizes down the gates on a net with many loads, off the "
        "critical path. False adds `-skip_size_down`. The default sequence of the tool does not "
        "use this move, so the switch matters only when `sequence` names `size_down_fanout` or "
        "`size`.",
        json_schema_extra=tier("advanced"),
    )
    buffering: bool = Field(
        True,
        description="Allow the moves that buffer a net again (`buffer`) and that split off the "
        "loads behind a buffer (`split`). False adds `-skip_buffering`.",
        json_schema_extra=tier("advanced"),
    )
    buffer_removal: bool = Field(
        True,
        description="Allow the move that removes a buffer from a critical path. False adds "
        "`-skip_buffer_removal`.",
        json_schema_extra=tier("advanced"),
    )
    buffer_to_inverters: bool = Field(
        True,
        description="Allow the move that replaces a buffer with two inverters. False adds "
        "`-skip_buffer_to_inverters`. The default sequence of the tool does not use this move, "
        "so the switch matters only when `sequence` names `buffer_to_inverters`.",
        json_schema_extra=tier("expert"),
    )
    vt_swap: bool = Field(
        True,
        description="Allow the move that swaps a cell for a cell of another threshold voltage. "
        "False adds `-skip_vt_swap`, which also stops the critical swap that ends the repair. "
        "The move has an effect only when the library has such cells.",
        json_schema_extra=tier("advanced"),
    )
    crit_vt_swap: bool = Field(
        True,
        description="Allow the swap of threshold voltage that ends the repair, on the cells of "
        "the critical paths. False adds `-skip_crit_vt_swap`. The swap has an effect only when "
        "the library has cells of several threshold voltages.",
        json_schema_extra=tier("advanced"),
    )


class RepairTiming(SettingsGroup):
    """One call of `repair_timing`. Each flow that calls it holds its own instance.

    The margins are times with units, so that a design file means the same on a platform whose
    Liberty files use picoseconds and on one that uses nanoseconds. A quantity has no entry in
    `FLAGS`, since `render_flags` prints a value as given. The template prints `-setup_margin` and
    `-hold_margin` and converts the margins to the time unit of the Liberty files. A leaf that the group prints is in `FLAGS`. The flow prints `fix` and
    `match_cell_footprint` itself: `fix` stands for `-setup` or `-hold` (`FIX_FLAGS`), and the
    platform decides `match_cell_footprint` when it is unset.

    `ALLOWS_REROUTE` says whether the instance may use the `reroute` move and the `REROUTE`
    phase. Both need the parasitics of a global route, so only the call after global routing sets
    it. A call with `recover_power_percent` recovers power and does nothing else, so its other
    leaves keep their defaults."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("allow_setup_violations", "-allow_setup_violations", "switch"),
        Flag("sequence", "-sequence", "list"),
        Flag("phases", "-phases", "list"),
        Flag("tns_end_percent", "-repair_tns", "value"),
        Flag("last_gasp", "-skip_last_gasp", "skip"),
        Flag("crpr", "-skip_crpr", "skip"),
        Flag("max_buffer_percent", "-max_buffer_percent", "value"),
        Flag("max_iterations", "-max_iterations", "value"),
        Flag("max_passes", "-max_passes", "value"),
        Flag("max_repairs_per_pass", "-max_repairs_per_pass", "value"),
        Flag("recover_power_percent", "-recover_power", "value"),
    )
    ALLOWS_REROUTE: ClassVar[bool] = False

    enabled: bool = Field(
        True,
        description="Run `repair_timing` at this point of the flow.",
        json_schema_extra=tier("common"),
    )
    fix: RepairFix = Field(
        "setup_and_hold",
        description="The checks to repair: `setup`, `hold`, or `setup_and_hold`, which repairs "
        "setup first and then hold (`-setup`, `-hold`, or neither).",
        json_schema_extra=tier("common"),
    )
    setup_margin: Annotated[Time, Bounds(ge="0 ns")] = Field(
        Time("0 ns"),
        description="Repair until the setup slack is at least this margin (`-setup_margin`), a "
        "time with a unit, such as `50 ps`. The hold repair keeps this margin of setup slack too. "
        "The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    hold_margin: Annotated[Time, Bounds(ge="0 ns")] = Field(
        Time("0 ns"),
        description="Repair until the hold slack is at least this margin (`-hold_margin`), a "
        "time with a unit, such as `20 ps`. The default is the one of the tool and of "
        "OpenROAD-flow-scripts. Not with `fix: setup`.",
        json_schema_extra=tier("common"),
    )
    allow_setup_violations: bool = Field(
        False,
        description="Let the hold repair insert buffers that cause setup violations "
        "(`-allow_setup_violations`). Without it, the repair inserts none of them. Not with "
        "`fix: setup`.",
        json_schema_extra=tier("advanced"),
    )
    tns_end_percent: float = Field(
        100.0,
        ge=0,
        le=100,
        description="The share of the endpoints with a setup violation that the repair works "
        "on, in percent (`-repair_tns`). 0 repairs only the worst path. The default is the one "
        "of the tool and of OpenROAD-flow-scripts. Not with `fix: hold`.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    sequence: list[SetupMove] = Field(
        [],
        description="The setup moves, in the order that the repair tries them (`-sequence`). "
        "Empty: the default sequence of the tool. A move that `moves` switches off must not "
        "appear. `reroute` needs the parasitics of a global route. Not with `fix: hold`.",
        json_schema_extra=tier("advanced"),
    )
    phases: list[RepairPhase] = Field(
        [],
        description="The phases of the setup repair, in order (`-phases`). Empty: the default "
        "phases of the tool. A phase that its switch turns off (`last_gasp` for `LAST_GASP`, "
        "`moves.crit_vt_swap` and `moves.vt_swap` for `CRIT_VT_SWAP`) must not appear. "
        "`REROUTE` needs the parasitics of a global route. Not with `fix: hold`.",
        json_schema_extra=tier("expert"),
    )
    moves: RepairMoves = Field(
        default_factory=RepairMoves,
        description="The setup moves that the repair may not use. Not with `fix: hold`.",
    )
    last_gasp: bool = Field(
        True,
        description="Run the last pass of the setup repair, a greedy sizing of the cells. False "
        "adds `-skip_last_gasp`, which saves run time and area. Not with `fix: hold`.",
        json_schema_extra=tier("advanced"),
    )
    crpr: bool = Field(
        True,
        description="Remove the clock reconvergence pessimism from the paths during the repair. "
        "False adds `-skip_crpr`, which saves run time at the cost of area and power. The "
        "timing that the repair sees then keeps the pessimism.",
        json_schema_extra=tier("expert"),
    )
    match_cell_footprint: Optional[bool] = Field(
        None,
        description="Swap a cell only for a cell with the same footprint in the Liberty file "
        "(`-match_cell_footprint`). Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    max_buffer_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The most hold buffers that the repair inserts, in percent of the cells "
        "(`-max_buffer_percent`). Unset: the default of the tool, 20. Needs hold repair: not "
        "with `fix: setup`.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    max_iterations: Optional[int] = Field(
        None,
        ge=1,
        description="The most iterations of the setup repair and of the hold repair "
        "(`-max_iterations`). Unset: no limit, which is the `-1` of the tool.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    max_passes: Optional[int] = Field(
        None,
        ge=1,
        description="The most passes of one run of the repair (`-max_passes`). The tool calls "
        "it an option for debugging. Unset: the default of the tool, 10000.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    max_repairs_per_pass: Optional[int] = Field(
        None,
        ge=1,
        description="The most repairs in one pass on the worst paths (`-max_repairs_per_pass`). "
        "The number goes down to 1 for the last violations. Unset: the default of the tool, 1. "
        "Not with `fix: hold`.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    recover_power_percent: Optional[float] = Field(
        None,
        gt=0,
        le=100,
        description="Trade positive slack for power on this share of the paths, in percent "
        "(`-recover_power`). The command then recovers power and repairs neither setup nor "
        "hold, so every other leaf of this call must keep its default, except "
        "`match_cell_footprint`. The documentation of OpenROAD advises the parasitics of a "
        "global route for it. Unset: no power recovery.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the leaves that cannot work together: a setup leaf with `fix: hold`, a hold leaf
        with `fix: setup`, a move that its switch turns off, and what needs a global route."""
        found: list[tuple[str, str]] = []
        default = type(self)()
        if self.fix == "hold":
            setup_leaves = [
                f"{key}.{name}"
                for name in (
                    "tns_end_percent",
                    "sequence",
                    "phases",
                    "last_gasp",
                    "max_repairs_per_pass",
                )
                if getattr(self, name) != getattr(default, name)
            ]
            setup_leaves += differing_leaves(self.moves, f"{key}.moves")
            for leaf in setup_leaves:
                found.append(
                    (
                        leaf,
                        f"`{leaf}` repairs setup, and `{key}.fix` is `hold`: set "
                        f"`{key}.fix: setup_and_hold` or delete `{leaf}`",
                    )
                )
        if self.fix == "setup":
            if self.hold_margin != default.hold_margin:
                found.append(
                    (
                        f"{key}.hold_margin",
                        f"`{key}.hold_margin` repairs hold, and `{key}.fix` is `setup`: set "
                        f"`{key}.fix: setup_and_hold` or delete `{key}.hold_margin`",
                    )
                )
            for name in ("max_buffer_percent", "allow_setup_violations"):
                if getattr(self, name) != getattr(default, name):
                    found.append(
                        (
                            f"{key}.{name}",
                            f"`{key}.{name}` repairs hold, and `{key}.fix` is `setup`: "
                            f"set `{key}.fix: setup_and_hold` or delete `{key}.{name}`",
                        )
                    )
        for move in self.sequence:
            switch = _MOVE_SWITCHES.get(move)
            if switch is not None and not getattr(self.moves, switch):
                dropped = "the sizing down that it includes" if move == "size" else "the move"
                found.append(
                    (
                        f"{key}.sequence",
                        f"`{key}.sequence` names `{move}`, and `{key}.moves.{switch}` is "
                        f"false, so the tool drops {dropped}: delete one of them",
                    )
                )
        for phase in self.phases:
            for leaf in _PHASE_SWITCHES.get(phase, ()):
                holder: Any = self
                for part in leaf.split("."):
                    holder = getattr(holder, part)
                if not holder:
                    found.append(
                        (
                            f"{key}.phases",
                            f"`{key}.phases` names `{phase}`, and `{key}.{leaf}` is false, so "
                            f"the tool drops the phase: delete one of them",
                        )
                    )
        if self.sequence and self.phases and not SEQUENCE_PHASES.intersection(self.phases):
            readers = ", ".join(sorted(SEQUENCE_PHASES))
            found.append(
                (
                    f"{key}.sequence",
                    f"`{key}.sequence` has no effect with the phases in `{key}.phases`, which "
                    f"choose their own moves: add a phase that reads the sequence ({readers}) "
                    f"or delete `{key}.sequence`",
                )
            )
        if self.recover_power_percent is not None:
            for leaf in differing_leaves(self, key):
                if leaf.removeprefix(f"{key}.") in _RECOVERY_LEAVES:
                    continue
                found.append(
                    (
                        leaf,
                        f"`{key}.recover_power_percent` recovers power instead of repairing "
                        f"setup and hold, so `{leaf}` has no effect: delete `{leaf}` or delete "
                        f"`{key}.recover_power_percent`",
                    )
                )
        if not self.ALLOWS_REROUTE:
            if "reroute" in self.sequence:
                found.append(
                    (
                        f"{key}.sequence",
                        f"`reroute` needs the parasitics of a global route, and this call runs "
                        f"before global routing: delete `reroute` from `{key}.sequence`",
                    )
                )
            if "REROUTE" in self.phases:
                found.append(
                    (
                        f"{key}.phases",
                        f"`REROUTE` needs the parasitics of a global route, and this call runs "
                        f"before global routing: delete `REROUTE` from `{key}.phases`",
                    )
                )
        return found


class RepairDesign(SettingsGroup):
    """One call of `repair_design`: it fixes the slew, capacitance and fanout violations and
    long wires by buffering and sizing. Each flow that calls it holds its own instance.

    The flow prints `match_cell_footprint`: the platform decides it when it is unset.

    `ALLOWS_REROUTE` says whether the instance may use `reroute`, which needs the parasitics of a
    global route. Only the call after global routing sets it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("slew_margin_percent", "-slew_margin", "value"),
        Flag("cap_margin_percent", "-cap_margin", "value"),
        Flag("reroute", "-reroute", "switch"),
    )
    ALLOWS_REROUTE: ClassVar[bool] = False

    enabled: bool = Field(
        True,
        description="Run `repair_design` at this point of the flow.",
        json_schema_extra=tier("common"),
    )
    slew_margin_percent: float = Field(
        0.0,
        ge=0,
        lt=100,
        description="Repair the slew with this margin, in percent of the limit "
        "(`-slew_margin`). The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    cap_margin_percent: float = Field(
        0.0,
        ge=0,
        lt=100,
        description="Repair the capacitance with this margin, in percent of the limit "
        "(`-cap_margin`). The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    max_wire_length: Optional[Annotated[Length, Bounds(ge="0 um")]] = Field(
        None,
        description="Buffer each wire that is longer than this length, a length with a unit "
        "(`-max_wire_length`). Unset: the flow does not pass the flag.",
        json_schema_extra=tier("advanced"),
    )
    match_cell_footprint: Optional[bool] = Field(
        None,
        description="Swap a cell only for a cell with the same footprint in the Liberty file "
        "(`-match_cell_footprint`). Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    reroute: bool = Field(
        False,
        description="Reroute wires with resistance-aware routing to fix slew violations "
        "(`-reroute`). The tool calls it experimental. It needs the parasitics of a global "
        "route, so only the call after global routing accepts it.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the rerouting where no global route exists yet."""
        if self.reroute and not self.ALLOWS_REROUTE:
            return [
                (
                    f"{key}.reroute",
                    f"`{key}.reroute` needs the parasitics of a global route, and this call runs "
                    f"before global routing: delete `{key}.reroute`",
                )
            ]
        return []


class Legalize(SettingsGroup):
    """The legalization of the placement after a repair or after the clock tree. The flow runs
    `detailed_placement` there with this padding."""

    cell_padding: int = Field(
        0,
        ge=0,
        description="The empty sites that each cell keeps on its left and on its right "
        "(`set_placement_padding -global -left N -right N`), in sites. The default is the one of "
        "OpenROAD-flow-scripts. Use the value of `detailed_placement.cell_padding` of "
        "`openroad_place`, so that both legalizations leave the same space.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
