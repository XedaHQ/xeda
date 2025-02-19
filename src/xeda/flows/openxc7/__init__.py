import logging
import os
from pathlib import Path
import subprocess
from typing import List, Optional, Union

from ...board import WithFpgaBoardSettings, get_board_data
from ...dataclass import Field, validator
from ...flow import FlowFatalError, FpgaSynthFlow, FPGA
from ...tool import Tool
from ...utils import setting_flag
from ...design import SourceType
from ..yosys import YosysFpga

__all__ = ["OpenXC7"]

log = logging.getLogger(__name__)


class OpenXC7(FpgaSynthFlow):
    """
    OpenXC7: FPGA synthesis using nextpnr-xilinx
    """

    aliases = ["openxc7"]

    next_pnr = Tool("nextpnr-xilinx")
    ofpga_loader = Tool("openFPGALoader")

    class Settings(WithFpgaBoardSettings):
        fpga: Optional[FPGA] = None
        verbose: bool = False
        seed: Optional[int] = None
        randomize_seed: bool = True
        timing_allow_fail: bool = False
        ignore_loops: bool = Field(
            False, description="ignore combinational loops in timing analysis"
        )
        out_of_context: bool = Field(
            False,
            description="disable IO buffer insertion and global promotion/routing, for building pre-routed blocks",
        )
        extra_args: List[str] = []
        py_script: Optional[str] = None
        sdf: Optional[str] = None
        log: Optional[str] = "nextpnr.log"
        report: Optional[str] = "report.json"
        detailed_timing_report: bool = False  # buggy and likely to segfault
        placed_svg: Optional[str] = None  # "placed.svg"
        routed_svg: Optional[str] = None  # "routed.svg"
        parallel_refine: bool = False
        chipdb: Union[Path, str, None] = Field(
            None,
            description="Xilinx: the path to the chip database, either the full binary path or the directory containing the database. If the value points to an existing directory, the binary file is automatically selected based on the FPGA part.",
        )
        prjxray_db_dir: Union[Path, str, None] = Field(
            None,
            description="Xilinx: the path to the prjxray database directory. If not set, the PRJXRAY_DB_DIR environment variable is used.",
        )
        placer: Optional[str] = Field(None, description="Placer algorithm to use")
        router: Optional[str] = Field(None, description="Router algorithm to use")
        slack_redist_iter: Optional[int] = Field(
            None, description="Xilinx: number of iterations between slack redistribution"
        )
        cstrweight: Optional[str] = Field(
            None, description="Xilinx: placer weighting for relative constraint satisfaction"
        )
        starttemp: Optional[str] = Field(
            None, description="Xilinx: placer's simulated annealing (SA) initial temperature"
        )
        placer_budgets: Optional[bool] = Field(
            None, description="Xilinx: use budget rather than criticality in placer timing"
        )
        pack_only: bool = Field(
            False, description="Xilinx: only run packing, do not place and route"
        )
        ignore_loops: bool = Field(
            False, description="Xilinx: ignore combinational loops in timing analysis"
        )
        no_route: bool = Field(False, description="Xilinx: process design without routing")
        no_place: bool = Field(False, description="Xilinx: process design without placement")
        no_pack: bool = Field(False, description="Xilinx: process design without packing")
        check_db: bool = Field(False, description="Xilinx: check architecture database integrity")
        no_tmdriv: bool = Field(False, description="Xilinx: disable timing-driven placement")
        xdc_files: List[Union[str, Path]] = Field(
            [], description="Xilinx: List of XDC constraint files."
        )
        fasm_output: Union[Path, str, None] = Field(
            None, description="Xilinx: FASM output file to write"
        )
        json_output: Union[Path, str, None] = Field(
            None, description="Xilinx: JSON output file to write"
        )
        bitstream: Union[Path, str, None] = Field(
            None, description="Xilinx: Bitstream file to write"
        )
        yosys: Optional[YosysFpga.Settings] = None
        program: Union[str, bool, None] = Field(
            None,
            description="Program the FPGA after bitstream generation. Can specify the cable type.",
        )

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
            log.error("FPGA settings not set!")
            return None
        family = self.settings.fpga.family
        assert self.settings.fpga.part, "FPGA part must be set!"
        assert family, "FPGA family must be set!"
        family = family.replace("-", "")
        prjxraydb_dir = self.settings.prjxray_db_dir or os.environ.get("PRJXRAY_DB_DIR")
        if not prjxraydb_dir:
            log.error(
                "prjxray_db_dir settings was not specified and PRJXRAY_DB_DIR environment variable not set!"
            )
            return None
        prjxraydb_dir = Path(prjxraydb_dir)

        assert fasm_path.exists(), f"FASM file {fasm_path} not found!"
        frames_path = fasm_path.with_suffix(".frames")
        subprocess.run(
            [
                "fasm2frames",
                "--part",
                self.settings.fpga.part,
                "--db-root",
                prjxraydb_dir / family,
                fasm_path,
                frames_path,
            ],
            check=True,
        )
        assert frames_path.exists(), f"Frames file {frames_path} not found!"
        bitstream_path = Path(self.settings.bitstream or fasm_path.with_suffix(".bit"))
        self.settings.bitstream = bitstream_path.resolve()
        part_yaml = prjxraydb_dir / family / self.settings.fpga.part / "part.yaml"
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

        if self.next_pnr.executable_path() is None:
            raise FlowFatalError("nextpnr executable not found!")

        if not netlist_json.exists():
            raise FlowFatalError(f"netlist json file {netlist_json} does not exist!")
        board_data = get_board_data(ss.board)

        args = setting_flag(netlist_json, name="json")
        if ss.main_clock:
            args += setting_flag(ss.main_clock.freq_mhz, name="freq")
        args += setting_flag(ss.seed)
        args += setting_flag(ss.out_of_context)
        args += setting_flag(ss.debug)
        args += setting_flag(ss.verbose)
        args += setting_flag(ss.quiet)
        if ss.seed is None:
            args += setting_flag(ss.randomize_seed)
        args += setting_flag(ss.timing_allow_fail)
        args += setting_flag(ss.ignore_loops)
        args += setting_flag(ss.py_script, name="run")
        args += setting_flag(ss.check_db, name="test")
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
        args += setting_flag(ss.json_output, name="write")
        args += setting_flag(ss.sdf)
        args += setting_flag(ss.log)
        args += setting_flag(ss.placer_budgets)
        if not ss.chipdb:
            ss.chipdb = os.environ.get("CHIPDB_DIR")
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
        if not ss.fasm_output:
            result_basename = self.design.rtl.top or "top"
            ss.fasm_output = result_basename + ".fasm"
        assert ss.fasm_output
        args += setting_flag(ss.fasm_output, name="fasm")

        if ss.extra_args:
            args += ss.extra_args
        self.next_pnr.run(*args)
        if ss.log:
            log_path = Path(ss.log).resolve()
            if log_path.exists():
                log.info("Nextpnr log written to %s", log_path)
            else:
                log.warning("Nextpnr log file %s not found!", log_path)

        if ss.fasm_output:
            fasm_path = Path(ss.fasm_output)
            assert fasm_path.exists(), f"FASM file {ss.fasm_output} not found!"
            log.info("FASM file written to %s", fasm_path.resolve())
            bitstream_path = self.generate_bitstream(fasm_path)
            if bitstream_path and bitstream_path.exists():
                log.info("Bitstream file written to %s", bitstream_path.resolve())
                self.results["bitstream_path"] = bitstream_path.resolve()
            else:
                log.warning("Bitstream generation failed!")

        if ss.program or ss.board:
            cable = ss.program if isinstance(ss.program, str) else None

            args = ["--bitstream", bitstream_path]
            if cable:
                args.extend(["--cable", cable])
            elif ss.board:
                args.extend(["--board", ss.board])
            if ss.fpga and ss.fpga.part:
                args.extend(["--fpga-part", ss.fpga.part])
            # if ss.reset:
            #     args.append("--reset")
            if ss.verbose:
                args.append("--verbose")
            self.ofpga_loader.run(*args)
