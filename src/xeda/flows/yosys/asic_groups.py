"""The settings groups of the `yosys` flow that act on a mapping to a liberty library.

Each group holds the options of one stage of the synthesis. A leaf that is `None` takes the value
of the platform, when the platform has one. `Yosys.asic_values` resolves every leaf, and the
scripts read that result. A group never changes after validation.

`coarse` and `memory` have no leaves yet.
"""

from pathlib import Path
from typing import Annotated, List, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import SettingsGroup

__all__ = [
    "AbcGroup",
    "ArithmeticGroup",
    "CoarseGroup",
    "FinishGroup",
    "HiLoMap",
    "HierarchyGroup",
    "MemoryGroup",
    "SequentialGroup",
]


class HiLoMap(XedaBaseModel):
    """The cells that drive the constant nets, for `hilomap`."""

    hi: Tuple[str, str]
    lo: Tuple[str, str]
    singleton: bool = True


class HierarchyGroup(SettingsGroup):
    """The hierarchy of the design during synthesis."""

    keep_min_cost: Optional[int] = Field(
        None,
        ge=1,
        description="Keep each module whose cost is above this many NAND2 gate equivalents "
        "(`keep_hierarchy -min_cost`). Unset: flatten the design. Needs a yosys with Tcl and "
        "`script_format: tcl`.",
        json_schema_extra=tier("common"),
    )


class CoarseGroup(SettingsGroup):
    """The coarse-grain passes of the synthesis. It has no setting yet."""


class ArithmeticGroup(SettingsGroup):
    """The mapping of adders and other arithmetic operators."""

    adder_architecture: Literal["yosys", "kogge_stone", "han_carlson", "sklansky"] = Field(
        "yosys",
        description="Carry-lookahead architecture of the adders. `yosys` keeps the architecture "
        "of yosys (Brent-Kung); the other values add `-extra-map +/choices/<name>.v` to `synth`.",
        json_schema_extra=tier("common"),
    )
    adder_map: Literal["platform", "none"] = Field(
        "platform",
        description="`platform` maps full adders to the full-adder cells of the platform "
        "(`extract_fa` and the map file of the platform, or `adder_map_file`) when there is a "
        "map. `none` maps no full adders.",
        json_schema_extra=tier("common"),
    )
    adder_map_file: Optional[Path] = Field(
        None,
        description="Verilog map of full adders, used with `extract_fa` instead of the map of "
        "the platform. Not with `adder_map: none`.",
        json_schema_extra=tier("advanced"),
    )
    wrap_operators: bool = Field(
        False,
        description="Wrap wide operators in modules with alternative architectures, for the "
        "place and route tool to choose from. Needs a yosys with Tcl, `script_format: tcl` and "
        "`abc.mapper: abc_new`. The netlist keeps its attributes.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse `adder_map_file` together with `adder_map: none`."""
        if self.adder_map == "none" and self.adder_map_file is not None:
            return [
                (
                    f"{key}.adder_map_file",
                    f"`{key}.adder_map_file` has no effect with `{key}.adder_map: none`: delete "
                    "one of them",
                )
            ]
        return []


class MemoryGroup(SettingsGroup):
    """The mapping of memories. It has no setting yet."""


class SequentialGroup(SettingsGroup):
    """The mapping of latches, clock gates and flip-flops."""

    latch_map: Optional[Path] = Field(
        None,
        description="Verilog map of latches, applied with `techmap -map` after `dfflegalize`. "
        "Unset: the map of the platform.",
        json_schema_extra=tier("advanced"),
    )
    clockgate_map: Optional[Path] = Field(
        None,
        description="Verilog file with the clock-gating cells (`read_verilog -defer`). Unset: "
        "the file of the platform.",
        json_schema_extra=tier("advanced"),
    )
    dff_liberty: Optional[Path] = Field(
        None,
        description="Liberty file for mapping flip-flops. It replaces the Liberty files on "
        "`dfflibmap`; `abc` still gets the Liberty files.",
        json_schema_extra=tier("advanced"),
    )


class AbcGroup(SettingsGroup):
    """The mapping of the logic with ABC."""

    strategy: Literal["area", "speed", "speed_roundtrip", "yosys"] = Field(
        "area",
        description="The ABC mapping script. `area` and `speed` are the scripts of "
        "OpenROAD-flow-scripts, `speed_roundtrip` is the former speed script of Xeda (it moves "
        "the network between ABC and yosys in each round), and `yosys` keeps the default script "
        "of yosys. `abc_script` replaces the script of the strategy.",
        json_schema_extra=tier("common"),
    )
    mapper: Literal["abc", "abc_new"] = Field(
        "abc",
        description="The yosys command that runs ABC. `abc_new` needs a yosys with Tcl and "
        "`script_format: tcl`. It maps with its own script, so it refuses `abc.strategy` other "
        "than `area` and `abc_script`.",
        json_schema_extra=tier("advanced"),
    )
    driver_cell: Optional[str] = Field(
        None,
        description="Cell that ABC assumes drives the inputs (`set_driving_cell`). Unset: the "
        "cell of the platform.",
        json_schema_extra=tier("common"),
    )
    load: Optional[float] = Field(
        None,
        ge=0,
        description="Load that ABC assumes on the outputs (`set_load`), in units of flip-flop "
        "input capacitance. Unset: the load of the platform.",
        json_schema_extra=tier("common"),
    )
    delay_target: Union[Literal["auto"], Annotated[float, Field(ge=0)], None] = Field(
        None,
        description="Delay target of ABC in picoseconds (`-D`). `auto` is the period of the "
        "main clock. Unset: no `-D`.",
        json_schema_extra=tier("advanced"),
    )


class FinishGroup(SettingsGroup):
    """The last commands before the netlist is written."""

    tie_undefined: Literal["zero", "one", "none"] = Field(
        "zero",
        description="The value of the undefined bits (`setundef`). `none` runs no `setundef`.",
        json_schema_extra=tier("common"),
    )
    tie_cells: Optional[HiLoMap] = Field(
        None,
        description="Tie-high and tie-low cells that drive the constant nets (`hilomap`). Unset: "
        "the cells of the platform.",
        json_schema_extra=tier("common"),
    )
    buffer_cell: Optional[Tuple[str, str, str]] = Field(
        None,
        description="Buffer cell, its input port and its output port, for `insbuf`. Unset: the "
        "minimum buffer of the platform.",
        json_schema_extra=tier("common"),
    )
