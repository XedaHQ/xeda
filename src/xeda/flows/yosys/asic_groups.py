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
from typing import Annotated, ClassVar, List, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup

__all__ = [
    "AbcGroup",
    "ArithmeticGroup",
    "CoarseGroup",
    "ExtractFaGroup",
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
        "with `flatten: false`.",
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
    when an adder map is in use (`arithmetic.adder_map`)."""

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
        json_schema_extra=tier("advanced"),
    )
    max_breadth: Optional[int] = Field(
        None,
        ge=1,
        description="The widest logic cone to search (`-b`). Unset: 6, the value of yosys.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> List[Tuple[str, str]]:
        """Refuse a run that extracts no kind of adder."""
        if not (self.full or self.half):
            return [
                (
                    f"{key}.full",
                    f"`{key}.full` and `{key}.half` are both false, so `extract_fa` has "
                    f"nothing to extract: set one of them to true, or set "
                    "`arithmetic.adder_map: none`",
                )
            ]
        return []


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
        description="Merge the arithmetic cells into `$alu` and `$macc` cells (`alumacc`). "
        "False adds `-noalumacc` to `synth`. Warning: without these cells, the adder "
        "architecture has nothing to map.",
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
        description="The options of `extract_fa`. It runs when an adder map is in use.",
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
        json_schema_extra=tier("advanced"),
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
        description="Check that a read port and a write port that meet on one address get a "
        "defined value. False marks every read port as returning any value then "
        "(`synth -no-rw-check`). Warning: the netlist can differ from the simulation.",
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
