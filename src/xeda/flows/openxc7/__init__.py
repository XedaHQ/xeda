import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, List, Optional, Union

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
        log: Union[str, Path] = "nextpnr.log"
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
        program: Union[str, bool, Dict, None] = Field(
            None,
            description="Program the FPGA after bitstream generation. Can specify the cable type.",
        )

        @validator("yosys", always=True, pre=False)
        def _validate_yosys(cls, value, values):
            clocks = values.get("clocks")
            fpga = values.get("fpga")
            if value is None:
                # return None
                value = YosysFpga.Settings(
                    fpga=fpga,
                    clocks=clocks,
                )  # type: ignore # pyright: reportGeneralTypeIssues=none
            else:
                if not isinstance(value, YosysFpga.Settings):
                    value = YosysFpga.Settings(**value)
                if fpga:
                    value.fpga = fpga
                if clocks and not value.clocks:
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

    @classmethod
    def check_settings(cls, settings: Settings) -> None:
        def check_true(assertion: bool, message: str) -> None:
            if not assertion:
                raise FlowFatalError(message)
            assert assertion

        check_true(
            (settings.nthreads or 0) < 2,
            "Unsupported flow setting 'nthreads'. Multithreading is not supported.",
        )
        check_true(
            settings.fpga is not None, "Missing flow setting: 'fpga'. FPGA settings are required!"
        )
        check_true(settings.fpga.part is not None, "Missing flow setting: 'fpga.part'. FPGA part must be specified!")  # type: ignore
        check_true(settings.fpga.family is not None, "'fpga.family' was not automatically inferred. Please specify the family in the settings.")  # type: ignore

    def generate_bitstream(self, fasm_path: Path) -> Path | None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.fpga and ss.fpga.part and ss.fpga.family, "FPGA part must be set!"
        family = ss.fpga.family
        if family.lower() in ["artix-7", "xc7"]:
            family = "artix7"
        elif family.lower() in ["kintex-7", "k7"]:
            family = "kintex7"
        elif family.lower() in ["zynq-7", "z7"]:
            family = "zynq7"
        family = family.replace("-", "")
        prjxraydb_dir = ss.prjxray_db_dir or os.environ.get("PRJXRAY_DB_DIR")
        if not prjxraydb_dir:
            log.error(
                "prjxray_db_dir settings was not specified and PRJXRAY_DB_DIR environment variable not set!"
            )
            return None
        prjxraydb_dir = Path(prjxraydb_dir)

        assert fasm_path.exists(), f"FASM file {fasm_path} not found!"
        frames_path = fasm_path.with_suffix(".frames")
        cmd = [
            "fasm2frames",
            "--part",
            ss.fpga.part,
            "--db-root",
            prjxraydb_dir / family,
            fasm_path,
            frames_path,
        ]
        log.info("Running: %s", " ".join(map(str, cmd)))
        subprocess.run(
            cmd,
            check=True,
        )
        assert frames_path.exists(), f"Frames file {frames_path} not found!"
        bitstream_path = Path(ss.bitstream or fasm_path.with_suffix(".bit"))
        ss.bitstream = bitstream_path.resolve()
        part_yaml = prjxraydb_dir / family / ss.fpga.part / "part.yaml"
        assert part_yaml.exists(), f"Part YAML file {part_yaml} not found!"
        cmd = [
            "xc7frames2bit",
            "--part_file",
            part_yaml,
            "--part_name",
            ss.fpga.part,
            "--frm_file",
            frames_path,
            "--output_file",
            bitstream_path,
        ]
        log.info("Running: %s", " ".join(map(str, cmd)))
        subprocess.run(cmd, check=True)
        return bitstream_path

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_flow = self.completed_dependencies[0]
        assert isinstance(yosys_flow, YosysFpga)
        assert isinstance(yosys_flow.settings, YosysFpga.Settings)
        self.results["_yosys"] = yosys_flow.results
        assert yosys_flow.settings.netlist_json
        netlist_json = yosys_flow.run_path / yosys_flow.settings.netlist_json

        if not ss.fpga and ss.yosys and ss.yosys.fpga:
            ss.fpga = ss.yosys.fpga
        if not ss.clocks and ss.yosys and ss.yosys.clocks:
            ss.clocks = ss.yosys.clocks

        self.check_settings(ss)

        if self.next_pnr.executable_path() is None:
            raise FlowFatalError("nextpnr executable not found!")

        if not netlist_json.exists():
            raise FlowFatalError(f"netlist json file {netlist_json} does not exist!")
        board_data = get_board_data(ss.board)  # TODO

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
        if not ss.chipdb and self.design_root:
            ss.chipdb = self.design_root / "chipdb"
            if not ss.chipdb.exists():
                ss.chipdb.mkdir(parents=True)
        if not ss.chipdb:
            raise FlowFatalError("Xilinx chip database (chipdb) is required!")
        if not isinstance(ss.chipdb, Path):
            ss.chipdb = Path(ss.chipdb)
        if ss.chipdb.is_dir():
            assert ss.fpga, "FPGA settings must be set!"
            assert ss.fpga.part
            f = next(ss.chipdb.glob(f"{ss.fpga.part}*.bin"), None)
            if not f:
                part_no_speed = ss.fpga.part.split("-")[0]
                f = next(ss.chipdb.glob(f"{part_no_speed}*.bin"), None)
            if not f or not f.exists():
                nextpnr_xilinx_python_dir = os.environ.get("NEXTPNR_XILINX_PYTHON_DIR")
                if nextpnr_xilinx_python_dir:
                    nextpnr_xilinx_python_dir = Path(nextpnr_xilinx_python_dir)
                    f = self.generate_chipdb(nextpnr_xilinx_python_dir, ss.chipdb)
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
            if isinstance(ss.program, dict):
                programmer = ss.program.get("programmer")
                cable = ss.program.get("cable")
                board = ss.program.get("board")
                freq = ss.program.get("freq")
                reset = ss.program.get("reset", False)
                flash = ss.program.get("flash", False)
            else:
                programmer = "openFPGALoader"
                cable = ss.program if isinstance(ss.program, str) else None
                freq = None
                reset = False
                flash = False

            if programmer not in ["openFPGALoader"]:
                raise FlowFatalError(f"Unsupported programmer: {programmer}")

            if programmer == "openFPGALoader":
                args = ["--bitstream", bitstream_path]
                if cable:
                    args.extend(["--cable", cable])
                elif ss.board or board:
                    args.extend(["--board", ss.board or board])
                if ss.fpga and ss.fpga.part:
                    args.extend(["--fpga-part", ss.fpga.part])
                if reset:
                    args.append("--reset")
                if flash:
                    args.append("--flash")
                if freq:
                    args += ["--freq", freq]
                if ss.verbose:
                    args.append("--verbose")
                self.ofpga_loader.run(*args)

    def parse_reports(self):
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.log:
            log_path = Path(ss.log).resolve()
            if log_path.exists():
                log.info("Nextpnr log was found in %s", log_path)
                parsed_data = parse_nextpnr_logfile(log_path)
                if parsed_data:
                    self.results.update(**parsed_data)

                    util = parsed_data.get("_device_utilization", {})

                    def get_used(resource: str):
                        return util.get(resource, {}).get("used", 0)

                    self.results["LUT"] = get_used("SLICE_LUTX") or None
                    self.results["FF"] = get_used("SLICE_FFX") or None
                    self.results["RAMB18E1"] = get_used("RAMB18E1") or None
                    self.results["RAMB36E1"] = get_used("RAMB36E1") or None
                    self.results["RAMBFIFO36E1"] = get_used("RAMBFIFO36E1") or None
                    self.results["DSP48E1"] = get_used("DSP48E1") or None

                    f_max = None
                    for clock in parsed_data.get("_clocks", []):
                        clock_max_freq = clock.get("max_freq", 0)
                        if f_max is None or clock_max_freq > f_max:
                            f_max = clock_max_freq
                    if f_max:
                        self.results["f_max"] = f_max

            else:
                log.error("Nextpnr log file %s not found!", log_path)
                return False  # fail
        else:
            log.warning("Logging was disabled, so cannot analyse Nextpnr reports!")
            # still OK
        return True

    def generate_chipdb(
        self, nextpnr_xilinx_python_dir: Path, output_dir: Optional[Path], force=False
    ) -> Optional[Path]:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        python_executable = "pypy3.11"
        bbasm_executable = "bbasm"
        if output_dir is None:
            if ss.chipdb is not None:
                output_dir = Path(ss.chipdb).parent if os.path.isdir(ss.chipdb) else Path(ss.chipdb)
            else:
                output_dir = Path.cwd() / "chipdb"
        if not output_dir.exists():
            output_dir.mkdir(parents=True)
        if ss.fpga and ss.fpga.part:
            part = ss.fpga.part
            bin_path = output_dir / f"{part}.bin"
            if bin_path.exists():
                if not force:
                    log.info("Chip database already exists: %s", bin_path)
                    return bin_path
                log.info("Forcing regeneration of chip database: %s", bin_path)
                bin_path.unlink()
            bba_path = output_dir / f"{part}.bba"
            cmd = [
                python_executable,
                nextpnr_xilinx_python_dir / "bbaexport.py",
                "--device",
                part,
                "--bba",
                bba_path,
            ]
            log.info(f"Running: {' '.join(map(str,cmd))}")
            subprocess.run(cmd, check=True)
            assert bba_path.exists(), f"bbaexport failed: {bba_path} not found!"

            cmd = [bbasm_executable, "-l", bba_path, bin_path]
            log.info(f"Running: {' '.join(map(str,cmd))}")
            subprocess.run(cmd, check=True)
            assert (
                bin_path.exists() and bin_path.is_file()
            ), f"Failed to generate chipdb: {bin_path} not found!"
            bba_path.unlink()
            log.info("Chip database generated: %s", bin_path)
            return bin_path
        return None


def parse_nextpnr_logfile(log_path: Path) -> Dict:
    """
    Parse the output log of nextpnr and return a hierarchical dictionary
    """

    # Read the entire file as lines
    with log_path.open("r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    # Prepare the data structures to hold results
    device_utilization = {}
    critical_path_reports = []
    clocks_reports = []

    # Helper functions find the section(s) between two regex patterns
    # end_section_re is NOT part of the section, it's the first line after
    def find_section_bounds(lines_list, start_section_re, end_section_re, re_flags=re.IGNORECASE):
        start_line = None
        end_line = None
        for idx, text in enumerate(lines_list):
            if re.search(start_section_re, text, re_flags):
                start_line = idx
            elif start_line is not None and re.search(end_section_re, text, re_flags):
                end_line = idx
                break
        return start_line, end_line

    def find_all_sections(
        lines, start_section_re, end_section_re=r"^\s*$", re_flags=re.IGNORECASE
    ) -> list[list[str]]:

        sections = []
        # fist occurence
        while True:
            start_line, end_line = find_section_bounds(
                lines, start_section_re, end_section_re, re_flags
            )
            if start_line is None:
                break
            if end_line is None:
                sections.append(lines[start_line:])
                break
            sections.append(lines[start_line:end_line])
            lines = lines[end_line:]

        return sections

    def find_section(lines_list, start_section_re, end_section_re=r"^\s*$", re_flags=re.IGNORECASE):
        start_line, end_line = find_section_bounds(
            lines_list, start_section_re, end_section_re, re_flags
        )
        if start_line is not None:
            if end_line is None:
                end_line = -1
            return lines[start_line:end_line]
        return None

    def try_convert(value: str, typ: type):
        try:
            return typ(value)
        except ValueError:
            return value

    ## device utilization
    device_util_re = re.compile(
        r"^\s*(\w+:)?\s*(?P<resource>[^:]+?)\s*:\s*"
        r"(?P<used>\d+)\s*/\s*(?P<available>\d+)\s+"
        r"(?P<percentage>\d[\d\.]*)\s*%"
    )

    section = find_section(lines, r"Device utili.ation:")
    if section is not None:
        for line in section:
            match = device_util_re.match(line)
            if match:
                res_name = match.group("resource").strip()
                used_val = try_convert(match.group("used"), int)
                avail_val = try_convert(match.group("available"), int)
                perc_val = try_convert(match.group("percentage"), float)

                device_utilization[res_name] = {
                    "used": used_val,
                    "available": avail_val,
                    "percentage": perc_val,
                }

    with log_path.open("r", encoding="utf-8") as f:
        full_text = f.read()

    clock_regex = re.compile(
        r"frequency for clock\s*'(?P<clock_name>\S+)':\s*(?P<max_freq>\d[\d\.]*)\s*(?P<max_freq_unit>\w+)\s*\(?(?P<status>\w+)\s+at\s*(?P<requested_freq>\d[\d\.]*)\s*(?P<requested_freq_unit>\w+)\s*\)?"
    )
    for match in clock_regex.finditer(full_text):
        clocks_reports.append(
            {
                "clock_name": match.group("clock_name"),
                "max_freq": try_convert(match.group("max_freq"), float),
                "max_freq_unit": match.group("max_freq_unit"),
                "status": match.group("status"),
                "requested_freq": try_convert(match.group("requested_freq"), float),
                "requested_freq_unit": match.group("requested_freq_unit"),
            }
        )

    cp_re = r"Critical path report for clock"
    critical_path_re = re.compile(
        cp_re + r"\s+'(?P<clock_name>\S+)'\s*"
        r"\(\s*(?P<from_edge>\S+)\s*->\s*(?P<to_edge>\S+)\s*\)"
    )
    # process all matches of:
    # Info:  0.1  0.1  Source $auto$ff.cc:266:slice$5358.Q
    # Info:  0.3  0.4    Net breath_effect_inst.counter[0] budget 0.320000 ns (33,125) -> (33,125)
    # Info:                Sink $abc$32698$lut$not$aiger32697$13.A1
    path_re = re.compile(
        r"\s*(\w+:)?\s*(?P<delay>\d+(\.\d+)?)\s+(?P<slack>\d+(\.\d+)?)\s*Source\s+(?P<source>\S+)\s*"
        r"\s*(\w+:)?\s+(?P<net_delay>\d+(\.\d+)?)\s+(?P<slack_delay>\d+(\.\d+)?)\s*Net\s+(?P<net>\S+)\s*budget\s+(?P<budget>\d+(\.\d+)?)\s*ns\s*\((?P<from>\d+,\d+)\)\s*->\s*\((?P<to>\d+,\d+)\)"
    )
    delay_breakdown_re = re.compile(
        r"\s*(\w+:)?\s*(?P<logic_delay>\d+(\.\d+)?)\s*(?P<logic_delay_unit>\w+)\s+logic,?\s*(?P<routing_delay>\d+(\.\d+)?)\s*(?P<routing_delay_unit>\w+)\s*"
    )

    for cp_section in find_all_sections(lines, cp_re):
        match = critical_path_re.search(cp_section[0])
        if match:
            crit_path = {
                "clock_name": match.group("clock_name"),
                "from_edge": match.group("from_edge"),
                "to_edge": match.group("to_edge"),
            }

            paths = []
            secs = "\n".join(cp_section[2:])
            for match in path_re.finditer(secs, re.MULTILINE | re.DOTALL):
                paths.append(
                    {
                        "delay": try_convert(match.group("delay"), float),
                        "slack": try_convert(match.group("slack"), float),
                        "source": match.group("source"),
                        "net_delay": try_convert(match.group("net_delay"), float),
                        "slack_delay": try_convert(match.group("slack_delay"), float),
                        "net": match.group("net"),
                        "budget": try_convert(match.group("budget"), float),
                        "from": match.group("from"),
                        "to": match.group("to"),
                    }
                )
            crit_path["paths"] = paths
            match = delay_breakdown_re.search(cp_section[-1])
            if match:
                crit_path["logic_delay"] = try_convert(match.group("logic_delay"), float)
                crit_path["logic_delay_unit"] = match.group("logic_delay_unit")
                crit_path["routing_delay"] = try_convert(match.group("routing_delay"), float)
                crit_path["routing_delay_unit"] = match.group("routing_delay_unit")

            critical_path_reports.append(crit_path)
    return {
        "_device_utilization": device_utilization,
        "_clocks": clocks_reports,
        "_critical_path_reports": critical_path_reports,
    }
