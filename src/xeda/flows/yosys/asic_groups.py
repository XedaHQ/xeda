"""The settings groups of the `yosys` flow that act on a mapping to a liberty library.

Each group holds the options of one stage of the synthesis. A leaf that is `None` takes the value
of the platform, when the platform has one. `Yosys.asic_values` resolves every leaf, and the
scripts read that result. A group never changes after validation.

A leaf that acts on the `synth` command is an option of it. The script runs `synth` in two parts:
the coarse part (`-run :fine`) and the fine part (`-run fine: -noabc`). The options of the coarse
part go on the first command only. A pass that `synth` runs with fixed options has a leaf only
when `synth` has an option for it.
"""

from pathlib import Path
from typing import Annotated, Any, ClassVar, List, Literal, Optional, Tuple, Union

from pydantic import ValidatorFunctionWrapHandler
from pydantic_core import PydanticCustomError

from ...dataclass import (
    Field,
    ValidationError,
    XedaBaseModel,
    accepts_non_mapping,
    extras,
    field_validator,
    model_validator,
    tier,
)
from ...flow.settings_group import Flag, SettingsGroup
from ...units import Bounds, Capacitance, Time, count, ratio

__all__ = [
    "AbcGroup",
    "ArithmeticGroup",
    "COMPOSED_STRATEGIES",
    "ClockGatingGroup",
    "CoarseGroup",
    "ExtractFaGroup",
    "FinishGroup",
    "HiLoMap",
    "HierarchyGroup",
    "MemoryGroup",
    "SequentialGroup",
    "TECHMAP_POSITIONS",
    "TechmapFile",
]


class HiLoMap(XedaBaseModel):
    """The cells that drive the constant nets, for `hilomap`."""

    hi: Tuple[str, str]
    lo: Tuple[str, str]
    singleton: bool = True


#: The points of the script where a user map can run, in the order of the script.
TECHMAP_POSITIONS = ("pre_synth", "post_coarse", "pre_dff", "post_dff")


class TechmapFile(XedaBaseModel):
    """A Verilog map that the script applies with `techmap -map` at one point of the script.

    A bare path is the shorthand for a map at `pre_dff`."""

    path: Path = Field(
        description="The Verilog map. A relative path is relative to the design root.",
    )
    when: Literal["pre_synth", "post_coarse", "pre_dff", "post_dff"] = Field(
        "pre_dff",
        description="Where the script applies the map: `pre_synth` before the first part of "
        "`synth`; `post_coarse` after the coarse part of `synth` and the memory mapping, before "
        "the fine part; `pre_dff` after the maps of the latches and the platform, before clock "
        "gating and the flip-flop mapping; `post_dff` after the flip-flop mapping.",
    )

    @model_validator(mode="before")
    @classmethod
    @accepts_non_mapping
    def _bare_path(cls, value):
        """Read a bare path as a map at the default position."""
        return {"path": value} if isinstance(value, (str, Path)) else value


class HierarchyGroup(SettingsGroup):
    """The hierarchy of the design during synthesis."""

    keep_min_cost: Optional[int] = Field(
        None,
        ge=1,
        description="Keep each module whose cost is above this many NAND2 gate equivalents "
        "(`keep_hierarchy -min_cost`). Unset: flatten the design. Needs a yosys with Tcl and "
        "`script_format: tcl`.",
        json_schema_extra=extras(tier("common"), count()),
    )
    keep_instances: List[str] = Field(
        [],
        description="Instances that keep their module. Each item is a yosys selection of cells, "
        "such as `top/u_core`, and gets the `keep_hierarchy` attribute before the design is "
        "flattened. Not with `flatten: false`, which keeps every module.",
        json_schema_extra=tier("advanced"),
    )
    separator: Optional[str] = Field(
        None,
        min_length=1,
        max_length=1,
        pattern=r"^[^\s\"]$",
        description="The character that joins the names of the levels of a flattened design. "
        "Unset: `.`, the character of yosys. The place and route tool splits the names at its "
        "own divider. Not with `flatten: false`.",
        json_schema_extra=tier("advanced"),
    )
    cross_boundary_opt: bool = Field(
        False,
        description="Optimize across the boundaries of the modules that stay separate "
        "(`synth -hieropt`): constants and unused ports pass through them. It acts only on "
        "modules that stay, such as `keep_hierarchy`, `hierarchy.keep_instances`, "
        "`hierarchy.keep_min_cost`, a `keep_hierarchy` attribute in the sources, or all modules "
        "with `flatten: false`. With `arithmetic.wrap_operators`, it acts in the coarse part "
        "only, since wrapping replaces the fine part of `synth`.",
        json_schema_extra=tier("advanced"),
    )


class CoarseGroup(SettingsGroup):
    """The coarse-grain passes of the synthesis. `synth` runs most of them with fixed options,
    so a pass has a leaf only when `synth` has an option for it, or when the script runs the
    command itself."""

    fsm: bool = Field(
        True,
        description="Extract and optimize finite state machines (`fsm`). False adds `-nofsm` "
        "to `synth`.",
        json_schema_extra=tier("advanced"),
    )
    share: bool = Field(
        True,
        description="Merge operators that cannot be active together, with a SAT solver "
        "(`share`). False adds `-noshare` to `synth`.",
        json_schema_extra=tier("advanced"),
    )
    latches: Optional[Literal["warn", "error"]] = Field(
        None,
        description="How `proc` reports an inferred latch. Unset: the default of yosys, which "
        "reports it in the log (`info` or `auto`, by release). `warn` prints a warning and "
        "`error` stops the run.",
        json_schema_extra=tier("advanced"),
    )
    opt_dff_sat: bool = Field(
        False,
        description="Remove flip-flops that a SAT solver proves constant or equal to another "
        "(`-sat` on the `opt` after `synth`). It costs run time. `synth` does not run it.",
        json_schema_extra=tier("advanced"),
    )


class ExtractFaGroup(SettingsGroup):
    """The options of `extract_fa`, which finds adders in the gate-level design. It runs only
    when the full-adder map runs: that is, when `arithmetic.adder_map` is `platform` and a map
    exists. It is skipped when operator wrapping has already extracted an operator, as in
    OpenROAD-flow-scripts, and when the platform has no adder map."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("max_depth", "-d", "value"),
        Flag("max_breadth", "-b", "value"),
    )

    full: bool = Field(
        True,
        description="Extract full adders.",
        json_schema_extra=tier("advanced"),
    )
    half: bool = Field(
        True,
        description="Extract half adders.",
        json_schema_extra=tier("advanced"),
    )
    max_depth: Optional[int] = Field(
        None,
        ge=1,
        description="The deepest logic cone to search (`-d`). Unset: 20, the value of yosys.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    max_breadth: Optional[int] = Field(
        None,
        ge=1,
        description="The widest logic cone to search (`-b`). Unset: 6, the value of yosys.",
        json_schema_extra=extras(tier("advanced"), count()),
    )


class ArithmeticGroup(SettingsGroup):
    """The mapping of adders and other arithmetic operators."""

    adder_architecture: Literal["yosys", "kogge_stone", "han_carlson", "sklansky"] = Field(
        "yosys",
        description="Carry-lookahead architecture of the adders. `yosys` keeps the architecture "
        "of yosys (Brent-Kung); the other values add `-extra-map +/choices/<name>.v` to `synth`. "
        "Not with `wrap_operators`, which replaces that part of `synth`: choose the "
        "architectures in `wrapped_adders`.",
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
        "the platform. Not with `adder_map: none`. The map runs only when the design has no "
        "operator wrapped yet (operator wrapping skips it, as OpenROAD-flow-scripts does).",
        json_schema_extra=tier("advanced"),
    )
    wrap_operators: bool = Field(
        False,
        description="Wrap wide operators in modules with alternative architectures, for the "
        "place and route tool to choose from. Needs a yosys with Tcl and `script_format: tcl`. "
        "The netlist keeps its attributes. With `abc.mapper: abc_new`, ABC maps each wrapped "
        "operator with the script that the operator carries, as OpenROAD-flow-scripts does. With "
        "`abc`, ABC maps every module with the script of `abc.strategy`.",
        json_schema_extra=tier("advanced"),
    )
    wrapped_adders: List[Literal["brent_kung", "han_carlson", "kogge_stone", "sklansky"]] = Field(
        [],
        description="The architectures of the wrapped adders, in order. The first one builds the "
        "module that the design instantiates. Only the named architectures get a module. Unset: "
        "`han_carlson`, `kogge_stone`, `sklansky` and `brent_kung`, the order of "
        "OpenROAD-flow-scripts. Needs `wrap_operators`.",
        json_schema_extra=tier("advanced"),
    )
    wrapped_multipliers: List[Literal["booth", "base"]] = Field(
        [],
        description="The architectures of the wrapped multipliers, in order. The first one builds "
        "the module that the design instantiates. Only the named architectures get a module. "
        "Unset: `booth`, then `base`. Needs `wrap_operators`.",
        json_schema_extra=tier("advanced"),
    )
    alumacc: bool = Field(
        True,
        description="Warning: without the `$alu` and `$macc` cells, the adder architecture has "
        "nothing to map. True merges the arithmetic cells into them (`alumacc`). False adds "
        "`-noalumacc` to `synth`.",
        json_schema_extra=tier("expert"),
    )
    multiplier: Literal["default", "booth"] = Field(
        "default",
        description="`booth` maps multipliers to radix-4 Booth multipliers (`synth -booth`). "
        "The Booth pass has an effect on multipliers of at least 4x4 bits with at least 8 bits "
        "of result. Not with `wrap_operators`: the wrapped multipliers decide.",
        json_schema_extra=tier("advanced"),
    )
    arith_tree: bool = Field(
        False,
        description="Convert chains of additions and `$macc` cells to carry-save adder trees "
        "(`synth -arith_tree`). Needs a yosys that has this option.",
        json_schema_extra=tier("expert"),
    )
    extract_fa: ExtractFaGroup = Field(
        default_factory=ExtractFaGroup,
        description="The options of `extract_fa`. They act only when the full-adder map runs: "
        "operator wrapping skips it when it extracts an operator, and a platform with no adder "
        "map has none to run.",
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse the settings of the arithmetic group that cannot work together."""
        found: List[Tuple[str, str]] = []
        if self.adder_map == "none" and self.adder_map_file is not None:
            found.append(
                (
                    f"{key}.adder_map_file",
                    f"`{key}.adder_map_file` has no effect with `{key}.adder_map: none`: delete "
                    "one of them",
                )
            )
        if self.adder_map == "none":
            default = ExtractFaGroup()
            for name in ExtractFaGroup.model_fields:
                if getattr(self.extract_fa, name) != getattr(default, name):
                    found.append(
                        (
                            f"{key}.extract_fa.{name}",
                            f"`{key}.extract_fa.{name}` has no effect with "
                            f"`{key}.adder_map: none`, which runs no `extract_fa`: delete one "
                            "of them",
                        )
                    )
        if self.adder_map == "platform" and not (self.extract_fa.full or self.extract_fa.half):
            found.append(
                (
                    f"{key}.extract_fa.full",
                    f"`{key}.extract_fa.full` and `{key}.extract_fa.half` are both false, so "
                    "`extract_fa` has nothing to extract: set one of them to true, or set "
                    f"`{key}.adder_map: none`",
                )
            )
        if self.adder_architecture != "yosys" and self.wrap_operators:
            found.append(
                (
                    f"{key}.adder_architecture",
                    f"`{key}.adder_architecture: {self.adder_architecture}` has no effect with "
                    f"`{key}.wrap_operators`, which replaces the part of `synth` that maps the "
                    f"adders: choose the architectures in `{key}.wrapped_adders`",
                )
            )
        if self.adder_architecture != "yosys" and not self.alumacc:
            found.append(
                (
                    f"{key}.adder_architecture",
                    f"`{key}.adder_architecture` maps the `$lcu` cells of the `$alu` cells, and "
                    f"`{key}.alumacc: false` leaves no `$alu` cell: delete one of them",
                )
            )
        if self.wrap_operators and self.multiplier != "default":
            found.append(
                (
                    f"{key}.multiplier",
                    f"`{key}.multiplier: {self.multiplier}` has no effect with "
                    f"`{key}.wrap_operators`, which wraps the multipliers: choose the "
                    f"architectures in `{key}.wrapped_multipliers`",
                )
            )
        for name in ("wrapped_adders", "wrapped_multipliers"):
            names = getattr(self, name)
            if names and not self.wrap_operators:
                found.append(
                    (
                        f"{key}.{name}",
                        f"`{key}.{name}` has no effect without `{key}.wrap_operators`: set "
                        f"`{key}.wrap_operators: true` or delete it",
                    )
                )
            for repeated in sorted({item for item in names if names.count(item) > 1}):
                found.append(
                    (
                        f"{key}.{name}",
                        f"`{key}.{name}` names `{repeated}` more than once: name each "
                        "architecture once",
                    )
                )
        return found


class MemoryGroup(SettingsGroup):
    """The mapping of memories to a library of memory cells and to flip-flops."""

    libmap: List[Path] = Field(
        [],
        description="Memory library files (`memory_libmap -lib`). The script maps the memories "
        "that the coarse part of `synth` collected to the cells of the libraries. The cells "
        "need a map: see `techmap`. The format is the one of the `memlib` of yosys.",
        json_schema_extra=tier("advanced"),
    )
    techmap: Optional[Path] = Field(
        None,
        description="Verilog map of the cells of the memory libraries (`techmap -map`), run "
        "after `memory_libmap`.",
        json_schema_extra=tier("advanced"),
    )
    max_bits: Optional[int] = Field(
        None,
        ge=1,
        description="The largest memory that the synthesis turns into flip-flops, in bits "
        "(rows times width). A larger memory stops the run, or is shrunk with `mock_large`. "
        "Unset: no limit. The check runs on the memories after the coarse part of `synth`. "
        "Needs a yosys with Tcl and `script_format: tcl`.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    mock_large: bool = Field(
        False,
        description="Shrink each memory above `max_bits` to one row, as OpenROAD-flow-scripts "
        "does. The netlist is not a correct implementation then. The log lists the shrunk "
        "memories. Needs `max_bits`, a yosys with Tcl and `script_format: tcl`.",
        json_schema_extra=tier("advanced"),
    )
    keep_mocked: bool = Field(
        True,
        description="Give the module of each mocked memory the `keep_hierarchy` attribute, as "
        "OpenROAD-flow-scripts does. The attribute keeps the module separate only if nothing "
        "flattened it before. Needs `mock_large`.",
        json_schema_extra=tier("advanced"),
    )
    merge_ff: bool = Field(
        True,
        description="Merge flip-flops into the read ports of memories. False adds `-nordff` to "
        "`synth`.",
        json_schema_extra=tier("expert"),
    )
    rw_check: bool = Field(
        True,
        description="Warning: with false, the netlist can differ from the simulation when a read "
        "and a write meet on one address. True keeps the value that the design defines. False "
        "marks every read port as returning any value then (`synth -no-rw-check`).",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse mocking without a limit and `keep_mocked: false` without mocking."""
        found: List[Tuple[str, str]] = []
        if self.mock_large and self.max_bits is None:
            found.append(
                (
                    f"{key}.mock_large",
                    f"`{key}.mock_large` shrinks the memories above `{key}.max_bits`, and that "
                    f"is not set: give `{key}.max_bits`",
                )
            )
        if not self.keep_mocked and not self.mock_large:
            found.append(
                (
                    f"{key}.keep_mocked",
                    f"`{key}.keep_mocked` has no effect without `{key}.mock_large`: delete it "
                    f"or set `{key}.mock_large: true`",
                )
            )
        return found


class ClockGatingGroup(SettingsGroup):
    """The inference of clock gates (`clockgate`). It finds flip-flops that share a clock and an
    enable signal, and replaces their enable logic by one clock-gating cell."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("min_width", "-min_net_size", "value"),
        Flag("tie_lo", "-tie_lo", "value"),
    )

    enabled: bool = Field(
        False,
        description="Infer clock gates after the latches are mapped and before the flip-flops "
        "are mapped. A design that wants a gate for an enable instantiates a clock-gating cell "
        "itself, with the map of `sequential.clockgate_map`.",
        json_schema_extra=tier("advanced"),
    )
    pos_icg: Optional[Tuple[str, str, str, str]] = Field(
        None,
        description="The clock-gating cell for flip-flops on the rising edge: the cell, its "
        "enable port, its clock port and its gated clock port (`clockgate -pos`). The enable "
        "must be active high. Unset: yosys picks a cell from the Liberty files when neither this "
        "nor `neg_icg` is set, and gates no rising-edge flip-flop otherwise.",
        json_schema_extra=tier("advanced"),
    )
    neg_icg: Optional[Tuple[str, str, str, str]] = Field(
        None,
        description="The clock-gating cell for flip-flops on the falling edge, in the form of "
        "`pos_icg` (`clockgate -neg`). Unset: see `pos_icg`.",
        json_schema_extra=tier("advanced"),
    )
    min_width: Optional[int] = Field(
        None,
        ge=1,
        description="Gate only the sets of at least this many flip-flops that share an enable "
        "(`clockgate -min_net_size`). Unset: the default of yosys.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    tie_lo: Optional[str] = Field(
        None,
        min_length=1,
        description="Warning: a port that is tied low for good can disable the cell. A port of "
        "the clock-gating cell that the script ties to zero, such as a scan enable "
        "(`clockgate -tie_lo`). Needs `pos_icg` or `neg_icg`: a cell that yosys picks from the "
        "Liberty files lists its own tie-low ports.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse a tie-low port when no cell is named: yosys finds the port in the Liberty file."""
        if self.tie_lo is not None and self.pos_icg is None and self.neg_icg is None:
            return [
                (
                    f"{key}.tie_lo",
                    f"`{key}.tie_lo` names a port of a clock-gating cell that you name, and "
                    f"`{key}.pos_icg` and `{key}.neg_icg` are not set: give a cell, or delete "
                    f"`{key}.tie_lo`",
                )
            ]
        return []


#: A module that a retiming run selects: a name, not a pattern.
ModuleName = Annotated[str, Field(min_length=1, pattern=r"^[^\s\"*?\[\]]+$")]


class SequentialGroup(SettingsGroup):
    """The mapping of latches, clock gates, flip-flops and tri-states."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("min_enable_fanout", "-mince", "value"),
        Flag("min_sync_reset_fanout", "-minsrst", "value"),
    )

    latch_map: Literal["platform", "none"] = Field(
        "platform",
        description="`platform` maps latches with the latch map of the platform (or "
        "`latch_map_file`) when there is one. `none` maps no latches.",
        json_schema_extra=tier("advanced"),
    )
    latch_map_file: Optional[Path] = Field(
        None,
        description="Verilog map of latches, applied with `techmap -map` after `dfflegalize`, "
        "instead of the map of the platform. Not with `latch_map: none`.",
        json_schema_extra=tier("advanced"),
    )
    clockgate_map: Literal["platform", "none"] = Field(
        "platform",
        description="`platform` reads the file with the clock-gating cells of the platform (or "
        "`clockgate_map_file`) when there is one. `none` reads no such file.",
        json_schema_extra=tier("advanced"),
    )
    clockgate_map_file: Optional[Path] = Field(
        None,
        description="Verilog file with the clock-gating cells (`read_verilog -defer`), instead of "
        "the file of the platform. Not with `clockgate_map: none`.",
        json_schema_extra=tier("advanced"),
    )
    dff_liberty: Optional[Path] = Field(
        None,
        description="Liberty file for mapping flip-flops. It replaces the Liberty files on "
        "`dfflibmap`; `abc` still gets the Liberty files. Not with `dff_map`.",
        json_schema_extra=tier("advanced"),
    )
    dff_map: Optional[Path] = Field(
        None,
        description="Verilog map of flip-flops. Every module in the file names a flip-flop type "
        "that the netlist may keep, such as `$_DFF_P_`. The script legalizes the flip-flops to "
        "these types and applies the map with `techmap -map`, instead of running `dfflibmap`. "
        "Not with `dff_liberty`.",
        json_schema_extra=tier("advanced"),
    )
    clock_gating: ClockGatingGroup = Field(
        default_factory=ClockGatingGroup,
        description="The inference of clock gates (`clockgate`).",
    )
    retime_modules: List[ModuleName] = Field(
        [],
        description="Modules to retime: the flip-flops move over the logic to shorten the "
        "longest path (`abc -dff` with a retiming script). Each module must still be a module "
        "when the retiming starts: it is the top module, or the flattening keeps it "
        "(`keep_hierarchy`, a kept instance, a `keep_hierarchy` attribute in the sources or in "
        "`set_attribute`), or the design is not flattened. If a module is gone, the run stops "
        "with an error that names it. Retiming moves flip-flops, so the netlist does not match "
        "the names or the states of the RTL, and the run checks no equivalence.",
        json_schema_extra=tier("advanced"),
    )
    tristate: Literal["off", "logic"] = Field(
        "off",
        description="`logic` converts the tri-state buffers that drive no output port to plain "
        "logic (`tribuf -logic`) after the coarse part of `synth`. `off` runs no such pass.",
        json_schema_extra=tier("advanced"),
    )
    min_enable_fanout: Optional[int] = Field(
        None,
        ge=1,
        description="Warning: this changes the netlist from the flip-flops on. Unmap a clock "
        "enable that fewer flip-flops share than this number into logic (`dfflegalize -mince`). "
        "Unset: keep every enable.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    min_sync_reset_fanout: Optional[int] = Field(
        None,
        ge=1,
        description="Warning: this changes the netlist from the flip-flops on. Unmap a "
        "synchronous set or reset that fewer flip-flops share than this number into logic "
        "(`dfflegalize -minsrst`). Unset: keep every set and reset.",
        json_schema_extra=extras(tier("expert"), count()),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse the settings of the sequential group that cannot work together."""
        found: List[Tuple[str, str]] = []
        for name in ("latch_map", "clockgate_map"):
            if getattr(self, name) == "none" and getattr(self, f"{name}_file") is not None:
                found.append(
                    (
                        f"{key}.{name}_file",
                        f"`{key}.{name}_file` has no effect with `{key}.{name}: none`: delete "
                        "one of them",
                    )
                )
        if self.dff_map is not None and self.dff_liberty is not None:
            found.append(
                (
                    f"{key}.dff_map",
                    f"`{key}.dff_map` and `{key}.dff_liberty` both map the flip-flops, and "
                    "only one way works: delete one of them",
                )
            )
        for repeated in sorted(
            {item for item in self.retime_modules if self.retime_modules.count(item) > 1}
        ):
            found.append(
                (
                    f"{key}.retime_modules",
                    f"`{key}.retime_modules` names `{repeated}` more than once: name each "
                    "module once",
                )
            )
        return found


#: The ABC strategies that the flow composes from the steps of ABC: `area_0` to `area_2` map for
#: area, and `delay_0` to `delay_3` map for delay.
COMPOSED_STRATEGIES = ("area_0", "area_1", "area_2", "delay_0", "delay_1", "delay_2", "delay_3")


class AbcGroup(SettingsGroup):
    """The mapping of the logic with ABC."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("liberty_args", "-liberty_args", "value"),
        Flag("dress", "-dress", "switch"),
    )

    strategy: Literal[
        "area",
        "speed",
        "speed_roundtrip",
        "yosys",
        "area_0",
        "area_1",
        "area_2",
        "delay_0",
        "delay_1",
        "delay_2",
        "delay_3",
    ] = Field(
        "area",
        description="The ABC mapping script. `area` and `speed` are the scripts of "
        "OpenROAD-flow-scripts, `speed_roundtrip` is the former speed script of Xeda (it moves "
        "the network between ABC and yosys in each round), and `yosys` keeps the default script "
        "of yosys. `area_0` to `area_2` and `delay_0` to `delay_3` are the strategies that "
        "LibreLane composes from the steps of ABC; the options below act on them only. "
        "`abc_script` replaces the script of the strategy.",
        json_schema_extra=tier("common"),
    )
    mapper: Literal["abc", "abc_new"] = Field(
        "abc",
        description="The yosys command that runs ABC. `abc_new` is experimental in yosys. It maps "
        "with a script that works on a GIA and ends in a mapped one (`&nf`). Unless `abc_script` "
        "gives one, it maps with the script `abc_speed_gia_only.script` of "
        "OpenROAD-flow-scripts. With `arithmetic.wrap_operators` and no `abc_script`, it gets no "
        "script: each wrapped operator maps with the script it carries, and the rest of the "
        "design with the default script of `abc_new`. It refuses `abc.strategy` other than "
        "`area`: those scripts end in a network.",
        json_schema_extra=tier("advanced"),
    )
    driver_cell: Optional[str] = Field(
        None,
        description="Cell that ABC assumes drives the inputs (`set_driving_cell`). Unset: the "
        "cell of the platform.",
        json_schema_extra=tier("common"),
    )
    load: Optional[Annotated[Capacitance, Bounds(ge="0 fF")]] = Field(
        None,
        description="Load that ABC assumes on each output (`set_load`), a capacitance such as "
        "`3.9 fF`. Unset: the load of the platform.",
        json_schema_extra=tier("common"),
    )
    delay_target: Union[Literal["auto"], Annotated[Time, Bounds(ge="0 ns")], None] = Field(
        None,
        description="Delay target of ABC (`-D`): a time such as `500 ps`, or `auto` for the "
        "period of the main clock. Unset: no `-D`.",
        json_schema_extra=tier("advanced"),
    )

    @field_validator("delay_target", mode="wrap")
    @classmethod
    def _delay_target_error(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Report a bad delay target by the problem with the time. The union of `auto` and a time
        reports both branches, and "Input should be 'auto'" hides the real problem."""
        try:
            return handler(value)
        except ValidationError as error:
            times = [e for e in error.errors() if e["type"] != "literal_error"]
            if not times:
                raise
            message = times[0]["msg"]
            if isinstance(value, str) and "cannot read" in message:
                # The text is no time. A user who wrote `Auto` or `AUTO` meant the other branch.
                if value.strip().casefold() == "auto":
                    message = f"cannot read `{value}` as a time: did you mean `auto`?"
                else:
                    message += "; or write `auto`"
            raise PydanticCustomError("quantity", "{message}", {"message": message}) from None

    clock_derating: float = Field(
        0.0,
        ge=0,
        lt=1,
        description="Warning: a large value makes ABC optimize for a period much shorter than the "
        "clock period, and that can grow the netlist. The part of the clock period that ABC "
        "leaves free, as a fraction: ABC gets the period times one minus this number. Needs "
        "`delay_target: auto`.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    buffering: bool = Field(
        False,
        description="End the composed script with `buffer`, `upsize` and `dnsize`, which buffer "
        "the high-fanout nets and size the cells. Needs a composed strategy. The default is the "
        "one of LibreLane.",
        json_schema_extra=tier("advanced"),
    )
    sizing: bool = Field(
        False,
        description="End the composed script with `upsize` and `dnsize`, which size the cells. "
        "Needs a composed strategy, and not `buffering`, which sizes too. The default is the one "
        "of LibreLane.",
        json_schema_extra=tier("advanced"),
    )
    max_fanout: Optional[int] = Field(
        None,
        ge=1,
        description="The largest fanout that `buffer` leaves (`buffer -N`). Unset: the value of "
        "ABC, 10. Needs `buffering`.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    max_slew: Optional[Time] = Field(
        None,
        description="The slew that `buffer` aims at (`buffer -S`): a time such as `100 ps`, at "
        "least 1 ps and a whole number of picoseconds, since ABC reads an integer. Unset: the "
        "value of ABC, 100 ps. Needs `buffering`.",
        json_schema_extra=tier("advanced"),
    )

    @field_validator("max_slew")
    @classmethod
    def _slew_in_whole_picoseconds(cls, value: Optional[Time]) -> Optional[Time]:
        """Refuse a slew that is under 1 ps or not a whole number of picoseconds: ABC's
        `buffer -S` takes a positive integer number of picoseconds."""
        if value is not None:
            picoseconds = value.to("ps")
            if picoseconds < 1 or not picoseconds.is_integer():
                raise ValueError(
                    f"the slew must be at least 1 ps and a whole number of picoseconds, got "
                    f"{picoseconds:g} ps: ABC's `buffer -S` takes an integer number of "
                    "picoseconds"
                )
        return value

    mfs3: bool = Field(
        False,
        description="Warning: this step can stop ABC. On some real designs, ABC stopped on an "
        "assertion in `Abc_TtExpand`, with area and delay strategies: for example all seven "
        "composed strategies on the aes design, and some delay strategies on gcd and on a small "
        "multiply-accumulate design. This happened with OSS CAD yosys 0.69+286, Homebrew yosys "
        "0.69+post, and yosys 0.68 and 0.66 on Linux. The cause is a bug in `mfs3 -e -m` of ABC "
        "(unsorted supports in `src/opt/sfm/sfmDec.c`). An ABC built without assertions can "
        "write a netlist that is not equivalent to the design instead. Where `mfs3` finished, it "
        "changed the area by at most 0.2%. Add a SAT-based remapping (`mfs3`) after the mapping.",
        json_schema_extra=tier("expert"),
    )
    area_mapper: Literal["amap", "nf"] = Field(
        "amap",
        description="Warning: `nf` is less tested for these strategies. The mapper for area of "
        "the composed strategies `area_0` to `area_2` and `delay_3`: `amap`, or `&nf` on the "
        "AIG of ABC. The other delay strategies do not map for area.",
        json_schema_extra=tier("expert"),
    )
    legacy_refactor: bool = Field(
        False,
        description="Warning: LibreLane calls the legacy commands less stable. Use `refactor` "
        "instead of `drf -l` in a composed strategy.",
        json_schema_extra=tier("expert"),
    )
    legacy_rewrite: bool = Field(
        False,
        description="Warning: LibreLane calls the legacy commands less stable. Use `rewrite` "
        "instead of `drw -l` in a composed strategy.",
        json_schema_extra=tier("expert"),
    )
    keep_ff: bool = Field(
        False,
        description="Keep the names of the flip-flop outputs that the retiming run of ABC moves "
        "(`abc -keepff`). Only the retiming run passes flip-flops through ABC, so this needs "
        "`sequential.retime_modules`.",
        json_schema_extra=tier("expert"),
    )
    liberty_args: Optional[str] = Field(
        None,
        min_length=1,
        description="Extra arguments for the `read_lib` command that reads the Liberty files in "
        'ABC, such as "-G 250" (`abc -liberty_args`). Not with `mapper: abc_new`.',
        json_schema_extra=tier("expert"),
    )

    dress: bool = Field(
        False,
        description="Warning: this option is experimental in ABC, and it has two known failures. "
        "With a Liberty library, `-dress` maps to LUTs instead of library cells "
        "(YosysHQ/yosys#3644, open since 2023). It fails when `abc` retimes "
        '(berkeley-abc/abc#445). Upstream ABC has a recent commit, "Skip incompatible name '
        'recovery after retiming (Issue #445)", so a newer ABC may fix the second failure. The '
        "option runs the `dress` command of ABC after the mapping, to keep the names of the "
        "original design (`abc -dress`). Not with `mapper: abc_new`, which has no such option.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse the options of the composed strategies where no composed strategy reads them,
        and the other settings of the group that cannot work together."""
        found: List[Tuple[str, str]] = []
        default = type(self)()
        composition = (
            "buffering",
            "sizing",
            "max_fanout",
            "max_slew",
            "mfs3",
            "area_mapper",
            "legacy_refactor",
            "legacy_rewrite",
        )
        if self.strategy not in COMPOSED_STRATEGIES:
            for name in composition:
                if getattr(self, name) != getattr(default, name):
                    found.append(
                        (
                            f"{key}.{name}",
                            f"`{key}.{name}` acts on the composed strategies (`area_0` to "
                            f"`area_2`, `delay_0` to `delay_3`), and `{key}.strategy: "
                            f"{self.strategy}` is not one of them: choose a composed strategy "
                            f"or delete `{key}.{name}`",
                        )
                    )
        else:
            for name in ("max_fanout", "max_slew"):
                if getattr(self, name) is not None and not self.buffering:
                    found.append(
                        (
                            f"{key}.{name}",
                            f"`{key}.{name}` is a limit of `buffer`, and `{key}.buffering` is "
                            f"false: set `{key}.buffering: true` or delete `{key}.{name}`",
                        )
                    )
            if self.buffering and self.sizing:
                found.append(
                    (
                        f"{key}.sizing",
                        f"`{key}.sizing` has no effect with `{key}.buffering: true`, which "
                        "sizes the cells too: delete one of them",
                    )
                )
            if self.area_mapper != default.area_mapper and self.strategy in (
                "delay_0",
                "delay_1",
                "delay_2",
            ):
                found.append(
                    (
                        f"{key}.area_mapper",
                        f"`{key}.area_mapper` has no effect with `{key}.strategy: "
                        f"{self.strategy}`, which does not map for area: use `area_0`, "
                        "`area_1`, `area_2` or `delay_3`, or delete it",
                    )
                )
        if self.clock_derating != 0 and self.delay_target != "auto":
            found.append(
                (
                    f"{key}.clock_derating",
                    f"`{key}.clock_derating` shortens the period that `{key}.delay_target: "
                    f"auto` takes, and `{key}.delay_target` is not `auto`: set "
                    f"`{key}.delay_target: auto` or delete `{key}.clock_derating`",
                )
            )
        if self.mapper == "abc_new" and self.liberty_args is not None:
            found.append(
                (
                    f"{key}.liberty_args",
                    f"`{key}.liberty_args` is an option of `abc`, and `{key}.mapper: abc_new` "
                    "runs `abc_new`, which has no such option: delete "
                    f"`{key}.liberty_args`",
                )
            )
        if self.mapper == "abc_new" and self.dress:
            found.append(
                (
                    f"{key}.dress",
                    f"`{key}.dress` is an option of `abc`, and `{key}.mapper: abc_new` runs "
                    f"`abc_new`, which has no such option: delete `{key}.dress` or set it to "
                    "false",
                )
            )
        return found


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
        "the cells of the platform. Not with `insert_tie_cells: false`.",
        json_schema_extra=tier("common"),
    )
    insert_tie_cells: bool = Field(
        True,
        description="Drive the constant nets with tie cells (`hilomap`) when the platform or "
        "`tie_cells` names the cells. False runs no `hilomap`, so a constant stays a constant "
        "net.",
        json_schema_extra=tier("advanced"),
    )
    buffer_cell: Optional[Tuple[str, str, str]] = Field(
        None,
        description="Buffer cell, its input port and its output port, for `insbuf`. Unset: the "
        "minimum buffer of the platform. Not with `insert_buffers: false`.",
        json_schema_extra=tier("common"),
    )
    insert_buffers: bool = Field(
        True,
        description="Put a buffer on each wire that connects two ports directly (`insbuf`) when "
        "the platform or `buffer_cell` names the cell. False runs no `insbuf`; a design that "
        "synthesizes its modules one by one needs that.",
        json_schema_extra=tier("advanced"),
    )
    drop_formal: bool = Field(
        True,
        description="Remove the formal cells and the `$print` cells (`chformal -remove` and "
        "`delete t:$print`), which the place and route tools do not read. False keeps them.",
        json_schema_extra=tier("advanced"),
    )
    names: Literal["rename_wire", "autoname", "none"] = Field(
        "rename_wire",
        description="How the objects with private names get public names. `rename_wire` names "
        "each cell after the wire that it drives (`rename -wire`), before the mapping, so the "
        "name of a register shows in its flip-flop. `autoname` names every private object when "
        "the netlist is complete (`autoname`); the names can be long. `none` renames nothing.",
        json_schema_extra=tier("advanced"),
    )
    normalize_single_bit: bool = Field(
        False,
        description="Warning: this changes the declarations in the netlist. Write a one-bit "
        "vector as a scalar: remove the `single_bit_vector` attribute (`attrmap -remove`), so "
        "`[0:0]` does not appear.",
        json_schema_extra=tier("expert"),
    )
    check_pre: bool = Field(
        False,
        description="Warning: this prints more checks in the log and fails nothing. Run `check` "
        "once before the synthesis, to find problems in the design as it is read.",
        json_schema_extra=tier("expert"),
    )
    allow_tristate: bool = Field(
        False,
        description="Warning: a tri-state buffer in the netlist is not a mapped cell. Let the "
        "check of mapped cells pass the tri-state buffers (`check -allow-tbuf`).",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse a cell that its switch turns off."""
        found: List[Tuple[str, str]] = []
        for cell, switch in (("tie_cells", "insert_tie_cells"), ("buffer_cell", "insert_buffers")):
            if getattr(self, cell) is not None and not getattr(self, switch):
                found.append(
                    (
                        f"{key}.{cell}",
                        f"`{key}.{cell}` names cells, and `{key}.{switch}` is false, so the "
                        f"script uses none: delete `{key}.{cell}` or set `{key}.{switch}: true`",
                    )
                )
        return found
