import logging
import os
from typing import Optional, List, Union
from urllib.request import urlretrieve
from urllib.parse import urlparse
from urllib.error import HTTPError

from .yosys import Yosys
from .flow import FlowFatalError, FpgaSynthFlow
from ..tool import Tool
from ..dataclass import Field
from ..board import WithFpgaBoardSettings, get_board_file_path, get_board_data

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


class Nextpnr(FpgaSynthFlow):
    class Settings(WithFpgaBoardSettings):
        lpf_cfg: Optional[str] = None
        clock_period: float
        seed: Optional[int] = None
        random_seed: bool = False
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
        extra_args: Optional[List[str]] = None
        py_script: Union[None, str, os.PathLike] = None
        out_json: Optional[str] = None
        sdf: Optional[str] = None
        log_to_file: Optional[str] = None
        report: Optional[str] = "report.json"
        detailed_timing_report: bool = True
        placed_svg: Optional[str] = "placed.svg"
        routed_svg: Optional[str] = "routed.svg"

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.add_dependency(
            Yosys,
            Yosys.Settings(
                fpga=ss.fpga,
                clock_period=ss.clock_period,
            ),  # pyright: reportGeneralTypeIssues=none
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

        assert (
            netlist_json.exists()
        ), f"netlist json file {netlist_json} does not exist!"

        lpf_cfg_path = ss.lpf_cfg
        board_data = get_board_data(ss.board)
        if not lpf_cfg_path and board_data and "lpf" in board_data:
            lpf = board_data["lpf"]
            r = urlparse(lpf)
            if r.scheme and r.netloc:
                try:
                    lpf_cfg_path, _ = urlretrieve(lpf)
                except HTTPError as e:
                    log.critical(
                        "Unable to retrive file from %s (HTTP Error %d)",
                        lpf,
                        e.code,
                    )
                    raise FlowFatalError("Unable to retreive LPF file") from None
            else:
                if "name" in board_data:
                    board_name = board_data["name"]
                else:
                    board_name = ss.board
                lpf_cfg_path = get_board_file_path(f"{board_name}.lpf")
        assert lpf_cfg_path

        freq_mhz = 1000 / ss.clock_period

        args = [
            "-q",
            "--json",
            netlist_json,
            "--freq",
            freq_mhz,
        ]
        if self.design.rtl.top:
            args.extend(["--top", self.design.rtl.top])
        if ss.seed is not None:
            args.extend(["--seed", ss.seed])

        package = ss.fpga.package
        speed = ss.fpga.speed
        device_type = ss.fpga.type
        if speed:
            args.extend(["--speed", speed])
        if ss.fpga.vendor and ss.fpga.vendor.lower() == "lattice":
            if package:
                package = package.upper()
                if package == "BG":
                    package = "CABGA"
                elif package == "MG":
                    package = "CSFBGA"
                assert ss.fpga.pins
                package += str(ss.fpga.pins)
        if ss.fpga.capacity:
            if device_type and device_type.lower() != "u":
                args.append(f"--{device_type}-{ss.fpga.capacity}")
            else:
                args.append(f"--{ss.fpga.capacity}")
        if ss.out_of_context:
            args.append("--out-of-context")
        if ss.lpf_allow_unconstrained:
            args.append("--lpf-allow-unconstrained")
        if ss.debug:
            args.append("--debug")
        if ss.verbose:
            args.append("--verbose")
        if ss.quiet:
            args.append("--quiet")
        if ss.random_seed:
            args.append("--randomize-seed")
        if ss.timing_allow_fail:
            args.append("--timing-allow-fail")
        if ss.ignore_loops:
            args.append("--ignore-loops")
        if ss.py_script:
            args += ["--run", ss.py_script]
        if package:
            args += ["--package", package]
        if lpf_cfg_path:
            # FIXME check what to do if no board
            args += ["--lpf", lpf_cfg_path]
        if ss.textcfg:
            args += ["--textcfg", ss.textcfg]
        if ss.out_json:
            args += ["--write", ss.out_json]
        if ss.nthreads:
            args += ["--threads", ss.nthreads]
        if ss.sdf:
            args += ["--sdf", ss.sdf]
        if ss.log_to_file:
            args += ["--log", ss.log_to_file]
        if ss.report:
            args += ["--report", ss.report]
        if ss.placed_svg:
            args += ["--placed-svg", ss.placed_svg]
        if ss.routed_svg:
            args += ["--routed-svg", ss.routed_svg]
        if ss.detailed_timing_report:
            args.append("--detailed-timing-report")
        if ss.extra_args:
            args += ss.extra_args
        next_pnr.run(*args)
