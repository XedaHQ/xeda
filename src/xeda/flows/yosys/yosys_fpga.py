import logging
from collections.abc import Iterable
from pathlib import Path
from typing import List, Literal, Optional

from ...dataclass import Field, field_validator
from ...flow import FlowSettingsException, FpgaSynthFlow, describe_results
from ...flows.ghdl import GhdlSynth
from .common import MINIMUM_YOSYS, YosysBase, YosysRelease, process_parameters, yosys_release

log = logging.getLogger(__name__)

GOWIN_FAMILIES = ("gw1n", "gw2a", "gw5a")

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


def _abc9_mode(target: str, release: YosysRelease) -> Literal["opt-in", "default", "always"]:
    """How the `target`'s synthesis pass of yosys `release` uses ABC9.

    "opt-in": only with `-abc9` (Xilinx); "default": unless `-noabc9` (the others); "always": it
    cannot be turned off, since yosys 0.69, which also dropped `-retime`. Read from the passes'
    sources of every release from xeda's minimum, 0.63, to 0.69.
    """
    if release >= (0, 69):
        return "always"
    return "opt-in" if target == "xilinx" else "default"


class YosysFpga(YosysBase, FpgaSynthFlow):
    """
    Yosys Open SYnthesis Suite: FPGA synthesis
    """

    minimum_yosys = MINIMUM_YOSYS

    results_description = describe_results(
        "LUT",
        "lut",
        "ff",
        **{
            "LUT:RAM": "Number of LUTs used as distributed RAM.",
            "FF": "Number of flip-flops (registers) used.",
        },
    )

    class Settings(YosysBase.Settings, FpgaSynthFlow.Settings):
        abc9: bool = Field(
            True,
            description="Map LUTs with ABC9. False maps with classic ABC (`-noabc9`, or no "
            "`-abc9` where ABC9 is opt-in); yosys 0.69 and newer require ABC9 unless iCE40 "
            "uses the separate `noabc` setting for built-in LUT mapping.",
        )
        flow3: bool = Field(
            True, description="Use flow3, which runs the mapping several times, if abc9 is set"
        )
        retime: bool = Field(
            False,
            description="Retime flip-flops with ABC (`-retime`). Removed in yosys 0.69.",
        )
        nobram: bool = Field(False, description="Do not map to block RAM cells")
        nodsp: bool = Field(False, description="Do not use DSP resources")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`)",
        )
        nowidelut: bool = Field(
            False,
            description="Disable wide LUT implementations using hard MUX resources. Leave false "
            "to use the target synthesis pass's normal mapping choices.",
        )
        abc_dff: bool = Field(False, description="Run abc/abc9 with -dff option")
        widemux: int = Field(
            0,
            description="enable inference of hard multiplexer resources for muxes at or above this number of inputs"
            " (minimum value 2, recommended value >= 5 or disabled = 0)",
        )

        @field_validator("widemux")
        @classmethod
        def _validate_widemux(cls, value: int) -> int:
            if value != 0 and value < 2:
                raise ValueError("widemux is 0 (off) or a mux size of at least 2")
            return value

        synth_flags: List[str] = Field(
            [],
            description="Extra flags appended verbatim to yosys' device-specific `synth_<family>` "
            "command, for options without a setting of their own.",
        )
        pre_synth_opt: bool = Field(
            False,
            description="run additional optimization steps before synthesis",
        )
        post_synth_opt: bool = Field(
            False,
            description="run additional optimization steps after synthesis if complete",
        )
        stop_after: Optional[Literal["rtl"]] = Field(
            None,
            description='Stop the flow after this stage. "rtl" elaborates the design and writes '
            "the RTL outputs without synthesizing.",
        )
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
            None, description="iCE40 timing model; inferred from fpga.device/type when unset."
        )

        def synthesis_target(self) -> str:
            """The yosys FPGA synthesis target: xilinx, gowin, ecp5, ice40 or nexus."""
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

        def synth_command(self, release: YosysRelease) -> List[str]:
            """The device synthesis command for yosys `release`, with its flags.

            Each setting becomes the flag the target's pass takes for it in that release (see
            `_abc9_mode`), and a combination that pass rejects is rejected here: a setting is
            honored or an error, never dropped. `synth_flags` follow verbatim.
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
            # unless told not to. An unset `flatten` leaves the pass's own choice.
            if target == "xilinx":
                if self.flatten:
                    command.append("-flatten")
            elif self.flatten is False:
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
                    f"ice40_device, ice40_dsp and ice40_spram are for iCE40 targets, not {name}."
                )
            return command + list(self.synth_flags)

        def _ice40_flags(self) -> List[str]:
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
                        "Cannot select synth_xilinx family from the Xilinx target: "
                        f"part={self.fpga.part!r}, device={self.fpga.device!r}, "
                        f"generation={generation or None!r}; set fpga.family or a recognized "
                        "generation (usp, u, or 7)."
                    )
                return []  # An unspecified Xilinx target uses synth_xilinx's Series 7 default.
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
                    "A generic Gowin target requires a device or part beginning with GW1N, "
                    "GW2A or GW5A."
                )
            else:
                chosen = inferred
            return chosen

    def run(self) -> None:
        """Synthesize the design for the selected FPGA target."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.fpga is not None, "checked at launch (`required_settings`)"
        self.artifacts.timing_report = ss.reports_dir / "timing.rpt"
        self.artifacts.utilization_report = ss.reports_dir / "utilization.json"
        synth_command = ss.synth_command(yosys_release(self.yosys))

        abc_constr_file = None
        if ss.abc_constr:
            abc_constr_file = "abc.constr"
            with open(abc_constr_file, "w") as f:
                f.write("\n".join(ss.abc_constr) + "\n")

        script_path = self.copy_from_template(
            f"yosys_fpga_synth{self.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            # `read_files.ys` lists the VHDL files and `-e <top>` itself, as for `yosys`: with
            # `one_shot_elab` the arguments carried every file a second time.
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=False),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            synth_command=synth_command,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = [self.script_flag, script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args.extend(["-L", ss.log_file])
        # With a log file, the console is left to the flow's own messages: the log has yosys's
        # output. `-T -Q` come with the tool's defaults already (`yosys`), and were given twice.
        if ss.log_file and not ss.verbose and not ss.debug:
            args.append("-q")
        self.yosys.run(*args)

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
                    self.results["LUT"] = sum_all_resources(
                        design_util, [f"LUT{i}" for i in range(2, 7)]
                    )
                    ram32m = sum_all_resources(design_util, ["RAM32M"])
                    if ram32m:
                        self.results["LUT"] += ram32m
                        self.results["LUT:RAM"] = ram32m
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

        # if self.settings.fpga:
        return True


def sum_all_resources(design_util: dict, lst: Iterable) -> int:
    return sum(int(design_util.get(t, 0)) for t in lst)
