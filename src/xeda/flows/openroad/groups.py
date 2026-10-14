"""The settings groups that more than one OpenROAD flow holds.

`repair_timing`, `repair_design` and the legalization after a repair run in several flows. Each
flow holds its own instance with its own defaults. A flow that needs other defaults than these
uses a small subclass in its groups module, so that the defaults of the class are the defaults of
the field.

A leaf whose default is `None` and that the platform can supply is not in `FLAGS`. The flow
resolves it first, and the template prints the result.
"""

from typing import Any, ClassVar, Literal, Optional, Sequence, Tuple

from ...dataclass import Field, tier
from ...flow.settings_group import Flag, SettingsGroup
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
    "comma_word",
    "differing_leaves",
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
#: keeps the move. The other moves have no such flag.
_MOVE_SWITCHES = {
    "unbuffer": "buffer_removal",
    "swap": "pin_swap",
    "clone": "gate_cloning",
    "vt_swap": "vt_swap",
}


def comma_word(items: Sequence[Any]) -> str:
    """Return the items as one Tcl word with commas between them: `"unbuffer,sizeup"`.

    `repair_timing` reads `-sequence` and `-phases` in this form. Pass it as the `list_word` of
    `render_flags`."""
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


class RepairMoves(SettingsGroup):
    """The moves of the setup repair that the group switches off, one flag each. A move that is
    on is allowed to run. It does not run unless the sequence of `repair_timing` names it, or the
    default sequence of the tool has it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("pin_swap", "-skip_pin_swap", "skip"),
        Flag("gate_cloning", "-skip_gate_cloning", "skip"),
        Flag("buffer_removal", "-skip_buffer_removal", "skip"),
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
    buffer_removal: bool = Field(
        True,
        description="Allow the move that removes a buffer from a critical path. False adds "
        "`-skip_buffer_removal`.",
        json_schema_extra=tier("advanced"),
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

    The margins are in the time unit of the Liberty files of the platform, as the tool reads
    them. A leaf that the group prints is in `FLAGS`. The flow prints `fix` and
    `match_cell_footprint` itself: `fix` stands for `-setup` or `-hold` (`FIX_FLAGS`), and the
    platform decides `match_cell_footprint` when it is unset.

    `ALLOWS_REROUTE` says whether the instance may use the `reroute` move and the `REROUTE`
    phase. Both need the parasitics of a global route, so only the call after global routing sets
    it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("setup_margin", "-setup_margin", "value"),
        Flag("hold_margin", "-hold_margin", "value"),
        Flag("sequence", "-sequence", "list"),
        Flag("phases", "-phases", "list"),
        Flag("tns_end_percent", "-repair_tns", "value"),
        Flag("last_gasp", "-skip_last_gasp", "skip"),
        Flag("max_buffer_percent", "-max_buffer_percent", "value"),
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
    setup_margin: float = Field(
        0.0,
        ge=0,
        description="Repair until the setup slack is at least this margin (`-setup_margin`), in "
        "the time unit of the Liberty files. The hold repair keeps this margin of setup slack "
        "too. The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    hold_margin: float = Field(
        0.0,
        ge=0,
        description="Repair until the hold slack is at least this margin (`-hold_margin`), in "
        "the time unit of the Liberty files. The default is the one of the tool and of "
        "OpenROAD-flow-scripts. Not with `fix: setup`.",
        json_schema_extra=tier("common"),
    )
    tns_end_percent: float = Field(
        100.0,
        ge=0,
        le=100,
        description="The share of the endpoints with a setup violation that the repair works "
        "on, in percent (`-repair_tns`). 0 repairs only the worst path. The default is the one "
        "of the tool and of OpenROAD-flow-scripts. Not with `fix: hold`.",
        json_schema_extra=tier("advanced"),
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
        "phases of the tool. `REROUTE` needs the parasitics of a global route. Not with `fix: "
        "hold`.",
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
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the leaves that cannot work together: a setup leaf with `fix: hold`, a hold leaf
        with `fix: setup`, a move that its switch turns off, and what needs a global route."""
        found: list[tuple[str, str]] = []
        default = type(self)()
        if self.fix == "hold":
            setup_leaves = [
                f"{key}.{name}"
                for name in ("tns_end_percent", "sequence", "phases", "last_gasp")
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
            if self.max_buffer_percent is not None:
                found.append(
                    (
                        f"{key}.max_buffer_percent",
                        f"`{key}.max_buffer_percent` repairs hold, and `{key}.fix` is `setup`: "
                        f"set `{key}.fix: setup_and_hold` or delete `{key}.max_buffer_percent`",
                    )
                )
        for move in self.sequence:
            switch = _MOVE_SWITCHES.get(move)
            if switch is not None and not getattr(self.moves, switch):
                found.append(
                    (
                        f"{key}.sequence",
                        f"`{key}.sequence` names `{move}`, and `{key}.moves.{switch}` is "
                        f"false, so the tool drops the move: delete one of them",
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

    The flow prints `match_cell_footprint`: the platform decides it when it is unset."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("max_wire_length", "-max_wire_length", "value"),
        Flag("slew_margin", "-slew_margin", "value"),
        Flag("cap_margin", "-cap_margin", "value"),
    )

    enabled: bool = Field(
        True,
        description="Run `repair_design` at this point of the flow.",
        json_schema_extra=tier("common"),
    )
    slew_margin: float = Field(
        0.0,
        ge=0,
        lt=100,
        description="Repair the slew with this margin, in percent of the limit "
        "(`-slew_margin`). The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    cap_margin: float = Field(
        0.0,
        ge=0,
        lt=100,
        description="Repair the capacitance with this margin, in percent of the limit "
        "(`-cap_margin`). The default is the one of the tool and of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    max_wire_length: Optional[float] = Field(
        None,
        ge=0,
        description="Buffer each wire that is longer than this length, in micrometers "
        "(`-max_wire_length`). Unset: the flow does not pass the flag.",
        json_schema_extra=tier("advanced"),
    )
    match_cell_footprint: Optional[bool] = Field(
        None,
        description="Swap a cell only for a cell with the same footprint in the Liberty file "
        "(`-match_cell_footprint`). Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )


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
        json_schema_extra=tier("advanced"),
    )
