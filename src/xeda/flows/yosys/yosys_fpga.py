import logging
from collections.abc import Iterable
from pathlib import Path
from typing import List, Literal, Optional

from ...dataclass import Field
from ...flow import FlowException, FpgaSynthFlow, describe_results
from ...flows.ghdl import GhdlSynth
from .common import YosysBase, append_flag, process_parameters

log = logging.getLogger(__name__)


class YosysFpga(YosysBase, FpgaSynthFlow):
    """
    Yosys Open SYnthesis Suite: FPGA synthesis
    """

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
            description="Use ABC9. Only iCE40 supports disabling it (`-noabc`); other FPGA "
            "synthesis passes require ABC9 and reject false.",
        )
        flow3: bool = Field(
            True, description="Use flow3, which runs the mapping several times, if abc9 is set"
        )
        retime: bool = Field(
            False,
            description="Reserved for compatibility; device synthesis passes do not accept "
            "a retiming flag, so enabling this raises an error.",
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
        synth_flags: List[str] = Field(
            [],
            description="Extra flags passed to yosys' device-specific `synth_<family>` command. "
            "Supported settings above are validated and converted to flags for the selected "
            "device pass; these extra flags allow newer tool options.",
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
        adder_map: Optional[str] = Field(
            None, description="Verilog file with device-specific adder cell mappings."
        )
        clockgate_map: Optional[str] = Field(
            None, description="Verilog file with device-specific clock-gating cell mappings."
        )
        other_maps: List[str] = Field(
            [], description="Additional Verilog files with device-specific cell mappings."
        )

        preserve_hierarchy: bool = Field(
            False,
            description="Preserve module hierarchy on FPGA families whose synthesis pass "
            "normally flattens it (iCE40, Lattice and Gowin).",
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

        def device_synth_flags(self) -> List[str]:
            """Flags supported by this target's synthesis pass, plus explicit custom flags."""
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            vendor = (self.fpga.vendor or "").lower()
            kind = (
                "xilinx"
                if vendor == "xilinx"
                else (
                    "gowin"
                    if vendor == "gowin" or family in {"gowin", "gw1n", "gw2a", "gw5a"}
                    else family
                )
            )
            if kind not in {"xilinx", "ecp5", "ice40", "nexus", "gowin"}:
                raise FlowException(f"No supported Yosys FPGA synthesis pass for {family!r}.")
            flags = list(self.synth_flags)
            if not self.abc9:
                if kind != "ice40":
                    raise FlowException(
                        f"synth_{kind} always uses ABC9; abc9=false is unsupported."
                    )
                append_flag(flags, "-noabc")
            if self.noabc and kind != "ice40":
                raise FlowException(f"synth_{kind} does not support noabc=true.")
            if self.widemux and kind != "xilinx":
                raise FlowException(f"synth_{kind} does not support widemux.")
            if kind in {"ecp5", "nexus", "gowin"}:
                # Lattice and Gowin synthesis already use ABC9 and flatten by default.
                if self.preserve_hierarchy:
                    append_flag(flags, "-noflatten")
                if self.nobram:
                    append_flag(flags, "-nobram")
                if self.nolutram:
                    append_flag(flags, "-nolutram")
                if self.nodsp:
                    append_flag(flags, "-nodsp")
                if self.nowidelut:
                    append_flag(flags, "-nowidelut")
            elif kind == "ice40":
                if self.nowidelut or self.nolutram:
                    raise FlowException("synth_ice40 has no nowidelut or nolutram option.")
                device = self.ice40_device
                if device is None:
                    name = (self.fpga.device or "").lower()
                    device = (
                        "u"
                        if name.startswith(("ice40up", "ice5lp"))
                        else ("lp" if name.startswith("ice40lp") else "hx")
                    )
                    if self.fpga.type:
                        device_type = self.fpga.type.lower()
                        if device_type not in {"hx", "lp", "up", "u"}:
                            raise FlowException(f"Unsupported iCE40 type {device_type!r}.")
                        device = (
                            "u"
                            if device_type in {"up", "u"}
                            else ("lp" if device_type == "lp" else "hx")
                        )
                append_flag(flags, f"-device {device}")
                if device != "u" and (self.ice40_dsp or self.ice40_spram):
                    raise FlowException(
                        "iCE40 DSP and SPRAM inference require an UltraPlus device."
                    )
                if self.ice40_dsp and self.nodsp:
                    raise FlowException("ice40_dsp and nodsp cannot both be true.")
                if self.preserve_hierarchy:
                    append_flag(flags, "-noflatten")
                if self.nobram:
                    append_flag(flags, "-nobram")
                if self.ice40_spram:
                    append_flag(flags, "-spram")
                if self.ice40_dsp and not self.nodsp:
                    append_flag(flags, "-dsp")
            else:
                if self.preserve_hierarchy and self.flatten:
                    raise FlowException("preserve_hierarchy and flatten cannot both be true.")
                if self.flatten:
                    append_flag(flags, "-flatten")
                for enabled, flag in (
                    (self.nobram, "-nobram"),
                    (self.nolutram, "-nolutram"),
                    (self.nodsp, "-nodsp"),
                    (self.nowidelut, "-nowidelut"),
                ):
                    if enabled:
                        append_flag(flags, flag)
                if self.widemux:
                    append_flag(flags, f"-widemux {self.widemux}")
            if self.abc_dff:
                if kind == "gowin":
                    raise FlowException("synth_gowin has no -dff option.")
                append_flag(flags, "-dff")
            if self.retime:
                raise FlowException(
                    "The selected FPGA synthesis pass has no -retime option; "
                    "use abc_dff or a custom staged synthesis script."
                )
            return flags

        def synth_command(self) -> str:
            """Select the installed Yosys family pass, including Gowin subfamilies."""
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            if (self.fpga.vendor or "").lower() == "xilinx":
                return "synth_xilinx"
            if family in {"gowin", "gw1n", "gw2a", "gw5a"}:
                return "synth_gowin"
            if family == "nexus" and (self.fpga.device or "").lower().startswith("lfd2nx"):
                return "synth_lattice"
            return f"synth_{family}"

        def synth_family_flags(self) -> List[str]:
            assert self.fpga is not None
            family = (self.fpga.family or "").lower()
            if self.synth_command() == "synth_xilinx":
                if not family:
                    return []  # synth_xilinx defaults to Series 7
                if family.endswith("-usp"):
                    target = "xcup"
                elif family.endswith("-us"):
                    target = "xcu"
                elif family.endswith("7"):
                    target = "xc7"
                else:
                    target = {
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
                    }.get(family, family)
                if target not in {
                    "xcup",
                    "xcu",
                    "xc7",
                    "xc6s",
                    "xc6v",
                    "xc5v",
                    "xc4v",
                    "xc3sda",
                    "xc3sa",
                    "xc3se",
                    "xc3s",
                    "xc2vp",
                    "xcve",
                    "xcv",
                }:
                    raise FlowException(f"Unsupported synth_xilinx family {family!r}.")
                return ["-family", target]
            if self.synth_command() == "synth_gowin" and family in {"gw1n", "gw2a", "gw5a"}:
                return ["-family", family]
            if self.synth_command() == "synth_lattice":
                return ["-family", "lfd2nx"]
            return []

    def run(self) -> None:
        """Synthesize the design for the selected FPGA target."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.fpga is not None, "checked at launch (`required_settings`)"
        assert ss.fpga.family or ss.fpga.vendor == "xilinx"
        self.artifacts.timing_report = ss.reports_dir / "timing.rpt"
        self.artifacts.utilization_report = ss.reports_dir / "utilization.json"
        synth_flags = ss.device_synth_flags()

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
            synth_flags=synth_flags,
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
