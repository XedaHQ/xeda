import logging
import os
from pathlib import Path
import subprocess
from typing import List, Optional, Union
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlretrieve

from ...board import WithFpgaBoardSettings, get_board_data, get_board_file_path
from ...dataclass import Field, XedaBaseModel, validator
from ...flow import FlowFatalError, FpgaSynthFlow, FPGA
from ...tool import Tool
from ...utils import setting_flag
from ...design import SourceType
from ..yosys import YosysFpga

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


class EcpPLL(Tool):
    class Clock(XedaBaseModel):
        name: Optional[str] = None
        mhz: float = Field(gt=0, description="frequency in MHz")
        phase: Optional[float] = None

    executable: str = "ecppll"
    module: str = "ecp5pll"
    reset: bool = False
    standby: bool = False
    highres: bool = False
    internal_feedback: bool = False
    internal_feedback_wake: bool = False
    clkin: Union[Clock, float] = 25.0
    clkouts: List[Union[Clock, float]]
    file: Optional[str] = None

    @classmethod
    def _fix_clock(cls, clock: Clock, out_clk=None) -> Clock:
        if isinstance(clock, float):
            clock = cls.Clock(mhz=clock)
        if not clock.name:
            clock.name = "clk_i" if out_clk is None else f"clk_o_{out_clk}"
        return clock

    @validator("clkin", always=True)
    def _validate_clockin(cls, value):
        return cls._fix_clock(value)

    @validator("clkouts", always=True)
    def _validate_clockouts(cls, value):
        new_value = []
        for i, v in enumerate(value):
            new_value.append(cls._fix_clock(v, i))
        return new_value

    @validator("file", always=True)
    def _validate_outfile(cls, value, values):
        if not value:
            value = values["module"] + ".v"
        return value

    def generate(self):
        def s(f: float) -> str:
            return f"{f:0.03f}"

        args = setting_flag(self.module)
        args += setting_flag(self.file)
        args += ["--clkin_name", self.clkin.name]  # type: ignore # pylint: disable=no-member
        args += ["--clkin", s(self.clkin.mhz)]  # type: ignore # pylint: disable=no-member
        if not 1 <= len(self.clkouts) <= 4:
            raise ValueError("At least 1 and at most 4 output clocks can be specified")
        for i, clk in enumerate(self.clkouts):
            assert isinstance(clk, self.Clock) and clk.name
            args += [f"--clkout{i}_name", clk.name]
            args += [f"--clkout{i}", s(clk.mhz)]
            if clk.phase:
                if i > 0:
                    args += [f"--phase{i}", s(clk.phase)]
                else:
                    raise ValueError("First output clock cannot have a phase difference!")
        args += setting_flag(self.highres)
        args += setting_flag(self.standby)
        args += setting_flag(self.internal_feedback)
        self.run(*args)


class Nextpnr(FpgaSynthFlow):
    class Settings(WithFpgaBoardSettings):
        fpga: Optional[FPGA] = None
        verbose: bool = False
        lpf_cfg: Optional[str] = None
        seed: Optional[int] = None
        randomize_seed: bool = True
        timing_allow_fail: bool = False
        ignore_loops: bool = Field(
            False, description="ignore combinational loops in timing analysis"
        )

        textcfg: Optional[str] = None
        out_of_context: bool = Field(
            False,
            description="disable IO buffer insertion and global promotion/routing, for building pre-routed blocks",
        )
        lpf_allow_unconstrained: bool = Field(
            False,
            description="don't require LPF file(s) to constrain all IOs",
        )
        extra_args: List[str] = []
        py_script: Optional[str] = None
        write: Optional[str] = None
        sdf: Optional[str] = None
        log: Optional[str] = "nextpnr.log"
        report: Optional[str] = "report.json"
        detailed_timing_report: bool = False  # buggy and likely to segfault
        placed_svg: Optional[str] = None  # "placed.svg"
        routed_svg: Optional[str] = None  # "routed.svg"
        parallel_refine: bool = False
        nextpnr_flavor: Optional[str] = None
        # nextpnr-xilinx:
        chipdb: Union[Path, str, None] = Field(
            None,
            description="Xilinx: the path to the chip database, either the full binary path or the directory containing the database. If the value points to an existing directory, the binary file is automatically selected based on the FPGA part.",
        )
        placer: Optional[str] = Field(None, description="Placer algorithm to use")
        router: Optional[str] = Field(None, description="Router algorithm to use")
        placer_budgets: Optional[bool] = Field(
            None, description="Xilinx: use budget rather than criticality in placer timing"
        )
        xdc_files: List[Union[str, Path]] = Field(
            [], description="Xilinx: List of XDC constraint files."
        )
        fasm: Union[Path, str, None] = Field(None, description="Xilinx: FASM output file to write")
        bitstream: Union[Path, str, None] = Field(
            None, description="Xilinx: Bitstream file to write"
        )
        yosys: Optional[YosysFpga.Settings] = None

        @validator("yosys", always=True, pre=False)
        def _validate_yosys(cls, value, values):
            clocks = values.get("clocks")
            fpga = values.get("fpga")
            if value is None:
                value = YosysFpga.Settings(
                    fpga=fpga,
                    clocks=clocks,
                )  # type: ignore # pyright: reportGeneralTypeIssues=none
            else:
                if not isinstance(value, YosysFpga.Settings):
                    value = YosysFpga.Settings(**value)
                value.fpga = fpga
                value.clocks = clocks
            return value

        @validator("nextpnr_flavor", always=True)
        def _validate_nextpnr_flavor(cls, value, values):
            fpga = values.get("fpga")
            if value is None and fpga:
                if fpga.vendor in ("xilinx", "gowin", "fpga-interchange"):
                    value = fpga.vendor
                else:
                    value = fpga.family or "generic"
            return value

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.yosys is not None
        self.add_dependency(
            YosysFpga,
            ss.yosys,
        )

    def generate_bitstream(self, fasm_path: Path) -> Path | None:
        assert isinstance(self.settings, self.Settings)
        if self.settings.fpga is None:
            log.warning("FPGA settings not set!")
            return None
        family = self.settings.fpga.family
        assert self.settings.fpga.part, "FPGA part must be set!"
        assert family, "FPGA family must be set!"
        family = family.replace("-", "")
        PRJXRAY_DB_DIR = os.environ.get("PRJXRAY_DB_DIR")
        if not PRJXRAY_DB_DIR:
            log.warning("PRJXRAY_DB_DIR environment variable not set!")
            return None
        PRJXRAY_DB_DIR = Path(PRJXRAY_DB_DIR)

        assert fasm_path.exists(), f"FASM file {fasm_path} not found!"
        frames_path = fasm_path.with_suffix(".frames")
        subprocess.run(
            [
                "fasm2frames",
                "--part",
                self.settings.fpga.part,
                "--db-root",
                PRJXRAY_DB_DIR / family,
                fasm_path,
                frames_path,
            ],
            check=True,
        )
        assert frames_path.exists(), f"Frames file {frames_path} not found!"
        bitstream_path = Path(self.settings.bitstream or fasm_path.with_suffix(".bit"))
        self.settings.bitstream = bitstream_path.resolve()
        part_yaml = PRJXRAY_DB_DIR / family / self.settings.fpga.part / "part.yaml"
        assert part_yaml.exists(), f"Part YAML file {part_yaml} not found!"
        subprocess.run(
            [
                "xc7frames2bit",
                "--part_file",
                part_yaml,
                "--part_name",
                self.settings.fpga.part,
                "--frm_file",
                frames_path,
                "--output_file",
                bitstream_path,
            ],
            check=True,
        )
        return bitstream_path

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_flow = self.completed_dependencies[0]
        assert isinstance(yosys_flow, YosysFpga)
        assert isinstance(yosys_flow.settings, YosysFpga.Settings)
        assert yosys_flow.settings.netlist_json
        netlist_json = yosys_flow.run_path / yosys_flow.settings.netlist_json
        if not ss.nextpnr_flavor in {
            "generic",
            "ecp5",
            "ice40",
            "nexus",
            "gowin",
            "fpga-interchange",
            "xilinx",
        }:
            log.warning(f"Unsupported FPGA: {ss.nextpnr_flavor}")

        next_pnr = Tool(f"nextpnr-{ss.nextpnr_flavor}")
        if next_pnr.executable_path() is None:
            raise FlowFatalError(f"nextpnr-{ss.nextpnr_flavor} executable not found!")

        if not netlist_json.exists():
            raise FlowFatalError(f"netlist json file {netlist_json} does not exist!")

        lpf = ss.lpf_cfg
        board_data = get_board_data(ss.board)
        if not lpf and board_data and "lpf" in board_data:
            uri = board_data["lpf"]
            r = urlparse(uri)
            if r.scheme and r.netloc:
                try:
                    lpf, _ = urlretrieve(uri)
                except HTTPError as e:
                    log.critical(
                        "Unable to retrive file from %s (HTTP Error %d)",
                        uri,
                        e.code,
                    )
                    raise FlowFatalError("Unable to retreive LPF file") from None
            else:
                if "name" in board_data:
                    board_name = board_data["name"]
                else:
                    board_name = ss.board
                lpf = get_board_file_path(f"{board_name}.lpf")

        args = setting_flag(netlist_json, name="json")
        if ss.main_clock:
            args += setting_flag(ss.main_clock.freq_mhz, name="freq")
        args += setting_flag(lpf)
        if ss.nextpnr_flavor != "xilinx":
            args += setting_flag(self.design.rtl.top)
        args += setting_flag(ss.seed)
        args += setting_flag(ss.out_of_context)
        args += setting_flag(ss.lpf_allow_unconstrained)
        args += setting_flag(ss.debug)
        args += setting_flag(ss.verbose)
        args += setting_flag(ss.quiet)
        if ss.seed is None:
            args += setting_flag(ss.randomize_seed)
        args += setting_flag(ss.timing_allow_fail)
        args += setting_flag(ss.ignore_loops)
        args += setting_flag(ss.py_script, name="run")
        if ss.fpga:
            package = ss.fpga.package
            if ss.fpga.vendor and ss.fpga.vendor.lower() == "lattice":
                if package:
                    package = package.upper()
                    if package == "BG":
                        package = "CABGA"
                    elif package == "MG":
                        package = "CSFBGA"
                    assert ss.fpga.pins
                    package += str(ss.fpga.pins)
            if ss.nextpnr_flavor in ("ecp5", "ice40"):
                args += setting_flag(ss.fpga.speed)
                args += setting_flag(package)
                if ss.fpga.capacity:
                    device_type = ss.fpga.type
                    if device_type and device_type.lower() != "u":
                        args.append(f"--{device_type}-{ss.fpga.capacity}")
                    else:
                        args.append(f"--{ss.fpga.capacity}")
        if ss.nextpnr_flavor in ("ecp5", "ice40"):
            args += setting_flag(ss.textcfg)
            args += setting_flag(lpf)
        args += setting_flag(ss.write)
        args += setting_flag(ss.sdf)
        args += setting_flag(ss.log)
        if ss.nextpnr_flavor in ("xilinx",):
            args += setting_flag(ss.placer_budgets)
            if not ss.chipdb:
                raise FlowFatalError("Xilinx chip database (chipdb) is required!")
            if not isinstance(ss.chipdb, Path):
                ss.chipdb = Path(ss.chipdb)
            if ss.chipdb.is_dir() and ss.chipdb.exists():
                assert ss.fpga, "FPGA settings must be set!"
                assert ss.fpga.part
                f = next(ss.chipdb.glob(f"{ss.fpga.part}*.bin"), None)
                if not f:
                    part_no_speed = ss.fpga.part.split("-")[0]
                    f = next(ss.chipdb.glob(f"{part_no_speed}*.bin"), None)
                if not f:
                    raise FlowFatalError(
                        f"Xilinx chip database file for part {ss.fpga.part} not found in {ss.chipdb}!"
                    )
                ss.chipdb = f
            if not ss.chipdb.exists():
                raise FlowFatalError(f"Xilinx chip database file {ss.chipdb} does not exist!")
            args += setting_flag(ss.chipdb)

            xdc_files = list(
                p.file
                for p in self.design.rtl.sources
                if p.type is SourceType.Xdc or p.type is SourceType.Sdc
            )
            xdc_files += (self.normalize_path_to_design_root(p) for p in ss.xdc_files)
            other_constraints = ""
            for f in xdc_files:
                log.debug("Appending user XDC: %s", f)
                if not f.exists():
                    raise FlowFatalError(f"XDC file {f} does not exist!")
                with f.open() as xdc:
                    other_constraints += xdc.read() + "\n"

            xdc = self.copy_from_template("constraints.xdc", other_constraints=other_constraints)
            args += ["--xdc", xdc]
            if not ss.fasm:
                result_basename = self.design.rtl.top or "top"
                ss.fasm = result_basename + ".fasm"
            assert ss.fasm
            args += setting_flag(ss.fasm)

        if ss.nextpnr_flavor in ("ecp5", "ice40"):
            args += setting_flag(ss.nthreads, name="threads")
            args += setting_flag(ss.report)
            args += setting_flag(ss.placed_svg)
            args += setting_flag(ss.routed_svg)
            args += setting_flag(ss.detailed_timing_report)
            args += setting_flag(ss.parallel_refine)
        if ss.extra_args:
            args += ss.extra_args
        next_pnr.run(*args)
        if ss.log:
            log_path = Path(ss.log).resolve()
            if log_path.exists():
                log.info("Nextpnr log written to %s", log_path)
            else:
                log.warning("Nextpnr log file %s not found!", log_path)

        if ss.fasm:
            fasm_path = Path(ss.fasm)
            assert fasm_path.exists(), f"FASM file {ss.fasm} not found!"
            log.info("FASM file written to %s", fasm_path.resolve())
            bitstream_path = self.generate_bitstream(fasm_path)
            if bitstream_path and bitstream_path.exists():
                log.info("Bitstream file written to %s", bitstream_path.resolve())
                self.results["bitstream_path"] = bitstream_path.resolve()
            else:
                log.warning("Bitstream generation failed!")
