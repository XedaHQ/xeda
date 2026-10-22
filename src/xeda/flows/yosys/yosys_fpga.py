import logging
import posixpath
import re
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Dict, List, Literal, NamedTuple, Optional, Tuple

from ...dataclass import Field, deliverable, field_validator, model_validator
from ...design import SourceType
from ...flow import (
    Flow,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
    FpgaSynthFlow,
    MetricSpec,
    Out,
    describe_results,
)
from ...flows.ghdl import GhdlSynth
from ...units import count
from ...utils import ToolException, replacing_file
from .common import (
    MINIMUM_YOSYS,
    STOP_AFTER_DESCRIPTION,
    YosysRelease,
    YosysSynthesisBase,
    process_parameters,
    refuse_output_after_rtl,
    same_file,
    stop_after_conflicts,
    yosys_data_dir,
    yosys_release,
)

log = logging.getLogger(__name__)

GOWIN_FAMILIES = ("gw1n", "gw2a", "gw5a")


class PrimitiveLibrary(NamedTuple):
    """A primitive library file, with the `read_verilog` flags that the family's pass reads it with.

    Xeda reads the library first, to check the hierarchy. The pass reads it again in its `begin`
    step (`-lib -specify`; a later read replaces an earlier blackbox module). Xeda uses the flags
    of the pass, so the two reads agree."""

    path: str
    flags: tuple[str, ...] = ("-lib", "-specify")


#: `synth_xilinx -family` values, the same in every supported yosys release (0.63 to 0.69).
XILINX_FAMILIES = frozenset(
    ("xcup", "xcu", "xc7", "xc6s", "xc6v", "xc5v", "xc4v", "xc3sda", "xc3sa", "xc3se", "xc3s")
    + ("xc2vp", "xc2v", "xcve", "xcv")
)

#: The LUT4-based `synth_xilinx` families, which take no `-widemux`.
XILINX_LUT4_FAMILIES = frozenset(
    ("xc4v", "xc3sda", "xc3sa", "xc3se", "xc3s", "xc2vp", "xc2v", "xcve", "xcv")
)

#: `fpga.family` spellings -> `synth_xilinx -family`, besides the `-usp`/`-us`/`7` suffixes.
XILINX_FAMILY_NAMES = {
    "spartan6": "xc6s",
    "spartan-6": "xc6s",
    "virtex6": "xc6v",
    "virtex-6": "xc6v",
    "virtex5": "xc5v",
    "virtex-5": "xc5v",
    "virtex4": "xc4v",
    "virtex-4": "xc4v",
    "spartan3": "xc3s",
    "spartan-3": "xc3s",
    "spartan3a": "xc3sa",
    "spartan3e": "xc3se",
}

# Mapped primitive footprints, in 6-input LUTs. A logic LUT cell is one LUT, LUT6_2 can use both
# outputs, a shift register is one LUT, and a distributed RAM or ROM takes as many LUTs as its
# bits need. One LUT holds 64 x 1 or 32 x 2 bits, and a dual-port memory has a second copy for
# its read port. A ROM is a LUT that holds an INIT value, which Vivado reports as logic. The MUXF7
# and MUXF8 that join the LUTs of a deep ROM are not LUTs.
# These are estimates at the synthesis stage. They are no placement occupancy, and they do not
# claim to equal the Vivado reports.
XILINX_LUT_FOOTPRINT = {
    **{f"LUT{width}": ("logic", 1) for width in range(1, 7)},
    "LUT6_2": ("logic", 2),
    "CFGLUT5": ("logic", 1),
    # an inverter is a LUT1 (UG953); yosys absorbs one into an invertible pin only under
    # `-ise`, so a data-path `INV` that a design instantiates costs a LUT here
    "INV": ("logic", 1),
    # the multi-port memories, whose names do not follow <depth>X<width><S|D>
    "RAM32M": ("ram", 4),
    "RAM64M": ("ram", 4),
    "RAM32M16": ("ram", 8),
    "RAM64M8": ("ram", 8),
    "RAM32X16DR8": ("ram", 8),
    "RAM64X8SW": ("ram", 8),
}

_XILINX_LUT_MEMORY = re.compile(r"(RAM|ROM)(\d+)X(\d+)([SD])?(?:_1)?")
_XILINX_SRL = re.compile(r"SRLC?(?:16|32)E?(?:_1)?")


def xilinx_lut_footprint(cell: str) -> tuple[str, int] | None:
    """Return `(kind, LUTs)` for a mapped Xilinx primitive, or None for a cell built from no LUTs.

    The kind is `logic` (a distributed ROM included), `ram` or `srl`. LUTs is the number of LUTs
    that the primitive occupies.
    """
    known = XILINX_LUT_FOOTPRINT.get(cell)
    if known is not None:
        return known
    if _XILINX_SRL.fullmatch(cell):
        return ("srl", 1)
    memory = _XILINX_LUT_MEMORY.fullmatch(cell)
    if memory is None:
        return None
    memory_kind, depth, width, ports = memory[1], int(memory[2]), int(memory[3]), memory[4]
    # up to 32 deep, one LUT gives two bits of a word; deeper, a bit takes depth / 64 LUTs
    luts = (width + 1) // 2 if depth <= 32 else width * (depth // 64)
    return ("ram" if memory_kind == "RAM" else "logic", luts * (2 if ports == "D" else 1))


def _abc9_mode(target: str, release: YosysRelease) -> Literal["opt-in", "default", "always"]:
    """Return how the synthesis pass of the family `target` in yosys `release` uses ABC9.

    The result is "opt-in" (only with `-abc9`, for Xilinx), "default" (unless `-noabc9`, for the
    other families) or "always" (it cannot be turned off, from yosys 0.69, which also dropped
    `-retime`). The values come from the pass sources of every release from the Xeda minimum,
    0.63, to 0.69.
    """
    if release >= (0, 69):
        return "always"
    return "opt-in" if target == "xilinx" else "default"


def _given_the_attribute(attributes: dict, name: str) -> list[str]:
    """Return the objects to which a `set_attribute` or `set_mod_attribute` mapping gives `name`.

    Only a value that sets the attribute counts (0 does not). A value with no object gives
    `every object`.
    """
    value = attributes.get(name)
    if isinstance(value, dict):
        return [str(path) for path, given in value.items() if given not in (0, False)]
    return [] if value is None or value in (0, False) else ["every object"]


class YosysFpga(YosysSynthesisBase, FpgaSynthFlow):
    """Synthesize a design for an FPGA device with Yosys."""

    minimum_yosys = MINIMUM_YOSYS

    results_description = describe_results(
        "LUT",
        "lut",
        "ff",
        **{
            "LUT:LOGIC": MetricSpec(
                "Mapped Xilinx logic LUT footprint estimate at the synthesis stage.", "integer"
            ),
            "LUT:RAM": MetricSpec(
                "Mapped distributed RAM LUT footprint estimate at the synthesis stage.", "integer"
            ),
            "LUT:SRL": MetricSpec(
                "Mapped shift-register LUT footprint estimate at the synthesis stage.", "integer"
            ),
            "LUT:STAGE": MetricSpec(
                "Stage represented by the canonical LUT resource estimate.", "text"
            ),
            "LUT:METHOD": MetricSpec(
                "Method used to estimate the canonical LUT resource units.", "text"
            ),
            "FF": MetricSpec("Number of flip-flops (registers) used.", "integer"),
        },
    )

    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
        }
    )

    class Settings(YosysSynthesisBase.Settings, FpgaSynthFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **YosysSynthesisBase.Settings.removed_settings,
            "nosynth": "`stop_after: rtl`, which stops after the RTL stage and writes the RTL "
            "outputs but no netlist (set `netlist_json` and `netlist_verilog` to null with it)",
            "netlist_verilog_extmem": "`netlist_verilog_flags: [-extmem]`, which `write_verilog` gets",
        }
        netlist_src_attrs: bool = Field(
            True,
            description="Keep `src` attributes (source file and line) in the written netlists "
            "(JSON, Verilog and BLIF). On by default: nextpnr's reports cite them as source "
            "locations when it places this netlist.",
        )
        netlist_edif: Path = Field(
            Path("netlist.edif"),
            description="The EDIF netlist for Vivado (`vivado_impl`), as a name in the run "
            "directory or a location to deliver it to. The flow writes it with `write_edif "
            "-pvector bra`, so Vivado reads every bus in the right order. It writes it only for a "
            "Xilinx device when the settings make the synthesis flat, because Vivado reads a flat "
            "netlist only. `flatten: false`, `keep_hierarchy`, `black_box` and `stop_after` "
            "prevent it.",
            json_schema_extra=deliverable(),
        )
        synth_pass_only: bool = Field(
            False,
            description="Omit the preparation and cleanup that Xeda adds around the "
            "`synth_<family>` pass, which does its own elaboration and mapping. Pass flags and "
            "explicit ABC9 script choices apply in either mode. The flow reads the sources as a "
            "bare `yosys <files>` does, so the mode requires `read_verilog_flags: []`, "
            "`systemverilog: default` (the built-in reader of yosys) and no "
            "`read_systemverilog_flags`. It reads a Verilog source with plain `read_verilog` and "
            "a SystemVerilog source with `read_verilog -sv`, as the `type` of the source says. "
            "Yosys does the same by file suffix, unless the design gives a `type` that "
            "contradicts the suffix. To compare with a native yosys invocation, also match "
            "source paths and order. When false, Xeda elaborates and optimizes around the pass "
            "and gives ABC9 a delay derived from the clock. Every flag of the pass itself "
            "(`flatten`, `abc9`, `nobram`, `widemux`, `synth_flags`, ...) applies in either "
            "mode. A setting that would add a step before or after the pass, or make it read "
            "differently from `yosys <files>`, is refused.",
        )
        flatten: Optional[bool] = Field(
            None,
            description="Flatten the design hierarchy. `true` flattens before the RTL outputs "
            "are written and before synthesis. `false` keeps the hierarchy (`-noflatten` for the "
            "Lattice, iCE40 and Gowin passes). Unset: `true` for a Xilinx device, so the RTL "
            "outputs are flat too (`synth_xilinx` alone keeps the hierarchy, and flattening "
            "measured better). For the other families, unset leaves the choice to the pass, "
            "which flattens on its own. Under `synth_pass_only`, unset leaves the choice to the "
            "pass for every family.",
        )
        read_verilog_flags: list[str] = Field(
            ["-sv"],
            description="Flags passed to yosys' `read_verilog` for each Verilog source. They do "
            "not apply to the Verilog sources of a design that `slang` reads together with its "
            "SystemVerilog sources. Add `-noautowire` explicitly if its stricter undeclared-net "
            "behavior is required. Must be `[]` under `synth_pass_only`, which reads sources as "
            "plain `yosys <file>` does.",
        )
        abc9: bool = Field(
            True,
            description="Map LUTs with ABC9. `false` maps with classic ABC (`-noabc9`, or no "
            "`-abc9` where ABC9 is opt-in). Yosys 0.69 and newer require ABC9. For iCE40, the "
            "separate `noabc` setting selects the built-in LUT mapping.",
        )
        flow3: Optional[bool] = Field(
            None,
            description="Boolean ABC9 script choice: true selects `flow3`, false leaves the "
            "script to the synthesis pass. Use `abc9_script` for another named script; do not "
            "give both. Unset uses flow3 in Xeda's full recipe and leaves the pass's script "
            "in `synth_pass_only`. Has no effect unless ABC9 maps LUTs.",
        )
        abc9_script: Optional[
            Literal["default", "default.area", "default.fast", "flow", "flow2", "flow3", "flow3mfs"]
        ] = Field(
            None,
            description="Select one of yosys's included ABC9 scripts in either synthesis "
            "mode: default, default.area, default.fast, flow, flow2, flow3 or flow3mfs. "
            "Unset uses flow3 in Xeda's full recipe and the synthesis pass's own choice in "
            "synth_pass_only. Has no effect when ABC9 mapping is disabled. Cannot be combined "
            "with the `flow3` setting.",
        )
        retime: bool = Field(
            False,
            description="Retime flip-flops with ABC (`-retime`). Removed in yosys 0.69.",
        )
        nobram: bool = Field(False, description="Do not map to block RAM cells.")
        nodsp: bool = Field(False, description="Do not use DSP resources.")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells.")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`).",
        )
        nowidelut: bool = Field(
            False,
            description="Disable wide LUT implementations that use hard MUX resources. Leave "
            "false to use the normal mapping choices of the family's synthesis pass.",
        )
        abc_dff: bool = Field(False, description="Run abc and abc9 with `-dff`.")
        widemux: int = Field(
            0,
            description="Infer hard multiplexer resources for muxes with at least this many "
            "inputs. 0 disables it. The minimum is 2, and 5 or more is recommended.",
            json_schema_extra=count(),
        )

        @field_validator("widemux")
        @classmethod
        def _validate_widemux(cls, value: int) -> int:
            if value != 0 and value < 2:
                raise ValueError("widemux is 0 (off) or a mux size of at least 2")
            return value

        synth_flags: List[str] = Field(
            [],
            description="Extra flags appended verbatim to the device-specific `synth_<family>` "
            "command of yosys, for flags that have no setting of their own.",
        )
        pre_synth_opt: bool = Field(
            False,
            description="Run additional optimization steps before synthesis.",
        )
        post_synth_opt: bool = Field(
            False,
            description="Run additional optimization steps after synthesis.",
        )
        stop_after: Optional[Literal["rtl"]] = Field(None, description=STOP_AFTER_DESCRIPTION)
        black_box: List[str] = Field(
            [],
            description="Modules to treat as black boxes: their contents are discarded and only "
            "their interface is kept.",
        )
        clockgate_map: Optional[Path] = Field(
            None, description="Verilog file with device-specific clock-gating cell mappings."
        )
        ice40_spram: bool = Field(
            False, description="Infer UltraPlus SPRAM256KA cells with `synth_ice40 -spram`."
        )
        ice40_dsp: bool = Field(
            False, description="Infer UltraPlus MAC16 DSP cells with `synth_ice40 -dsp`."
        )
        ice40_device: Optional[Literal["hx", "lp", "u"]] = Field(
            None,
            description="iCE40 timing model. Unset: inferred from `fpga.device` or `fpga.type`.",
        )

        @field_validator("netlist_edif", mode="before")
        @classmethod
        def _the_edif_is_always_written(cls, value):
            """Refuse an empty `netlist_edif`: the flow has no switch for the EDIF netlist.

            The EDIF netlist is a declared output (`netlist_edif`) whenever the synthesis is flat,
            whichever consumer asks for it, so one run serves every consumer. The setting only
            names the file, or the location to deliver it to.
            """
            if value is None or (isinstance(value, (str, Path)) and not str(value).strip()):
                raise ValueError(
                    "yosys_fpga always writes its EDIF netlist for a flattened Xilinx synthesis; "
                    "give a file name or leave the default"
                )
            return value

        def edif_problem(self) -> Optional[str]:
            """Return why the synthesis writes no EDIF netlist, or None when it writes one.

            The method is pure and does not raise, so planning can ask it of any settings. It
            reads `fpga.vendor` and does not call `synthesis_target()`, which raises for a device
            that yosys cannot synthesize.
            """
            if self.fpga is None:
                return "no `fpga` device is given"
            vendor = (self.fpga.vendor or "").lower()
            if vendor != "xilinx":
                return f"the FPGA is not a Xilinx device (fpga.vendor is {vendor or None!r})"
            if self.stop_after == "rtl":
                return "`stop_after: rtl` stops the flow before the synthesis"
            if not self.effective_flatten():
                cause = (
                    "`flatten` is false"
                    if self.flatten is False
                    else "`synth_pass_only` leaves `flatten` to the synthesis pass, which keeps "
                    "the hierarchy"
                )
                return f"{cause}, and Vivado reads a flat netlist only; set `flatten: true`"
            return self._kept_out_of_the_flat_netlist()

        def _kept_out_of_the_flat_netlist(self) -> Optional[str]:
            """Return what the settings keep out of a flat netlist, or None.

            `flatten` leaves alone a module or a cell with the attribute `keep_hierarchy`,
            whichever setting gave it, and `black_box` empties a module. Vivado reads a flat
            netlist and resolves no module that the netlist does not define. `vivado_impl` finds
            in the netlist a `keep_hierarchy` that the settings do not show (one in the HDL or in
            `rtl.attributes`). It does not find a black box that the HDL makes: the file declares
            it as it does a primitive.
            """
            if self.keep_hierarchy:
                return (
                    "`keep_hierarchy` keeps "
                    + ", ".join(self.keep_hierarchy)
                    + " unflattened, and Vivado reads a flat netlist only"
                )
            for setting in ("set_mod_attribute", "set_attribute"):
                kept = _given_the_attribute(getattr(self, setting), "keep_hierarchy")
                if kept:
                    return (
                        f"`{setting}` gives `keep_hierarchy` to {', '.join(kept)}, so the "
                        "synthesis keeps the hierarchy, and Vivado reads a flat netlist only"
                    )
            if self.black_box:
                boxes = ", ".join(self.black_box)
                return (
                    f"`black_box` makes {boxes} "
                    + ("a black box" if len(self.black_box) == 1 else "black boxes")
                    + ", and Vivado cannot resolve a module that the netlist does not define"
                )
            return None

        def synthesis_target(self) -> str:
            """Return the FPGA family that selects the yosys synthesis pass.

            The result is xilinx, gowin, ecp5, ice40 or nexus.
            """
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            vendor = (self.fpga.vendor or "").lower()
            if vendor == "xilinx":
                return "xilinx"
            if vendor == "gowin" or family == "gowin" or family in GOWIN_FAMILIES:
                return "gowin"
            if family in ("ecp5", "ice40", "nexus"):
                return family
            raise FlowSettingsException(
                f"yosys has no FPGA synthesis for fpga.family={family or None!r} "
                f"(vendor={vendor or None!r}); supported are Xilinx, Gowin, and the Lattice "
                "families ecp5, ice40 and nexus."
            )

        def primitive_libraries(self) -> list[PrimitiveLibrary]:
            """Return the primitive models that the synthesis pass reads in its `begin` step.

            Xeda reads them the same way before it checks the hierarchy. Every supported yosys
            release (0.63 to 0.69) reads the same files. `PASS_READS` in
            `tests/test_yosys_fpga_flags.py` records them, checked by hand against the pass of
            each release. A test compares only the installed yosys with this list, in both
            directions: its help, and the files it reads in a run.
            """
            target = self.synthesis_target()
            if target == "xilinx":
                return [
                    PrimitiveLibrary("+/xilinx/cells_sim.v"),
                    PrimitiveLibrary("+/xilinx/cells_xtra.v", ("-lib",)),
                ]
            if target in ("ecp5", "nexus"):  # `synth_<family>` is `synth_lattice -family <family>`
                return [
                    PrimitiveLibrary(f"+/lattice/cells_sim_{target}.v"),
                    PrimitiveLibrary(f"+/lattice/cells_bb_{target}.v"),
                ]
            if target == "gowin":
                return [
                    PrimitiveLibrary("+/gowin/cells_sim.v"),
                    PrimitiveLibrary(f"+/gowin/cells_xtra_{self._gowin_family()}.v"),
                ]
            assert target == "ice40", target
            define = f"ICE40_{self._ice40_device().upper()}"
            return [PrimitiveLibrary("+/ice40/cells_sim.v", ("-D", define, "-lib", "-specify"))]

        def effective_flatten(self) -> Optional[bool]:
            """Return whether the recipe flattens the design.

            The recipe flattens in its own step before the RTL outputs, and with `-flatten` on
            `synth_xilinx`. The result is `flatten` when that is set. Unset on a Xilinx device,
            it is True, so the run is the one that `flatten=True` gives, RTL outputs included
            (`synth_xilinx` alone keeps the hierarchy, and flattening measured better). With
            `synth_pass_only` it is False, and the pass keeps its own default. Unset on any other
            family, it is None: their passes flatten on their own unless told `-noflatten`.
            """
            if self.flatten is None and self.synthesis_target() == "xilinx":
                return not self.synth_pass_only
            return self.flatten

        def synth_command(self, release: YosysRelease) -> List[str]:
            """Return the device synthesis command for yosys `release`, with its flags.

            Each setting becomes the flag that the family's pass takes for it in that release (see
            `_abc9_mode`). The method rejects a combination that the pass rejects. A setting is
            honored or an error, and never dropped. `synth_flags` follow verbatim.
            """
            target = self.synthesis_target()
            if target == "xilinx":
                command = ["synth_xilinx", *self._xilinx_family()]
            elif target == "gowin":
                command = ["synth_gowin", "-family", self._gowin_family()]
            elif target == "nexus":
                # `synth_nexus` is `synth_lattice -family lifcl`, which cannot name Certus-NX.
                command = ["synth_lattice", "-family", self._nexus_family()]
            else:
                command = [f"synth_{target}"]
            name = command[0]
            version = ".".join(map(str, release))

            mode = _abc9_mode(target, release)
            if self.noabc:
                if target != "ice40":
                    raise FlowSettingsException(f"noabc is for iCE40 only; {name} has no -noabc.")
                # Before 0.69 `synth_ice40` rejects -noabc while ABC9 is on, as it is by default.
                command += ["-noabc9", "-noabc"] if mode == "default" else ["-noabc"]
            elif self.abc9 and mode == "opt-in":
                command.append("-abc9")
            elif not self.abc9 and mode == "default":
                command.append("-noabc9")
            elif not self.abc9 and mode == "always":
                instead = (
                    "; noabc=true maps with yosys' built-in LUT mapping instead"
                    if target == "ice40"
                    else ""
                )
                raise FlowSettingsException(
                    f"{name} of yosys {version} always maps with ABC9, so abc9=false needs yosys "
                    f"0.68 or older{instead}."
                )
            if self.retime:
                if mode == "always":  # both changed in yosys 0.69
                    raise FlowSettingsException(
                        f"yosys {version} removed `{name} -retime`; retime=true needs yosys 0.68 "
                        "or older."
                    )
                if self.noabc:
                    raise FlowSettingsException("retime retimes with ABC, which noabc turns off.")
                # Only synth_gowin retimes with a separate classic ABC run; the other passes
                # reject -retime while ABC9 is on.
                if self.abc9 and target != "gowin":
                    raise FlowSettingsException(
                        f"{name} retimes with classic ABC only, so retime=true needs abc9=false."
                    )
                command.append("-retime")
            if self.abc_dff:
                if self.noabc:
                    raise FlowSettingsException("abc_dff needs ABC/ABC9, which noabc turns off.")
                if target == "gowin":
                    raise FlowSettingsException("synth_gowin has no -dff option for abc_dff.")
                command.append("-dff")
            # `synth_xilinx` keeps the hierarchy unless told to flatten; the others flatten it
            # unless told not to. An unset `flatten` leaves the choice to the pass, except that
            # the Xeda recipe flattens for Xilinx (`effective_flatten`).
            flatten = self.effective_flatten()
            if target == "xilinx":
                if flatten:
                    command.append("-flatten")
            elif flatten is False:
                command.append("-noflatten")
            if self.nobram:
                command.append("-nobram")
            if self.nolutram:
                if target == "ice40":
                    raise FlowSettingsException(
                        "synth_ice40 has no -nolutram: iCE40 has no LUT RAM."
                    )
                command.append("-nolutram")
            # iCE40 maps DSPs only with `ice40_dsp`, so `nodsp` needs no flag there.
            if self.nodsp and target != "ice40":
                command.append("-nodsp")
            if self.nowidelut:
                if target == "ice40":
                    raise FlowSettingsException("synth_ice40 has no -nowidelut.")
                command.append("-nowidelut")
            if self.widemux:
                if target != "xilinx":
                    raise FlowSettingsException(
                        f"widemux is for Xilinx only; {name} has no -widemux."
                    )
                family = command[command.index("-family") + 1] if "-family" in command else None
                if family in XILINX_LUT4_FAMILIES:
                    raise FlowSettingsException(
                        f"synth_xilinx has no widemux for the LUT4-based family {family}."
                    )
                command += ["-widemux", str(self.widemux)]
            if target == "ice40":
                command += self._ice40_flags()
            elif self.ice40_device or self.ice40_dsp or self.ice40_spram:
                raise FlowSettingsException(
                    f"ice40_device, ice40_dsp and ice40_spram are for iCE40 only; {name} takes "
                    "none of them."
                )
            return command + list(self.synth_flags)

        @model_validator(mode="after")
        def _one_abc9_script_choice(self):
            if self.abc9_script is not None and self.flow3 is not None:
                raise ValueError("use `abc9_script` or `flow3`, not both")
            return self

        def abc9_scratchpad(self) -> List[str]:
            """Return the `scratchpad` commands that tune ABC9 before the synthesis pass runs.

            The method runs where the script is written, as `synth_command` does, because the
            result depends on several settings: ABC9 must be mapping at all, an explicit script
            applies in either mode, and only the full recipe supplies an implicit flow3 and a
            delay derived from the clock. The script itself comes from the constpad of yosys.
            Xeda does not copy it.
            """
            if not self.abc9 or self.noabc:
                return []
            commands: List[str] = []
            script = self.abc9_script
            if script is None and (
                self.flow3 is True or (self.flow3 is None and not self.synth_pass_only)
            ):
                script = "flow3"
            if script is not None:
                commands.append(f"scratchpad -copy abc9.script.{script} abc9.script")
            clock = self.main_clock
            if not self.synth_pass_only and clock:
                commands.append(f"scratchpad -set abc9.D {clock.period.to('ps') / 1.5}")
            return commands

        def synth_pass_only_conflicts(self) -> List[Tuple[str, str]]:
            """Return every setting that `synth_pass_only` cannot honor, as (setting, why) pairs.

            The mode runs the synthesis pass of the family and nothing else. It refuses a setting
            that adds a pass before or after the pass, that needs the elaborated hierarchy (the
            mode never builds it), or that makes the pass read differently from the reference
            invocation (a file it does not read, a reader flag it does not pass). The method is
            pure, so the launcher can report the conflicts at planning time
            (`check_settings_supported`), and `run()` can report them again once `init()` has
            folded the attributes of the design in. The result is empty when the mode is off.

            A flag of the pass is never a conflict: `synth_command` renders those into the
            command line of the pass, and they mean the same in either recipe. A constrained clock
            is no conflict either: the mode does not pass it on to ABC9. A reader flag is a
            conflict. The reference reads a Verilog source with `read_verilog` and no flag of its
            own, so `read_verilog_flags` must be empty. The value decides, whether or not the user
            wrote it, because `settings.json` writes every field and a reload would otherwise
            differ from the first run. That includes the default, so the mode needs
            `read_verilog_flags: []` to be written. A front end other than the one of yosys
            (`systemverilog`, whose default is the slang plugin) and the flags that only the
            plugin of that front end reads (`read_systemverilog_flags`) are conflicts for the
            same reason. All three are values of settings, so the check stays pure and
            class-level whatever sources the design has: the mode reads the sources as a bare
            `yosys <files>` does. The `type` of a source selects its reader: the type that the
            suffix implies, unless the design states one ("every design source is typed"). A
            source whose explicit `type` contradicts its suffix is read as that type. The author
            states the type, so it is no conflict, and reading the source by its suffix would
            make it read differently in the full recipe.
            """
            if not self.synth_pass_only:
                return []
            runs_a_pass = "`synth_pass_only` runs no pass but the family's own, so it cannot "
            needs_hierarchy = (
                "`synth_pass_only` lets the synthesis pass elaborate the design, so there is no "
                "elaborated hierarchy before it for "
            )
            no_rtl_stage = (
                "`synth_pass_only` has no pre-synthesis stage, because the pass elaborates the "
                "design itself, so it cannot "
            )
            conflicts: List[Tuple[str, str]] = []
            if self.prep is not None:
                conflicts.append(
                    ("prep", runs_a_pass + "elaborate with `prep`; the pass does that itself")
                )
            if self.pre_synth_opt:
                conflicts.append(
                    ("pre_synth_opt", runs_a_pass + "optimize before it with `pre_synth_opt`")
                )
            if self.post_synth_opt:
                conflicts.append(
                    ("post_synth_opt", runs_a_pass + "optimize after it with `post_synth_opt`")
                )
            if self.splitnets:
                conflicts.append(
                    ("splitnets", runs_a_pass + "split the nets of the netlist it produced")
                )
            if self.post_synth_rename:
                conflicts.append(
                    (
                        "post_synth_rename",
                        runs_a_pass + "rename the objects of the netlist it produced",
                    )
                )
            if self.black_box:
                conflicts.append(
                    ("black_box", needs_hierarchy + "`blackbox` to take a module out of")
                )
            if self.keep_hierarchy:
                conflicts.append(
                    (
                        "keep_hierarchy",
                        needs_hierarchy + "`setattr -mod` to mark a module in; write "
                        "`(* keep_hierarchy *)` on the module in the HDL instead, which the pass "
                        "reads as it elaborates",
                    )
                )
            for name in ("set_attribute", "set_mod_attribute"):
                if getattr(self, name):
                    conflicts.append(
                        (
                            name,
                            needs_hierarchy + f"`setattr` to apply `{name}` to (the design's own "
                            "`rtl.attributes` are merged into `set_attribute`)",
                        )
                    )
            if self.read_verilog_flags:
                conflicts.append(
                    (
                        "read_verilog_flags",
                        "`synth_pass_only` reads each Verilog source as `yosys <file>` does, with "
                        f"no reader flag, so it cannot also pass {self.read_verilog_flags!r}: the "
                        "flags change how a `.v` source parses and what it accepts (a "
                        "SystemVerilog source is read with `-sv` regardless, as yosys does for "
                        "`.sv`). Write "
                        "`read_verilog_flags: []` (`-s read_verilog_flags=` on the command line)",
                    )
                )
            if self.systemverilog != "default":
                front_end = {"slang": "`read_slang`", "uhdm": "Surelog/UHDM"}[self.systemverilog]
                conflicts.append(
                    (
                        "systemverilog",
                        "`synth_pass_only` reads SystemVerilog as `yosys <file>.sv` does, with "
                        f"the built-in `read_verilog -sv`, not {front_end} "
                        f"(`systemverilog: {self.systemverilog}`), which reads and elaborates "
                        "differently before the pass starts. Write `systemverilog: default` "
                        "(`-s systemverilog=default` on the command line)",
                    )
                )
            if self.read_systemverilog_flags:
                conflicts.append(
                    (
                        "read_systemverilog_flags",
                        "`synth_pass_only` reads SystemVerilog as `yosys <file>.sv` does, which "
                        "passes no flag of its own to a plugin front end, so it cannot also pass "
                        f"{self.read_systemverilog_flags!r}. Write `read_systemverilog_flags: []` "
                        "(`-s read_systemverilog_flags=` on the command line)",
                    )
                )
            if self.clockgate_map:
                conflicts.append(
                    (
                        "clockgate_map",
                        "`synth_pass_only` reads the design's sources and the libraries named "
                        "in `verilog_lib`, so it cannot also read the clock-gating map "
                        "`clockgate_map`: the pass reads no such map, and every extra "
                        "`read_verilog` renumbers the cells that the pass names",
                    )
                )
            if self.rmports:
                conflicts.append(
                    (
                        "rmports",
                        runs_a_pass + "remove ports with the separate `rmports` pass",
                    )
                )
            # `sta` and `ltp` also make `YosysSynthesisBase.init` set `flatten`, which would add
            # `-flatten` to the pass. This list has no `flatten` entry, so the check refuses only
            # the request of the user for `sta` or `ltp`.
            for name, report in (
                ("sta", "run static timing analysis"),
                ("ltp", "report the longest path"),
            ):
                if getattr(self, name):
                    conflicts.append(
                        (
                            name,
                            runs_a_pass + f"{report} (`{name}`) after it, and `{name}` would turn "
                            "on `flatten`, which changes the command line of the pass itself",
                        )
                    )
            if self.stop_after is not None:
                conflicts.append(("stop_after", no_rtl_stage + f"stop after {self.stop_after!r}"))
            for name in ("rtl_json", "rtl_verilog", "rtl_graph"):
                if getattr(self, name):
                    conflicts.append((name, no_rtl_stage + f"write `{name}`"))
            return conflicts

        def _ice40_device(self) -> str:
            """Return `hx`, `lp` or `u`: `ice40_device`, else the one that `fpga` names."""
            assert self.fpga is not None
            device: Optional[str] = self.ice40_device
            if device is None:
                device_type = (self.fpga.type or "").lower()
                name = (self.fpga.device or "").lower()
                if device_type:
                    if device_type not in ("hx", "lp", "up", "u"):
                        raise FlowSettingsException(
                            f"Unknown iCE40 fpga.type {device_type!r}; expected hx, lp, up or u."
                        )
                    device = "u" if device_type in ("up", "u") else device_type
                elif name.startswith(("ice40up", "ice5lp")):
                    device = "u"
                else:
                    device = "lp" if name.startswith("ice40lp") else "hx"
            return device

        def _ice40_flags(self) -> List[str]:
            device = self._ice40_device()
            flags: List[str] = ["-device", device]
            if (self.ice40_dsp or self.ice40_spram) and device != "u":
                raise FlowSettingsException(
                    "ice40_dsp and ice40_spram need an UltraPlus (iCE40UP/iCE5LP) device."
                )
            if self.ice40_dsp:
                if self.nodsp:
                    raise FlowSettingsException("ice40_dsp and nodsp cannot both be true.")
                flags.append("-dsp")
            if self.ice40_spram:
                flags.append("-spram")
            return flags

        def _nexus_family(self) -> str:
            assert self.fpga is not None
            device = (self.fpga.device or self.fpga.part or "").lower()
            if device.startswith("lfd2nx"):
                return "lfd2nx"
            if device.startswith("lifcl"):
                return "lifcl"
            raise FlowSettingsException(
                "Nexus synthesis needs an LIFCL or LFD2NX device or part to select its "
                f"synth_lattice family; got {device or None!r}."
            )

        def _xilinx_family(self) -> List[str]:
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            if not family:
                generation = (self.fpga.generation or "").lower()
                inferred = {"usp": "xcup", "u": "xcu", "7": "xc7"}.get(generation)
                if inferred:
                    return ["-family", inferred]
                if self.fpga.part or self.fpga.device or generation:
                    raise FlowSettingsException(
                        "Cannot select synth_xilinx family from the Xilinx device: "
                        f"part={self.fpga.part!r}, device={self.fpga.device!r}, "
                        f"generation={generation or None!r}; set fpga.family or a recognized "
                        "generation (usp, u, or 7)."
                    )
                return []  # an unspecified Xilinx device gets the Series 7 default of synth_xilinx
            if family.endswith("-usp"):
                target = "xcup"
            elif family.endswith("-us"):
                target = "xcu"
            elif family.endswith("7"):
                target = "xc7"
            else:
                target = XILINX_FAMILY_NAMES.get(family, family)
            if target not in XILINX_FAMILIES:
                raise FlowSettingsException(
                    f"synth_xilinx has no family for fpga.family={family!r}; its families are "
                    f"{', '.join(sorted(XILINX_FAMILIES))}."
                )
            return ["-family", target]

        def _gowin_family(self) -> str:
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            device = (self.fpga.device or self.fpga.part or "").lower()
            inferred = next((f for f in GOWIN_FAMILIES if device.startswith(f)), None)
            if family in GOWIN_FAMILIES:
                if inferred and inferred != family:
                    raise FlowSettingsException(
                        f"Gowin family {family!r} conflicts with device {device!r}."
                    )
                chosen = family
            elif family not in ("", "gowin"):
                raise FlowSettingsException(
                    f"Unknown Gowin family {family!r}; expected one of {', '.join(GOWIN_FAMILIES)}."
                )
            elif inferred is None:
                raise FlowSettingsException(
                    "A generic Gowin family requires a device or part beginning with GW1N, "
                    "GW2A or GW5A."
                )
            else:
                chosen = inferred
            return chosen

    class Outputs(FpgaSynthFlow.Outputs):
        netlist: Path | None = Out(
            SourceType.JsonNetlist,
            enabled_by="netlist_json",
            description="The synthesized JSON netlist, written at `netlist_json`, for nextpnr.",
        )
        netlist_edif: Path | None = Out(
            SourceType.Edif,
            description="The synthesized netlist as EDIF, with bus ranges, written at "
            "`netlist_edif`, for Vivado (`vivado_impl`). Written when the settings make a Xilinx "
            "synthesis flat, and absent otherwise.",
        )

    @classmethod
    def output_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        if name == "netlist_edif":
            assert isinstance(settings, cls.Settings)
            return () if settings.edif_problem() else (SourceType.Edif,)
        return super().output_types(settings, name)

    @classmethod
    def enable_output(cls, settings: Flow.Settings, name: str, *, design_name: str) -> None:
        if name != "netlist_edif":
            refuse_output_after_rtl(cls, settings, name)
            return super().enable_output(settings, name, design_name=design_name)
        assert isinstance(settings, cls.Settings)
        if settings.fpga is None:
            return  # no `fpga` yet: the required-settings check names what is missing
        problem = settings.edif_problem()
        if problem:
            raise ValueError(f"no EDIF netlist is written: {problem}")
        # nothing to switch on: a flat Xilinx synthesis always writes it

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse settings that the run cannot honor, before any producer runs.

        The refused settings are: a step that `synth_pass_only` does not run; a result of a later
        stage when the run stops after the RTL (`stop_after_conflicts`); and an EDIF file name
        for a synthesis that writes no EDIF netlist. The method is class-level and pure, so the
        refusal comes at planning time, also under `xeda run --dry-run`. `run()` checks the
        pass-only conflicts again after `init()` has merged the attributes of the design, which
        this method cannot see.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        cls._refuse(settings.synth_pass_only_conflicts() + stop_after_conflicts(settings))
        # judged by value, as every setting is: a file name other than the default asks for one
        if settings.netlist_edif != cls.Settings.model_fields["netlist_edif"].get_default():
            problem = settings.edif_problem()
            if problem:
                raise FlowSettingsException(
                    f"netlist_edif names a file for the EDIF netlist, but none is written: {problem}"
                )

    @classmethod
    def _refuse_synth_pass_only_conflicts(cls, settings: "YosysFpga.Settings") -> None:
        """Raise one error that lists each setting that `synth_pass_only` cannot honor."""
        cls._refuse(settings.synth_pass_only_conflicts())

    @classmethod
    def _refuse(cls, conflicts: list[tuple[str, str]]) -> None:
        if conflicts:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in conflicts],
                cls.Settings,
            )

    def verilog_libraries_to_read(self, libraries: list[PrimitiveLibrary]) -> list[Path]:
        """Return the `verilog_lib` entries that the script reads.

        These are all entries but a library that the synthesis pass reads itself. The method
        skips an entry that names one of `libraries`, in either recipe. It decides by the file
        that the entry names, and the spelling does not matter. `+/xilinx/cells_sim.v` (the
        spelling of yosys, compared as text) and the same file by its absolute path, through a
        link or in another letter case (compared against the data directory of the installed
        yosys, which `+/` stands for) are one library. The decision does not depend on what the
        script reads itself, which is nothing of the kind under `synth_pass_only`: one more
        `read_verilog` renumbers the cells of the design.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        passes = [library.path for library in libraries]
        data_dir: Optional[Path] = None

        def is_pass_library(entry: Path) -> bool:
            nonlocal data_dir
            text = entry.as_posix()
            if text.startswith("+/"):
                return any(posixpath.normpath(text) == posixpath.normpath(p) for p in passes)
            if data_dir is None:
                try:
                    data_dir = yosys_data_dir(self.yosys)
                except ToolException as error:
                    raise FlowFatalError(
                        f"Cannot tell whether `verilog_lib` entry {text} is a library the "
                        f"{ss.synthesis_target()} pass reads itself: yosys' data "
                        f"directory is unknown ({error}). Name it as `+/...` or remove it."
                    ) from error
            return any(same_file(entry, data_dir / p.removeprefix("+/")) for p in passes)

        return [entry for entry in ss.verilog_lib if not is_pass_library(entry)]

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        # Judge as the plan did, on the settings as given. `init()` folds the `rtl.attributes`
        # of the design into `set_attribute`, and the run writes what the plan declared.
        problem = self.settings.edif_problem()
        super().init()
        if self.settings.clockgate_map:
            self.settings.clockgate_map = self.normalize_path_to_design_root(
                self.settings.clockgate_map
            )
        if problem is None:
            self.artifacts.netlist_edif = self.settings.netlist_edif

    def prepare_output_parents(self) -> None:
        super().prepare_output_parents()
        assert isinstance(self.settings, self.Settings)
        if self.artifacts.get("netlist_edif"):
            self.run_directory.inside(self.settings.netlist_edif).parent.mkdir(
                parents=True, exist_ok=True
            )

    def run(self) -> None:
        """Synthesize the design for the selected FPGA device."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self._refuse_synth_pass_only_conflicts(ss)
        self.prepare_output_parents()
        declared = self.outputs
        assert isinstance(declared, self.Outputs)
        if ss.netlist_json:
            declared.netlist = self.run_path / ss.netlist_json
        if self.artifacts.get("netlist_edif"):
            declared.netlist_edif = self.run_path / ss.netlist_edif
        assert ss.fpga is not None, "checked at launch (`required_settings`)"
        self.artifacts.timing_report = ss.reports_dir / "timing.rpt" if ss.sta else None
        # `stop_after: rtl` skips the stage that writes this report, so a stopped run lists none
        self.artifacts.utilization_report = (
            ss.reports_dir / "utilization.json" if ss.stop_after != "rtl" else None
        )
        release = yosys_release(self.yosys)
        synth_command = ss.synth_command(release)
        libraries = ss.primitive_libraries()

        abc_constr_file = None
        if ss.abc_constr:
            abc_constr_file = "abc.constr"
            with replacing_file(self.run_directory.writable(abc_constr_file)) as f:
                f.write("\n".join(ss.abc_constr) + "\n")

        script_path = self.copy_from_template(
            f"yosys_fpga_synth{self.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            # `read_files.ys` lists the VHDL files and `-e <top>` itself, as for `yosys`. With
            # `one_shot_elab`, the arguments would carry every file a second time.
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=False),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            # `read_files.ys` reads the clock-gating map from this variable. `yosys` passes none.
            clockgate_map=ss.clockgate_map,
            synth_command=synth_command,
            # `primitive_libraries` names what the pass reads in its `begin` step.
            # `synth_pass_only` lets the pass do that itself. An early read is redundant, and it
            # also changes the netlist: every `read_verilog` advances the `autoidx` of yosys,
            # which renumbers the generated cell names of the design, and ABC9 maps by those
            # names. Only the script omits the read; the method is unchanged.
            primitive_libraries=([] if ss.synth_pass_only else libraries),
            # A `verilog_lib` entry that names one of them is skipped in either recipe.
            verilog_libs=self.verilog_libraries_to_read(libraries),
            synth_pass_only=ss.synth_pass_only,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = [self.script_flag, script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args.extend(["-L", ss.log_file])
        # With a log file, the console shows only the messages of the flow, and the log has the
        # yosys output. `-T -Q` already come with the defaults of the tool (`yosys`).
        if ss.log_file and not ss.verbose and not ss.debug:
            args.append("-q")
        depfile = self.run_path / "yosys.d"
        args += ["-E", depfile]
        self.depfiles.append(depfile)
        self.run_yosys(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if not self.artifacts.utilization_report:
            return True

        if Path(self.artifacts.utilization_report).suffix == ".json":
            utilization = self.get_utilization()
            if not utilization:
                return False
            mod_util = utilization.get("modules")
            if mod_util:
                self.results["_hierarchical_utilization"] = mod_util
            design_util = utilization.get("design")
            if design_util:
                num_cells_by_type = design_util.get("num_cells_by_type")
                if num_cells_by_type:
                    design_util = {
                        **{k: v for k, v in design_util.items() if k != "num_cells_by_type"},
                        **num_cells_by_type,
                    }
                    self.results["_utilization"] = design_util

                def add_util_if_nonzero(name: str) -> None:
                    util = int(design_util.get(name, 0))
                    if util:
                        self.results[name] = util

                def add_util_sum_if_nonzero(group_name: str, names: List[str]):
                    util = sum(int(design_util.get(t, 0)) for t in names)
                    if util:
                        self.results[group_name] = util

                assert self.settings.fpga
                if self.settings.fpga.vendor == "xilinx":
                    lut_footprint = {"logic": 0, "ram": 0, "srl": 0}
                    for cell, count in design_util.items():
                        footprint = xilinx_lut_footprint(str(cell))
                        if footprint is not None:
                            lut_footprint[footprint[0]] += footprint[1] * int(count)
                    logic_luts = lut_footprint["logic"]
                    ram_luts = lut_footprint["ram"]
                    srl_luts = lut_footprint["srl"]
                    self.results["LUT"] = logic_luts + ram_luts + srl_luts
                    self.results["LUT:LOGIC"] = logic_luts
                    self.results["LUT:RAM"] = ram_luts
                    self.results["LUT:SRL"] = srl_luts
                    self.results["LUT:STAGE"] = "mapped"
                    self.results["LUT:METHOD"] = "primitive-footprint estimate"
                    add_util_sum_if_nonzero(
                        "FF",
                        [
                            "FDCE",  # D Flip-Flop with Clock Enable and Asynchronous Clear
                            "FDPE",  # D Flip-Flop with Clock Enable and Asynchronous Preset
                            "FDRE",  # D Flip-Flop with Clock Enable and Synchronous Reset
                            "FDSE",  # D Flip-Flop with Clock Enable and Synchronous Set
                        ],
                    )
                    add_util_sum_if_nonzero(
                        "LATCH",
                        [
                            "LDCE",  # Transparent Data Latch with Asynchronous Clear and Gate Enable
                            "LDPE",  # Transparent Data Latch with Asynchronous Preset and Gate Enable
                        ],
                    )

                    add_util_sum_if_nonzero("RAMB18", ["RAMB18", "RAMB18E1", "RAMB18E2"])
                    add_util_sum_if_nonzero("RAMB36", ["RAMB36", "RAMB36E1", "RAMB36E2"])
                    add_util_sum_if_nonzero("DSP", ["DSP48E1", "DSP48E2", "DSP48E"])
                    for res in ["CARRY4", "CARRY8", "MUXF7", "MUXF8", "MUXF9"]:
                        add_util_if_nonzero(res)

        return True


def sum_all_resources(design_util: dict, lst: Iterable) -> int:
    return sum(int(design_util.get(t, 0)) for t in lst)
