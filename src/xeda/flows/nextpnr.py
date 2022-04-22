import logging
from typing import Optional, List, Union
from urllib.request import urlretrieve
from urllib.parse import urlparse
from urllib.error import HTTPError

from .yosys import Yosys
from .flow import FlowFatalError, FpgaSynthFlow
from ..tool import Tool
from ..utils import setting_flag
from ..dataclass import Field, XedaBaseModel, validator
from ..board import WithFpgaBoardSettings, get_board_file_path, get_board_data

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


class EcpPLL(Tool):
    class Clock(XedaBaseModel):
        name: Optional[str] = None
        mhz: float = Field(gt=0, description="frequency in MHz")
        phase: Optional[float] = None

    executable = "ecppll"
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
    def _fix_clock(cls, clock, out_clk=None) -> Clock:
        if isinstance(clock, float):
            clock = cls.Clock(mhz=clock)
        if not clock.name:
            clock.name = "clk_i" if out_clk is None else f"clk_o_{out_clk}"
        return clock

    @validator("clkin", always=True)  # type: ignore
    def _validate_clockin(cls, value):
        return cls._fix_clock(value)

    @validator("clkouts", always=True)  # type: ignore
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
                    raise ValueError(
                        "First output clock cannot have a phase difference!"
                    )
        args += setting_flag(self.highres)
        args += setting_flag(self.standby)
        args += setting_flag(self.internal_feedback)
        self.run(*args)


class Nextpnr(FpgaSynthFlow):
    class Settings(WithFpgaBoardSettings):
        verbose: bool = False
        lpf_cfg: Optional[str] = None
        seed: Optional[int] = None
        randomize_seed: bool = False
        timing_allow_fail: bool = False
        ignore_loops: bool = Field(
            False, description="ignore combinational loops in timing analysis"
        )

        textcfg: Optional[str] = "config.txt"
        out_of_context: bool = Field(
            False,
            description="disable IO buffer insertion and global promotion/routing, for building pre-routed blocks",
        )
        lpf_allow_unconstrained = Field(
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
        yosys: Optional[Yosys.Settings] = None

        @validator("yosys", always=True, pre=False)
        def _validate_yosys(cls, value, values):
            clocks = values.get("clocks")
            fpga = values.get("fpga")
            if value is None:
                value = Yosys.Settings(
                    fpga=fpga,
                    clocks=clocks,
                )  # pyright: reportGeneralTypeIssues=none
            else:
                if not isinstance(value, Yosys.Settings):
                    value = Yosys.Settings(**value)
                value.fpga = fpga
                value.clocks = clocks
            return value

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.yosys is not None
        self.add_dependency(
            Yosys,
            ss.yosys,
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_flow = self.completed_dependencies[0]
        assert isinstance(yosys_flow, Yosys)
        assert isinstance(yosys_flow.settings, Yosys.Settings)
        assert yosys_flow.settings.netlist_json
        netlist_json = yosys_flow.run_path / yosys_flow.settings.netlist_json
        fpga_family = ss.fpga.family if ss.fpga.family else "generic"
        assert fpga_family in {
            "generic",
            "ecp5",
            "ice40",
            "nexus",
            "gowin",
            "fpga-interchange",
            "xilinx",
        }, "unsupported fpga family"
        next_pnr = Tool(f"nextpnr-{fpga_family}")

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
        args += setting_flag(ss.clock_period and (1000 / ss.clock_period), name="freq")
        args += setting_flag(lpf)
        args += setting_flag(self.design.rtl.top)
        args += setting_flag(ss.seed)
        args += setting_flag(ss.fpga.speed)
        if ss.fpga.capacity:
            device_type = ss.fpga.type
            if device_type and device_type.lower() != "u":
                args.append(f"--{device_type}-{ss.fpga.capacity}")
            else:
                args.append(f"--{ss.fpga.capacity}")
        args += setting_flag(ss.out_of_context)
        args += setting_flag(ss.lpf_allow_unconstrained)
        args += setting_flag(ss.debug)
        args += setting_flag(ss.verbose)
        args += setting_flag(ss.quiet)
        args += setting_flag(ss.randomize_seed)
        args += setting_flag(ss.timing_allow_fail)
        args += setting_flag(ss.ignore_loops)
        args += setting_flag(ss.py_script, name="run")
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
        args += setting_flag(package)
        args += setting_flag(lpf)
        args += setting_flag(ss.textcfg)
        args += setting_flag(ss.write)
        args += setting_flag(ss.nthreads, name="threads")
        args += setting_flag(ss.sdf)
        args += setting_flag(ss.log)
        args += setting_flag(ss.report)
        args += setting_flag(ss.placed_svg)
        args += setting_flag(ss.routed_svg)
        args += setting_flag(ss.detailed_timing_report)
        args += setting_flag(ss.parallel_refine)
        if ss.extra_args:
            args += ss.extra_args
        next_pnr.run(*args)
