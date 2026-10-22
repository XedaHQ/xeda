import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable, ClassVar, Dict, List, Literal, Mapping, Optional, Tuple

from ...dataclass import Field
from ...design import SourceType
from ...flow import (
    Flow,
    FlowException,
    FlowSettingsError,
    NoReplacement,
    Out,
    SynthFlow,
    describe_results,
)
from ...flow.asic import AsicPlatformFlow, AsicPlatformSettings
from ...flow.flow import FlowSettingsException
from ...flow.settings_group import SettingsGroup, render_flags
from ...tool import NonZeroExitCode
from ...utils import replacing_copy, replacing_file, tcl_word
from ..ghdl import GhdlSynth
from .abc_strategies import Composition, composed_abc_script
from .asic_groups import (
    COMPOSED_STRATEGIES,
    AbcGroup,
    ArithmeticGroup,
    CoarseGroup,
    FinishGroup,
    HiLoMap,
    HierarchyGroup,
    MemoryGroup,
    SequentialGroup,
    TechmapFile,
)
from .flag_hatches import LIBERTY_HATCHES, flag_problems
from .common import (
    STOP_AFTER_DESCRIPTION,
    YosysBase,
    YosysSynthesisBase,
    append_flag,
    process_parameters,
    refuse_output_after_rtl,
    stop_after_conflicts,
    ys_path,
)

log = logging.getLogger(__name__)

# The tail of the ABC mapping scripts: buffer, time and size the netlist.
_ABC_TAIL = ["buffer -c", "topo", "stime -c", "upsize -c", "dnsize -c"]
# One round of the speed scripts after the first mapping.
_ABC_SPEED_ROUND = ["&st", "&syn2", "&if -g -K 6", "&synch2", "&nf"]


def abc_strategy_script(strategy: str, options: Optional[Composition] = None) -> Optional[str]:
    """Return the abc mapping script of `abc.strategy`, in the form `abc -script` takes.

    The text starts with `+`. It holds the commands separated by `;`, and each command has its
    words separated by `,`. `area` and `speed` are `abc_area.script` and `abc_speed.script` of
    OpenROAD-flow-scripts (`flow/scripts`). `speed_roundtrip` is the speed script that Xeda
    used before: it moves the network back to yosys and into ABC again in each round
    (`&put`, `&get -n`). `area_0` to `area_2` and `delay_0` to `delay_3` are composed from the
    steps of ABC (`abc_strategies.py`) with the `options`. `None` selects the default mapping
    script of yosys.
    """
    if strategy == "yosys":
        return None
    if strategy in COMPOSED_STRATEGIES:
        return composed_abc_script(strategy, options if options is not None else Composition())
    if strategy == "area":
        commands = ["strash", "dch", "map -B 0.9", "topo", "stime -c", "buffer -c"]
        commands += ["upsize -c", "dnsize -c"]
    elif strategy == "speed":
        commands = ["&get -n", "&st", "&dch", "&nf", *_ABC_SPEED_ROUND * 5, "&put", *_ABC_TAIL]
    elif strategy == "speed_roundtrip":
        commands = ["&get -n", "&st", "&dch", "&nf", "&put"]
        commands += ["&get -n", *_ABC_SPEED_ROUND, "&put"] * 5
        commands += _ABC_TAIL
    else:
        raise ValueError(f"unknown abc strategy {strategy!r}")
    return "+" + ";".join(commands).replace(" ", ",")


#: The name, in the run directory, of the script that `abc_new` maps with. The flow gives it to
#: `abc_new` with `-script`, so a change of the default script of yosys cannot change the mapping.
#: The name is relative and holds no text `-script`, which an old yosys took for an option.
#: Operator wrapping gives `abc_new` no script: each wrapped operator carries its own in the
#: attribute `abc9_script`, and a `-script` would replace it.
ABC_NEW_SCRIPT = "abc_speed_gia_only.script"

#: The files of the package (`data/asic`) that operator wrapping reads, by their names in the
#: package and in the run directory.
WRAP_SCRIPT = "synth_wrap_operators.tcl"
BOOTH_MAP = "synth_wrap_operators-booth.v"

#: The text of `ABC_NEW_SCRIPT`: `abc_speed_gia_only.script` of OpenROAD-flow-scripts
#: (`flow/scripts`, BSD-3-Clause, see `data/asic/NOTICE.md`). It only builds and maps the
#: network. `abc_new` maps each wrapped operator with the script that the operator carries.
ABC_SPEED_GIA_ONLY = "&st\n&dch -r\n&nf\n" + "&st\n&syn2\n&if -g -K 6\n&synch2 -r\n&nf\n" * 5

#: The retiming script of the modules in `sequential.retime_modules`: `abc_retime.script` of
#: OpenROAD-flow-scripts (`flow/scripts`, BSD-3-Clause, see `data/asic/NOTICE.md`), in the form
#: `abc -script` takes. It reads no Liberty file: ABC maps with its own gates and moves the
#: flip-flops over them. The later mapping reads the Liberty files.
ABC_RETIME_SCRIPT = "+" + ";".join(
    ["st", "retime -v -o", *["retime -M 5 -v -o"] * 3, "map"]
).replace(" ", ",")

#: The flip-flop and latch types that `dfflegalize` ingests, each with every polarity and reset
#: value (`?`). A `dfflegalize` that allows all of them changes no cell; the limits
#: `-mince` and `-minsrst` then unmap the enables and resets that few cells share.
LEGALIZE_ANY_FLIP_FLOP = (
    "$_DFF_?_",
    "$_DFFE_??_",
    "$_DFF_???_",
    "$_DFFE_????_",
    "$_ALDFF_??_",
    "$_ALDFFE_???_",
    "$_DFFSR_???_",
    "$_DFFSRE_????_",
    "$_SDFF_???_",
    "$_SDFFE_????_",
    "$_SDFFCE_????_",
    "$_SR_??_",
    "$_DLATCH_?_",
    "$_DLATCH_???_",
    "$_DLATCHSR_???_",
)

_MODULE_DECLARATION = re.compile(r"^[ \t]*module[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)


def dff_map_cells(path: Path) -> Tuple[str, ...]:
    """Return the names of the modules in the flip-flop map `path`, in order.

    Each module of a flip-flop map names a flip-flop type that the netlist may keep. The reader
    is the one of `get_module_names` in `flow/scripts/synth.tcl` of OpenROAD-flow-scripts
    (BSD-3-Clause, see `data/asic/NOTICE.md`): a line that starts with `module` and a name.
    """
    if not path.is_file():
        raise FileNotFoundError(f"The flip-flop map {path} does not exist.")
    names = tuple(_MODULE_DECLARATION.findall(path.read_text()))
    if not names:
        raise FlowException(
            f"`sequential.dff_map` ({path}) names no module: the map needs one module for each "
            "flip-flop type that the netlist may keep, such as `module $_DFF_P_`"
        )
    return names


#: The attributes that the wrapped operators carry in the netlist. The place and route tool finds
#: them with these, and swaps their architectures.
WRAPPED_OPERATOR_ATTRIBUTES = (
    "arithmetic_operator",
    "implements_operator",
    "architecture",
    "source_cell",
)

#: The roles of the platform files that `yosys` opens: the Liberty files, and the mapping files.
#: The derivation of the platform model adds the LEF files.
PLATFORM_ROLES = ("liberty", "latch_map", "clkgate_map", "adder_map")


#: The settings that a liberty mapping refuses, each as `(setting, whether a value asks for what
#: the mapping cannot do, the message)`. The liberty mapping runs its own elaboration, synthesis
#: and mapping with `abc`, and it ends with the tail of OpenROAD-flow-scripts.
LIBERTY_REFUSALS: Tuple[Tuple[str, Callable[[object], bool], str], ...] = (
    (
        "abc_dff",
        lambda value: value is True,
        "`abc_dff` has no effect with a liberty mapping: the mapping maps the flip-flops with "
        "`dfflibmap` before `abc` runs. Delete `abc_dff` or set it to false",
    ),
    (
        "post_synth_opt",
        lambda value: value is True,
        "`post_synth_opt: true` has no effect with a liberty mapping: the mapping runs no extra "
        "`opt` after `synth`. Delete the setting or set it to false. `coarse.opt_dff_sat` adds "
        "`-sat` to the `opt` that follows `synth`",
    ),
    (
        "nosynth",
        lambda value: value is True,
        "`nosynth` cannot map to a liberty library: mapping to library cells needs `synth` to "
        "reduce the design to gates first. Delete `nosynth` or set it to false",
    ),
    (
        "noabc",
        lambda value: value is True,
        "`noabc` cannot map to a liberty library: the mapping always maps with `abc`. Delete the "
        "setting or set it to false",
    ),
)


@dataclass(frozen=True)
class AsicValues:
    """The values of the mapping to a liberty library that `Yosys.asic_values` resolves.

    A value the user gave wins. A value left unset (`None`) is the one of the platform, when the
    platform has one. Without a liberty mapping, every value is empty. Paths are absolute.

    * `latch_map`: the map applied after `dfflegalize` for latches.
    * `clockgate_map`: the file with the clock-gating cells.
    * `dff_map`, `dff_map_cells`: the map of flip-flops, and the names of its modules.
    * `legalize_any`: the cell types of a `dfflegalize` that changes no flip-flop.
    * `retime_script`: the script of the retiming run.
    * `adder_map`: the map of full adders that follows `extract_fa`.
    * `techmap_files`: the user's maps for `techmap -map`, each as `(position, path)`, in order.
      `techmaps(position)` gives the paths at one position.
    * `dff_liberty`: the Liberty file for `dfflibmap` instead of the Liberty files.
    * `driver_cell`, `load`: the `set_driving_cell` and `set_load` of the abc constraints. The
      load is in femtofarads, the unit that ABC reads.
    * `tie_cells`, `buffer_cell`: the cells of `hilomap` and `insbuf`.
    * `abc_script`: the abc script, from `abc_script` or else from `abc.strategy`.
    * `abc_new_script`: the `-script` of `abc_new`, when `abc.mapper` is `abc_new`: `abc_script`,
      or else the name of the script file that the flow writes (`ABC_NEW_SCRIPT`), but none when
      operators are wrapped.
    * `abc_new_default_script`: with wrapped operators and no `abc_script`, the script file
      (`ABC_NEW_SCRIPT`) that maps the rest of the design, set as the default of `abc_new`
      (`abc9.script`) as OpenROAD-flow-scripts does.
    * `wrap_script`, `booth_map`: the names of the files of the package that wrap the operators,
      when `arithmetic.wrap_operators` is on. `run` copies each into the run directory, and the
      script names the copy, since a container does not mount the package.
    * `wrapped_adders`, `wrapped_multipliers`: the architectures to wrap, in the order and the
      spelling that the wrapping script takes (`KOGGE_STONE`, `BOOTH`).
    * `memory_libmap`, `memory_techmap`: the memory library files and the map of their cells.
    * `delay_target_ps`: the `-D` of abc, in picoseconds.
    * `constraints`: the lines of the abc constraints from the driver cell and the load.
    """

    latch_map: Optional[Path] = None
    clockgate_map: Optional[Path] = None
    adder_map: Optional[Path] = None
    techmap_files: Tuple[Tuple[str, Path], ...] = ()
    dff_liberty: Optional[Path] = None
    dff_map: Optional[Path] = None
    dff_map_cells: Tuple[str, ...] = ()
    legalize_any: Tuple[str, ...] = ()
    retime_script: Optional[str] = None
    driver_cell: Optional[str] = None
    load: Optional[float] = None
    tie_cells: Optional[HiLoMap] = None
    buffer_cell: Optional[Tuple[str, str, str]] = None
    abc_script: Optional[str] = None
    abc_new_script: Optional[str] = None
    abc_new_default_script: Optional[str] = None
    wrap_script: Optional[str] = None
    booth_map: Optional[str] = None
    wrapped_adders: Tuple[str, ...] = ()
    wrapped_multipliers: Tuple[str, ...] = ()
    memory_libmap: Tuple[Path, ...] = ()
    memory_techmap: Optional[Path] = None
    delay_target_ps: Optional[float] = None
    constraints: Tuple[str, ...] = ()

    def techmaps(self, position: str) -> Tuple[Path, ...]:
        """Return the user's maps at `position`, in the order of the setting."""
        return tuple(path for where, path in self.techmap_files if where == position)


def _asic_data(name: str) -> Path:
    """Return the path of the file `name` in the `data/asic` directory of the package."""
    return Path(str(resources.files("xeda.flows.yosys").joinpath("data", "asic", name)))


def _changed_group_leaves(settings: "Yosys.Settings") -> List[str]:
    """Return the dotted names of the group leaves of `settings` that differ from their defaults,
    and `techmap_files` when it is not empty. These settings act on a mapping to a liberty
    library only."""

    def changed(group: SettingsGroup, key: str) -> List[str]:
        default = type(group)()
        found: List[str] = []
        for name in type(group).model_fields:
            value = getattr(group, name)
            if isinstance(value, SettingsGroup):
                found += changed(value, f"{key}.{name}")
            elif value != getattr(default, name):
                found.append(f"{key}.{name}")
        return found

    found: List[str] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SettingsGroup):
            found += changed(value, name)
    if settings.techmap_files:
        found.append("techmap_files")
    return found


def _no_liberty_message(settings: "Yosys.Settings", key: str) -> str:
    """Return what to tell the user about the group setting `key` when no liberty mapping runs.

    With `gates` or `lut`, the reason is that the run maps to generic cells or LUTs instead, even
    when a platform is given. Otherwise nothing names a liberty library."""
    for name, instead in (("gates", "generic cells"), ("lut", "LUTs")):
        if getattr(settings, name):
            return (
                f"`{key}` acts on mapping to a liberty library, and `{name}` maps to {instead} "
                f"instead: delete `{key}` or `{name}`"
            )
    return (
        f"`{key}` acts on mapping to a liberty library: give `platform`, `platform_file` or "
        "`liberty` too"
    )


class Yosys(AsicPlatformFlow, YosysSynthesisBase, SynthFlow):
    """Synthesize a design with Yosys to a standard-cell, gate or LUT netlist."""

    results_description = describe_results(
        "area",
        **{
            "cells": "Total number of cells in the synthesized netlist.",
            "platform": "The platform that the netlist is mapped to, when the run maps to one: "
            "`name`, the PDK `provider` and `version`, the `corner`, and `sources`, the layer "
            "(derived, upstream, residue or user) that gave each value of the platform model.",
        },
    )

    class Settings(AsicPlatformSettings, YosysSynthesisBase.Settings, SynthFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **YosysSynthesisBase.Settings.removed_settings,
            "retime": "`abc_dff`, which passes `-dff` to `abc` (not with a liberty mapping), and "
            "`abc_flags` for more options of `abc`; yosys's `synth` has no retime option",
            "netlist_verilog_extmem": "`netlist_verilog_flags: [-extmem]`, which `write_verilog` gets",
            "merge_libs_to": NoReplacement(
                "Xeda does not merge Liberty files; `dont_use_cells` reach `dfflibmap` and `abc` "
                "as `-dont_use`"
            ),
            "optimize": "`abc.strategy` (`speed_roundtrip` for `speed`, and `yosys` for `null`)",
            "abc_driver_cell": "`abc.driver_cell`",
            "abc_load_in_ff": "`abc.load`",
            "adder_map": "`arithmetic.adder_map_file` for a file, or `arithmetic.adder_map: none`",
            "clockgate_map": "`sequential.clockgate_map_file` for a file, or "
            "`sequential.clockgate_map: none`",
            "dff_liberty": "`sequential.dff_liberty`",
            "dff_library": "`sequential.dff_liberty`",
            "other_maps": "`sequential.latch_map_file` for the latch map, "
            "`sequential.latch_map: none` for no latch map, and `techmap_files` for other maps",
            "hilomap": "`finish.tie_cells`",
            "insbuf": "`finish.buffer_cell`",
        }
        nosynth: bool = Field(False, description="Do not run `synth`.")
        netlist_attrs: Optional[bool] = Field(
            None,
            description="Include cell and wire attributes in the written Verilog netlist. Unset: "
            "included, except when mapping to a liberty library (that netlist is written for "
            "place and route). A netlist with wrapped operators (`arithmetic.wrap_operators`) "
            "keeps its attributes, and `false` is refused.",
        )
        post_synth_opt: Optional[bool] = Field(
            None,
            description="Run additional optimization after synthesis (gates and LUTs only). "
            "Unset: off. A liberty mapping runs no such step and refuses `true`. Use "
            "`coarse.opt_dff_sat` there.",
        )
        # The base classes declare a plain `bool`, which the FPGA flows keep. `Yosys` adds "unset".
        abc_dff: Optional[bool] = Field(  # type: ignore[assignment]
            None,
            description="Run abc with `-dff`. Unset: off when mapping to a liberty library, on "
            "otherwise. A liberty mapping refuses `true`: it maps the flip-flops with "
            "`dfflibmap` before `abc` runs, so `-dff` has no effect there.",
        )
        splitnets: Optional[bool] = Field(  # type: ignore[assignment]
            None,
            description="Run `splitnets`, splitting multi-bit nets into individual bits. Unset: "
            "on when mapping to a liberty library, off otherwise.",
        )
        netlist_hex: Optional[bool] = Field(
            None,
            description="Write constants in the netlist in hexadecimal. Unset: hexadecimal, "
            "except when mapping to a liberty library.",
        )
        gates: Optional[List[str]] = Field(
            None,
            description="Map to this generic gate set instead of a liberty library, e.g. "
            '["AND", "OR", "NOT"]. Accepts a comma-separated string. Refused together with '
            "`platform`, `platform_file` or `liberty`.",
        )
        lut: Optional[str] = Field(
            None,
            description='Map to LUTs of this size instead of standard cells, e.g. "4" or a '
            '"<width>:<cost>" pair. Refused together with `platform`, `platform_file` or '
            "`liberty`.",
        )
        stop_after: Optional[Literal["rtl"]] = Field(None, description=STOP_AFTER_DESCRIPTION)
        hierarchy: HierarchyGroup = Field(
            default_factory=HierarchyGroup,
            description="The hierarchy of the design during a mapping to a liberty library.",
        )
        coarse: CoarseGroup = Field(
            default_factory=CoarseGroup,
            description="The coarse-grain passes of a mapping to a liberty library.",
        )
        arithmetic: ArithmeticGroup = Field(
            default_factory=ArithmeticGroup,
            description="The mapping of adders and other operators to a liberty library.",
        )
        memory: MemoryGroup = Field(
            default_factory=MemoryGroup,
            description="The mapping of memories to a liberty library.",
        )
        sequential: SequentialGroup = Field(
            default_factory=SequentialGroup,
            description="The mapping of latches, clock gates and flip-flops to a liberty library.",
        )
        abc: AbcGroup = Field(
            default_factory=AbcGroup,
            description="The mapping of the logic with ABC to a liberty library.",
        )
        finish: FinishGroup = Field(
            default_factory=FinishGroup,
            description="The last commands of a mapping to a liberty library, before the netlist "
            "is written.",
        )
        techmap_files: List[TechmapFile] = Field(
            [],
            description="Verilog techmap files of a mapping to a liberty library. Each one is "
            "applied with `techmap -map` at its `when` position: `pre_synth`, `post_coarse`, "
            "`pre_dff` or `post_dff`. A bare path is a map at `pre_dff`, after the latch map and "
            "before clock gating and the flip-flop mapping. Maps at one position run in the "
            "order of the list.",
        )

        def uses_platform(self) -> bool:
            """Return whether abc maps to the liberty library of a platform.

            That is the case with `platform` or `platform_file`. The platform then supplies the
            mapping inputs that no setting gives."""
            return self.platform is not None or self.platform_file is not None

        def maps_to_liberty(self) -> bool:
            """Return whether abc maps to a liberty library, from `liberty` or the platform.

            It does not when `gates` or `lut` is set. The platform-derived settings act only
            when it does.
            """
            return not self.gates and not self.lut and (bool(self.liberty) or self.uses_platform())

    class Outputs(SynthFlow.Outputs):
        netlist: Path | None = Out(
            SourceType.VerilogNetlist,
            enabled_by="netlist_verilog",
            description="The synthesized Verilog netlist, written at `netlist_verilog`, when the "
            "flow maps to a Liberty library (`liberty`, `platform` or `platform_file`, and no "
            "`gates` or `lut`). A place and route tool reads it.",
        )
        netlist_generic: Path | None = Out(
            SourceType.GenericVerilogNetlist,
            enabled_by="netlist_verilog",
            description="The synthesized Verilog netlist, written at `netlist_verilog`, when the "
            "flow does not map to a Liberty library: its cells are those of yosys (gates, LUTs "
            "or coarse cells). No Liberty library holds them.",
        )

    @classmethod
    def output_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        """Narrow the two netlists to the one that the settings write: the mapped netlist when
        `maps_to_liberty()`, else the generic one. The other has no type."""
        types = super().output_types(settings, name)
        if name in ("netlist", "netlist_generic"):
            assert isinstance(settings, cls.Settings)
            return types if cls.netlist_output(settings) == name else ()
        return types

    @staticmethod
    def netlist_output(settings: "Yosys.Settings") -> str:
        """Return the name of the output that the netlist is: the one decision of the flow."""
        return "netlist" if settings.maps_to_liberty() else "netlist_generic"

    @classmethod
    def enable_output(cls, settings: Flow.Settings, name: str, *, design_name: str) -> None:
        """Switch on the output that a consumer asks for, unless `stop_after` stops the flow."""
        refuse_output_after_rtl(cls, settings, name)
        super().enable_output(settings, name, design_name=design_name)

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse settings the run cannot honor, before anything runs.

        These are a result that a run stopped by `stop_after` does not write
        (`stop_after_conflicts`), a platform, `liberty` or `dont_use_cells` together with `gates`
        or `lut`, the settings of the groups when no liberty mapping runs, the settings that the
        liberty mapping cannot honor (`LIBERTY_REFUSALS`), and the rules that join several
        groups. All problems come back in one error.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        problems = stop_after_conflicts(settings)
        if settings.uses_platform():
            selector = "platform" if settings.platform is not None else "platform_file"
            for name in ("gates", "lut"):
                if getattr(settings, name):
                    problems.append(
                        (
                            name,
                            f"`{name}` and `{selector}` cannot both be set: `{selector}` maps to "
                            f"the standard cells of the platform and `{name}` maps to generic "
                            "cells. Set only one of them",
                        )
                    )
        if settings.liberty:
            for name in ("gates", "lut"):
                if getattr(settings, name):
                    problems.append(
                        (
                            name,
                            f"`{name}` and `liberty` cannot both be set: `liberty` maps to the "
                            f"cells of the Liberty files and `{name}` maps to generic cells. "
                            "Set only one of them",
                        )
                    )
        if settings.dont_use_cells:
            for name in ("gates", "lut"):
                if getattr(settings, name):
                    problems.append(
                        (
                            "dont_use_cells",
                            f"`dont_use_cells` and `{name}` cannot both be set: `{name}` maps to "
                            "generic cells, which have no library cells to exclude. Set only one "
                            "of them",
                        )
                    )
        if settings.maps_to_liberty():
            for name, asks, message in LIBERTY_REFUSALS:
                if asks(getattr(settings, name)):
                    problems.append((name, message))
            problems += cls._liberty_mapping_problems(settings)
        else:
            problems += [
                (key, _no_liberty_message(settings, key)) for key in _changed_group_leaves(settings)
            ]
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in problems], cls.Settings
            )

    @classmethod
    def tcl_reasons(cls, settings: YosysBase.Settings) -> List[str]:
        """Return the names of the settings that need a Tcl script: the script runs a Tcl
        procedure for each of them. The planning refuses each of them without a liberty
        mapping."""
        assert isinstance(settings, cls.Settings)
        asked = (
            ("hierarchy.keep_min_cost", settings.hierarchy.keep_min_cost is not None),
            ("arithmetic.wrap_operators", settings.arithmetic.wrap_operators),
            ("memory.max_bits", settings.memory.max_bits is not None),
            ("memory.mock_large", settings.memory.mock_large),
        )
        return [name for name, given in asked if given]

    @classmethod
    def _liberty_mapping_problems(cls, settings: "Yosys.Settings") -> List[Tuple[str, str]]:
        """Return what is wrong with the group settings of a liberty mapping, beyond the rules
        of each group: the values that only a Tcl script renders, the script that two settings
        choose, and a delay target that needs a clock."""
        problems: List[Tuple[str, str]] = []
        problems += cls._tcl_mode_problems(settings)
        problems += cls._hierarchy_problems(settings)
        problems += flag_problems(settings, LIBERTY_HATCHES)
        if settings.prep is not None and settings.coarse.latches is not None:
            problems.append(
                (
                    "coarse.latches",
                    "`coarse.latches` is an option of the `proc` that the script runs, and `prep` "
                    "runs its own `proc` without it: delete one of them",
                )
            )
        if settings.abc.keep_ff and not settings.sequential.retime_modules:
            problems.append(
                (
                    "abc.keep_ff",
                    "`abc.keep_ff` keeps the flip-flops that the retiming run of ABC moves, and "
                    "`sequential.retime_modules` names no module: name a module, or delete "
                    "`abc.keep_ff`",
                )
            )
        if settings.abc_script is not None and settings.abc.strategy != "area":
            problems.append(
                (
                    "abc_script",
                    "`abc_script` replaces the script of `abc.strategy` "
                    f"(`{settings.abc.strategy}`): give only one of them",
                )
            )
        clock = settings.main_clock
        if settings.abc.delay_target == "auto" and clock is None:
            problems.append(
                (
                    "abc.delay_target",
                    "`abc.delay_target: auto` takes the period of the main clock: give `clock`",
                )
            )
        return problems

    @classmethod
    def _hierarchy_problems(cls, settings: "Yosys.Settings") -> List[Tuple[str, str]]:
        """Return the hierarchy settings that `flatten: false` leaves with nothing to do. The
        design is not flattened then, so no instance needs a mark and no separator is used."""
        if settings.flatten is not False:
            return []
        asked = (
            ("hierarchy.keep_instances", bool(settings.hierarchy.keep_instances)),
            ("hierarchy.separator", settings.hierarchy.separator is not None),
        )
        return [
            (
                key,
                f"`{key}` has no effect with `flatten: false`, which flattens nothing: delete "
                "one of them",
            )
            for key, given in asked
            if given
        ]

    @classmethod
    def _tcl_mode_problems(cls, settings: "Yosys.Settings") -> List[Tuple[str, str]]:
        """Return what is wrong with hierarchical synthesis, operator wrapping and `abc_new`
        together with the other settings."""
        problems: List[Tuple[str, str]] = []
        abc_new = settings.abc.mapper == "abc_new"
        if settings.hierarchy.keep_min_cost is not None and settings.flatten is False:
            problems.append(
                (
                    "hierarchy.keep_min_cost",
                    "`hierarchy.keep_min_cost` flattens every module that costs less, so it "
                    "cannot run with `flatten: false`: delete one of them",
                )
            )
        if settings.arithmetic.wrap_operators:
            stripped = [
                name
                for name in WRAPPED_OPERATOR_ATTRIBUTES
                if any(given.lstrip("\\") == name for given in settings.netlist_unset_attributes)
            ]
            for name in stripped:
                problems.append(
                    (
                        "netlist_unset_attributes",
                        f"`netlist_unset_attributes` names `{name}`, which the place and route "
                        "tool needs to find and swap the wrapped operators "
                        f"(`arithmetic.wrap_operators`): delete `{name}` from the list",
                    )
                )
            if settings.netlist_attrs is False:
                problems.append(
                    (
                        "netlist_attrs",
                        "`netlist_attrs: false` removes the attributes `arithmetic_operator`, "
                        "`implements_operator`, `architecture` and `source_cell`, which the "
                        "place and route tool needs to swap the wrapped operators "
                        "(`arithmetic.wrap_operators`): delete `netlist_attrs` or set it to "
                        "true",
                    )
                )
        if abc_new and settings.abc.strategy != "area":
            problems.append(
                (
                    "abc.strategy",
                    f"`abc.strategy` (`{settings.abc.strategy}`) cannot run with "
                    "`abc.mapper: abc_new`: the script of a strategy ends in a network, and "
                    "`abc_new` needs a script that ends in a mapped GIA (`&nf`). Delete "
                    "`abc.strategy`, or give such a script in `abc_script`",
                )
            )
        return problems

    def add_template_helpers(self) -> None:
        """Add the helpers of every yosys template, and `render_flags` for the groups of settings
        whose flags a template prints. It quotes a value for the script language."""
        super().add_template_helpers()
        assert isinstance(self.settings, self.Settings)
        quote = tcl_word if type(self).script_format(self.settings) == "tcl" else ys_path
        self.jinja_env.globals["render_flags"] = lambda group: render_flags(group, quote)

    def init(self):
        """Resolve the Liberty files that the user gave against the design root.

        `abc_dff` is resolved first, since the shared `init()` adds `-dff` to the flags of `abc`
        when it is on."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.abc_dff is None:
            ss.abc_dff = not ss.maps_to_liberty()
        super().init()
        ss.liberty = [self.normalize_path_to_design_root(lib) for lib in ss.liberty]

    def prepare_inputs(self) -> None:
        """Build the platform model, when abc maps to the liberty library of a platform."""
        super().prepare_inputs()
        assert isinstance(self.settings, self.Settings)
        model = self.prepare_platform(PLATFORM_ROLES) if self.settings.uses_platform() else None
        self._refuse_liberty_includes(None if model is None else model.files)

    def _liberty_files_read(self, platform_files: Optional[Mapping[str, List[Path]]]) -> List[Path]:
        """Return the Liberty files that the mapping reads: those of `liberty`, else those of the
        platform (`platform_files`, by role), and `sequential.dff_liberty`."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        files = ss.liberty_files() or list((platform_files or {}).get("liberty", []))
        dff = ss.sequential.dff_liberty
        if dff is not None and ss.maps_to_liberty():
            files.append(self.normalize_path_to_design_root(dff))
        return files

    def _refuse_liberty_includes(self, platform_files: Optional[Mapping[str, List[Path]]]) -> None:
        """Refuse a Liberty file that includes another with `include_file(...)`.

        The Liberty reader of yosys does not read `include_file`, and it stops with a syntax
        error at the statement. So the flow refuses it before it starts yosys, in the launch and
        in the dry run (`locate_prepared_inputs`)."""
        assert isinstance(self.settings, self.Settings)
        for liberty in self._liberty_files_read(platform_files):
            included = self.settings.included_by(liberty)
            if included:
                raise FlowSettingsException(
                    f"the Liberty file {liberty} includes {included[0]} with `include_file`, which "
                    "the Liberty reader of yosys does not read. Put the text of the included "
                    "files in the Liberty file"
                )

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the ABC script file, then the files of the platform that `prepare_inputs` reads.

        It refuses what `prepare_inputs` refuses (`_refuse_liberty_includes`)."""
        assert isinstance(self.settings, self.Settings)
        located = super().locate_prepared_inputs()
        if located is None:
            return None
        platform_files = None
        if self.settings.uses_platform():
            located = [*located, *self.locate_platform_inputs(PLATFORM_ROLES)]
            platform_files = self.settings.locate_platform().files
        self._refuse_liberty_includes(platform_files)
        return located

    def _derive_from_platform(self) -> None:
        """Complete the netlist settings that the user left unset.

        For a liberty mapping: flattening, splitting the nets, and a gate-level netlist (without
        attributes and hexadecimal constants; wrapped operators keep their attributes). Without
        a liberty mapping, the netlist keeps attributes and hexadecimal constants.
        Post-synthesis optimization is off unless the user turns it on for another mapping. A setting that the user gave is never replaced. Only an
        unset (`None`) setting is derived. The settings of the groups are never derived: see
        `asic_values`.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        mapping = ss.maps_to_liberty()
        if ss.netlist_attrs is None:
            # the place and route tool reads the attributes of the wrapped operators
            ss.netlist_attrs = not mapping or ss.arithmetic.wrap_operators
        if ss.netlist_hex is None:
            ss.netlist_hex = not mapping
        if ss.post_synth_opt is None:
            ss.post_synth_opt = False
        if ss.splitnets is None:
            ss.splitnets = mapping
        if mapping and ss.flatten is None:
            ss.flatten = True

    def _abc_new_script(self) -> Optional[str]:
        """Return the `-script` of `abc_new`, or None when it gets none.

        The script of the user replaces everything, the scripts of wrapped operators too. Without
        it, `abc_new` maps with the file that the flow writes, unless operators are wrapped:
        each wrapped operator carries its own script, which a `-script` would replace."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.abc.mapper != "abc_new":
            return None
        if ss.abc_script is not None:
            return ss.abc_script
        return None if ss.arithmetic.wrap_operators else ABC_NEW_SCRIPT

    def _abc_new_default_script(self) -> Optional[str]:
        """Return the script file that maps the modules other than the wrapped operators, or None.

        `abc_new` maps each wrapped operator with the script it carries and every other module
        with its default script. The flow sets that default to the gia-only script, as
        OpenROAD-flow-scripts does: the default of yosys maps to LUTs when a path in the
        arguments contains `-script` (YosysHQ/yosys#6320), and it can change with yosys."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        wrapped = ss.abc.mapper == "abc_new" and ss.arithmetic.wrap_operators
        return ABC_NEW_SCRIPT if wrapped and ss.abc_script is None else None

    def asic_values(self) -> AsicValues:
        """Return the values of a mapping to a liberty library, with the platform's defaults.

        A value that the user set wins, and a leaf left unset (`None`) takes the value of the
        platform model, when the model has one. A relative path resolves against the design
        root. The method changes no setting. Without a liberty mapping, it returns empty values.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if not ss.maps_to_liberty():
            return AsicValues()
        model = self.platform_model
        files = model.files if model else {}

        def given(path: Optional[Path]) -> Optional[Path]:
            return None if path is None else self.normalize_path_to_design_root(path)

        def platform_file(role: str) -> Optional[Path]:
            return files[role][0] if files.get(role) else None

        latch_map = None
        if ss.sequential.latch_map == "platform":
            latch_map = given(ss.sequential.latch_map_file)
            if latch_map is None:
                latch_map = platform_file("latch_map")
        clockgate_map = None
        if ss.sequential.clockgate_map == "platform":
            clockgate_map = given(ss.sequential.clockgate_map_file)
            if clockgate_map is None:
                clockgate_map = platform_file("clkgate_map")
        dff_map = given(ss.sequential.dff_map)
        adder_map = None
        if ss.arithmetic.adder_map == "platform":
            adder_map = given(ss.arithmetic.adder_map_file)
            if adder_map is None:
                adder_map = platform_file("adder_map")
        tie_cells = ss.finish.tie_cells
        if not ss.finish.insert_tie_cells:
            tie_cells = None
        elif tie_cells is None and model and model.tie_high and model.tie_low:
            tie_cells = HiLoMap(
                hi=(model.tie_high.cell, model.tie_high.port),
                lo=(model.tie_low.cell, model.tie_low.port),
            )
        buffer_cell = ss.finish.buffer_cell
        if not ss.finish.insert_buffers:
            buffer_cell = None
        elif buffer_cell is None and model and model.min_buffer:
            buffer = model.min_buffer
            buffer_cell = (buffer.cell, buffer.input, buffer.output)
        driver_cell = ss.abc.driver_cell
        if driver_cell is None and model:
            driver_cell = model.abc_driver_cell
        # ABC reads every library in picoseconds and femtofarads, so `set_load` is in femtofarads.
        load = None if ss.abc.load is None else ss.abc.load.to("fF")
        if load is None and model and model.abc_load_in_ff is not None:
            load = float(model.abc_load_in_ff)
        target = ss.abc.delay_target
        delay_target_ps = None
        if target == "auto":
            clock = ss.main_clock
            if clock is not None:
                delay_target_ps = clock.period.to("ps") * (1 - ss.abc.clock_derating)
        elif target is not None:
            delay_target_ps = target.to("ps")
        constraints = []
        if driver_cell:
            constraints.append(f"set_driving_cell {driver_cell}")
        if load is not None:
            constraints.append(f"set_load {load}")
        return AsicValues(
            latch_map=latch_map,
            clockgate_map=clockgate_map,
            adder_map=adder_map,
            techmap_files=tuple(
                (item.when, self.normalize_path_to_design_root(item.path))
                for item in ss.techmap_files
            ),
            dff_liberty=given(ss.sequential.dff_liberty),
            dff_map=dff_map,
            dff_map_cells=dff_map_cells(dff_map) if dff_map is not None else (),
            legalize_any=LEGALIZE_ANY_FLIP_FLOP,
            retime_script=ABC_RETIME_SCRIPT if ss.sequential.retime_modules else None,
            driver_cell=driver_cell,
            load=load,
            tie_cells=tie_cells,
            buffer_cell=buffer_cell,
            abc_script=(
                ss.abc_script
                if ss.abc_script is not None
                else abc_strategy_script(
                    ss.abc.strategy,
                    Composition(
                        buffering=ss.abc.buffering,
                        sizing=ss.abc.sizing,
                        max_fanout=ss.abc.max_fanout,
                        max_slew=(
                            None if ss.abc.max_slew is None else round(ss.abc.max_slew.to("ps"))
                        ),
                        mfs3=ss.abc.mfs3,
                        area_mapper=ss.abc.area_mapper,
                        legacy_refactor=ss.abc.legacy_refactor,
                        legacy_rewrite=ss.abc.legacy_rewrite,
                    ),
                )
            ),
            abc_new_script=self._abc_new_script(),
            abc_new_default_script=self._abc_new_default_script(),
            wrap_script=WRAP_SCRIPT if ss.arithmetic.wrap_operators else None,
            booth_map=BOOTH_MAP if ss.arithmetic.wrap_operators else None,
            wrapped_adders=tuple(name.upper() for name in ss.arithmetic.wrapped_adders),
            wrapped_multipliers=tuple(name.upper() for name in ss.arithmetic.wrapped_multipliers),
            memory_libmap=tuple(self.normalize_path_to_design_root(f) for f in ss.memory.libmap),
            memory_techmap=given(ss.memory.techmap),
            delay_target_ps=delay_target_ps,
            constraints=tuple(constraints),
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        # TODO factor out common code
        ss = self.settings
        self.prepare_output_parents()
        declared = self.outputs
        assert isinstance(declared, self.Outputs)
        if ss.netlist_verilog:
            setattr(declared, self.netlist_output(ss), self.run_path / ss.netlist_verilog)

        # The Liberty files of the platform, when the user gave none: the script reads each as it is.
        model = self.platform_model
        assert model is not None or not ss.uses_platform(), "prepare_inputs() builds the model"
        if model is not None and not ss.liberty:
            ss.liberty = list(model.files.get("liberty", []))
        self._derive_from_platform()
        # List a timing report only when `sta` writes one, as `yosys_fpga` does. A remote run is
        # asked for every listed artifact and cannot send one that was never written.
        timing_report = ss.reports_dir / "timing.rpt"
        utilization_report = ss.reports_dir / "utilization.json"
        self.artifacts.timing_report = timing_report if ss.sta else None
        # `stop_after: rtl` skips the stage that writes this report, so a stopped run lists none
        self.artifacts.utilization_report = utilization_report if ss.stop_after != "rtl" else None
        # an earlier run's reports must not stand for this run's
        self.run_directory.remove(utilization_report, timing_report)
        if ss.gates:
            append_flag(ss.abc_flags, f"-g {','.join(ss.gates)}")
        elif ss.lut:
            append_flag(ss.abc_flags, f"-lut {ss.lut}")
        elif ss.liberty and ss.netlist_expr is None:
            ss.netlist_expr = False
        if ss.flatten and not ss.maps_to_liberty():  # the liberty mapping's script renders it
            append_flag(ss.synth_flags, "-flatten")

        for lib in ss.liberty:
            if not lib.exists():
                raise FileNotFoundError(f"Liberty file {lib} does not exist.")

        asic = self.asic_values()
        abc_constr_file = None
        constraints = [*asic.constraints, *ss.abc_constr]
        if constraints:
            abc_constr_file = "abc.constr"
            with replacing_file(self.run_directory.writable(abc_constr_file)) as f:
                f.write("\n".join(constraints) + "\n")

        for name in (asic.wrap_script, asic.booth_map):
            if name:
                replacing_copy(_asic_data(name), self.run_directory.writable(name))

        if ABC_NEW_SCRIPT in (asic.abc_new_script, asic.abc_new_default_script):
            with replacing_file(self.run_directory.writable(ABC_NEW_SCRIPT)) as f:
                f.write(ABC_SPEED_GIA_ONLY)

        script_path = self.copy_from_template(
            f"yosys_synth{self.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=False),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            asic=asic,
            dont_use=self.dont_use_cells(),
        )
        log.info("Yosys script: %s", self.run_file(script_path))
        args = [self.script_flag, script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args += ["-L", ss.log_file]
        depfile = self.run_path / "yosys.d"
        args += ["-E", depfile]
        self.depfiles.append(depfile)
        try:
            self.run_yosys(*args)
        except NonZeroExitCode:
            reasons = type(self).tcl_reasons(ss)
            if reasons:
                log.error(
                    "Xeda runs a Tcl script, since %s %s one. A yosys built without Tcl cannot "
                    "run it, so if the log says so, use a yosys with Tcl, or delete %s.",
                    " and ".join(f"`{name}`" for name in reasons),
                    "needs" if len(reasons) == 1 else "need",
                    "the setting" if len(reasons) == 1 else "the settings",
                )
            raise

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
        if not self.artifacts.utilization_report:
            return True
        utilization = self.get_utilization()
        if not utilization:
            return False
        mod_util = utilization.get("modules")
        if mod_util:
            self.results["_hierarchical_utilization"] = mod_util
        design_util = utilization.get("design")
        if design_util:
            num_cells_by_type = design_util.get("num_cells_by_type", {})
            self.results.update(**num_cells_by_type)
            num_cells = design_util.get("num_cells")
            if num_cells is not None:
                self.results["cells"] = num_cells
            area = design_util.get("area")
            if area:
                self.results["area"] = area
            self.results["_utilization"] = design_util
        return True
