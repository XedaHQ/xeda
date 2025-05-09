import logging
import os
from pathlib import Path
from typing import List, Literal, Optional, Union

from colorama import Fore as fg
from colorama import Style as style

from ..dataclass import Field
from ..design import SourceType
from ..flow import FlowSettingsException, SimFlow
from ..tool import Tool

log = logging.getLogger(__name__)


class Vcs(SimFlow):
    """Synopsys VCS simulator"""

    highlight_rules = {
        r"^(Error:)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\*+ERROR\*+)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(Error-\s*\[\s*\w+\s*\])(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\s*SDF Error:)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\s*Total errors:)(\s+[1-9]\d*\s*.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(Lint-\[\w+\])(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(Warning:)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(\*+WARN\*+)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(.WARN)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(Warning-\s*\[\s*\w+\s*\])(.+)$": fg.YELLOW
        + style.BRIGHT
        + r"\g<1>"
        + style.NORMAL
        + r"\g<2>",
        r"^(Information[:-])(.+)$": fg.GREEN + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
    }

    vlogan = Tool("vlogan", version_flag=None, highlight_rules=highlight_rules)
    vhdlan = Tool("vhdlan", version_flag=None, highlight_rules=highlight_rules)
    vcs = Tool("vcs", highlight_rules=highlight_rules)
    fsdb2vcd = Tool("fsdb2vcd", version_flag=None, highlight_rules=highlight_rules)
    vpd2vcd = Tool(
        "vpd2vcd",
        default_args=["-full64", "-q"],
        version_flag=None,
        highlight_rules=highlight_rules,
    )
    simv = Tool("./simv", version_flag=None)

    class Settings(SimFlow.Settings):
        clean: bool = Field(True, description="Clean the run path before running")
        simv_flags: List[str] = []
        work_dir: Optional[str] = "work"
        sim_no_save: bool = True
        generate_kdb: bool = False
        supress_banner: bool = True
        quiet: bool = False
        vhdl_xlrm: bool = Field(
            False, description="Enables VHDL features beyond those described in LRM"
        )
        one_shot_run: bool = Field(
            False,
            description="Run the simulation using the vcs step. If set to true, simv_flags will be ignored.",
        )
        ucli: bool = Field(
            False,
            description="Enable UCLI (Universal Command Line Interface) when running simulator executable (simv)",
        )
        ucli_script: Optional[Path] = Field(
            None,
            description="Run this UCLI script executing the simulator (simv)",
        )
        gui: Optional[Union[bool, str]] = Field(
            None,
            description="Enable GUI (Graphical User Interface) when running simulator executable. A string can be used to specify the GUI type, either 'dve' or 'verdi', otherwise VCS will start verdi if VC_HOME is set.",
        )
        full64: bool = True
        time_unit: Optional[str] = "1ns"
        time_resolution: Optional[str] = "1ps"
        sdf_file: Optional[Union[str, Path]] = Field(
            None, description="SDF file for back-annotating delays using the unified SDF feature"
        )
        sdf_instance: Optional[str] = Field(
            None,
            description="Hierarchy instance to use as the root path for back-annotating delays. Use dot to separate hierarchy levels, e.g.: TB_NAME.UUT_NAME. This setting is required when sdf_file is provided.",
        )
        sdf_type: Literal["min", "typ", "max"] = Field(
            "typ", description="SDF type for back-annotating delays"
        )
        vcs_warn: Optional[str] = "all,noTFIPC,noLCA_FEATURES_ENABLED"
        vlogan_warns: Optional[str] = None
        vcs_nowarn: List[str] = []
        lint: Optional[str] = "all,TFIPC-L,noVCDE,noTFIPC,noIWU,noOUDPE,noUI"
        debug_access: Optional[Union[str, bool]] = True
        debug_region: Optional[str] = None
        functional_vital: bool = Field(False, description="Disable timing simulation for VITAL")
        init_std_logic: Optional[Literal["U", "X", "0", "1", "Z", "W", "L", "H", "-"]] = Field(
            None, description="Initialize std_logic to this value"
        )
        initreg: Optional[str] = Field(
            None,
            description="Initialize registers to this value. Use 'random' for random initialization.",
        )
        lic_wait: Optional[int] = Field(100, description="Wait for license if not available.")
        vlogan_flags: List[str] = ["+v2k"]
        vhdlan_flags: List[str] = []
        vcs_flags: List[str] = []
        cflags: List[str] = ["-O3", "-march=native", "-mtune=native"]
        vcs_log_file: Optional[str] = "vcs.log"
        fsdb: Optional[Path] = Field(
            None,
            description="Enable FSDB (Fast Signal DataBase) for waveform generation",
        )
        fsdb_size_limit: Optional[int] = Field(
            256,
            description="Set the FSDB size limit in MB. If not set, the default value will be used.",
        )
        to_vcd: bool = Field(
            False,
            description="Convert FSDB/VPD to VCD (Value Change Dump) format after simulation. The VCD file will be saved in the same directory as the FSDB file and with the same stem (base name), but with a .vcd extension.",
        )
        vpd: Optional[Path] = Field(
            None,
            description="Enable VPD waveform generation and specify the file path.",
        )
        evcd: Optional[Path] = Field(
            None,
            description="Enable EVCD waveform generation and specify the file path.",
        )
        vpd_size_limit: Optional[int] = Field(
            None,
            description="Set the VPD size limit in MB. When the limit is reached, the simulation overwrites older history",
        )

    def clean(self):
        super().purge_run_path()

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.sdf_file:
            if ss.sdf_instance is None:
                raise FlowSettingsException("SDF instance is required when SDF file is provided")
            ss.sdf_file = self.process_path(ss.sdf_file, resolve_to=self.design.design_root)
        if not ss.ucli or (ss.ucli_script is None):  # non-interactive
            self.simv.highlight_rules = self.highlight_rules
        if ss.ucli_script:
            ss.ucli = True
            ss.ucli_script = self.process_path(ss.ucli_script, resolve_to=self.design.design_root)
        elif ss.fsdb:
            ss.fsdb = self.process_path(ss.fsdb, resolve_to=self.design.design_root)
            ss.ucli = True
            ss.ucli_script = Path("dump_fsdb.do")
        elif ss.vpd:
            ss.vpd = self.process_path(ss.vpd, resolve_to=self.design.design_root)
            ss.ucli = True
            ss.ucli_script = Path("dump_vpd.do")
        elif ss.evcd:
            ss.evcd = self.process_path(ss.evcd, resolve_to=self.design.design_root)
            ss.ucli = True
            ss.ucli_script = Path("dump_evcd.do")

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if not os.environ.get("VCS_TARGET_ARCH"):
            os.environ["VCS_TARGET_ARCH"] = "amd64"
        if not os.environ.get("VCS_ARCH_OVERRIDE"):
            os.environ["VCS_ARCH_OVERRIDE"] = "linux"
        vlogan_args = ss.vlogan_flags
        vhdlan_args = ss.vhdlan_flags
        vcs_args = ss.vcs_flags
        simv_args = ss.simv_flags
        common_run_args: List[str] = []

        if ss.fsdb or ss.vpd or ss.evcd:
            assert ss.ucli_script
            with open(ss.ucli_script, "w", encoding="utf-8") as f:
                if ss.fsdb:
                    f.write(f"dump -file {ss.fsdb} -type FSDB\n")
                    f.write(f"dump -add . -add / -aggregates -fid FSDB0\n")
                    f.write(f"dump -enable -fid FSDB0\n")
                elif ss.vpd:
                    f.write(f"dump -file {ss.vpd} -type vpd\n")
                    f.write(f"dump -add . -add / -aggregates -fid VPD0\n")
                    f.write(f"dump -enable -fid VPD0\n")
                elif ss.evcd:
                    f.write(f"dump -file {ss.evcd} -type evcd\n")
                    f.write(f"dump -add . -add / -aggregates -fid EVCD0\n")
                    f.write(f"dump -enable -fid EVCD0\n")
                if ss.stop_time is not None:
                    f.write(f"run -absolute {ss.stop_time}\n")
                else:
                    f.write(f"run\n")
                f.write(f"quit\n")
        if ss.gui:
            ss.generate_kdb = True
        if ss.supress_banner:
            vlogan_args.append("-nc")
            vhdlan_args.append("-nc")
            vcs_args.append("-nc")
            simv_args.append("-nc")
        if ss.quiet:
            vlogan_args.append("-q")
            vhdlan_args.append("-q")
            vcs_args.append("-q")
        if ss.full64:
            vlogan_args.append("-full64")
            vhdlan_args.append("-full64")
            vcs_args.append("-full64")
        if ss.generate_kdb:
            vlogan_args.append("-kdb")
            vhdlan_args.append("-kdb")
            vcs_args.append("-kdb")
        if ss.lic_wait is not None:
            vhdlan_args += ["-licw", str(ss.lic_wait)]
            vcs_args += ["-vc_lic_wait", str(ss.lic_wait)]
            # simv_args.append("+vcs+lic+wait")
        if ss.cflags:
            vcs_args += ["-CFLAGS", " ".join(ss.cflags)]
        # vlogan_args.extend(["-work", "WORK"])
        # vhdlan_args.extend(["-work", "WORK"])
        vlogan_args.append(f"-timescale={ss.time_unit}/{ss.time_resolution}")
        if ss.vhdl_xlrm:
            vhdlan_args.append("-xlrm")
        if ss.functional_vital:
            vhdlan_args.append("-functional_vital")
        incdirs: List[str] = []
        for d in incdirs:
            vlogan_args.append(f"+incdir+{d}")
        if self.design.language.vhdl.standard in ("08", "2008"):
            vhdlan_args.append("-vhdl08")
        elif self.design.language.vhdl.standard in ("02", "2002"):
            vhdlan_args.append("-vhdl02")
        if ss.vlogan_warns:
            vlogan_args.append(f"+warn={ss.vlogan_warns}")
        if ss.init_std_logic:
            vhdlan_args += ["-init_std_logic", str(ss.init_std_logic)]
        vlog_files = self.design.sources_of_type(SourceType.Verilog, rtl=True, tb=True)
        if vlog_files:
            log.info("analyzing Verilog files")
            self.vlogan.run(*vlogan_args, *(str(f) for f in vlog_files))
        sv_files = self.design.sources_of_type(SourceType.SystemVerilog, rtl=True, tb=True)
        if sv_files:
            log.info("analyzing SystemVerilog files")
            self.vlogan.run(*vlogan_args, "-sverilog", *(str(f) for f in sv_files))
        vhdl_files = self.design.sources_of_type(SourceType.Vhdl, rtl=True, tb=True)
        if vhdl_files:
            log.info("analyzing VHDL files")
            self.vhdlan.run(*vhdlan_args, *(str(f) for f in vhdl_files))
        top = self.design.tb.top[0]
        if ss.verbose:
            vlogan_args.append("-notice")
            vhdlan_args.append("-verbose")
            vcs_args.append("-notice")
        if ss.lint:
            vcs_args.append(f"+lint={ss.lint}")
        if ss.vcs_warn:
            warns = ss.vcs_warn.split(",")
            warns += [f"no{w}" for w in ss.vcs_nowarn]
            vcs_args.append(f"+warn={','.join(warns)}")
        if ss.debug_region is not None:
            vcs_args.append(f"-debug_region={ss.debug_region}")
        if ss.debug_access:
            if isinstance(ss.debug_access, str):
                if ss.debug_access[0] not in ("+", "-", "="):
                    if ss.debug_access == "all":
                        ss.debug_access = "+all"
                    else:
                        raise FlowSettingsException(
                            "debug_access option must start with '+', '-', or '='"
                        )
                vcs_args.append(f"-debug_access{ss.debug_access}")
            else:
                vcs_args.append("-debug_access")
            vcs_args.append("+vcs+dumpvars")
        if ss.time_resolution:
            vcs_args.append(f"-sim_res={ss.time_resolution}")

        if ss.fsdb:
            vcs_args += [
                "+vcs+fsdbon",
            ]
            if ss.fsdb_size_limit is not None:
                simv_args.append(f"+fsdb+dump_limit={ss.fsdb_size_limit}")
            # simv_args += [
            #     f"+fsdbfile+{ss.fsdb}",
            # ]
        # simv_args += ["+dumpports+portdir"]

        if ss.sdf_file and ss.sdf_instance:
            vcs_args += [
                "-sdf",
                f"{ss.sdf_type}:{ss.sdf_instance}:{ss.sdf_file}",
            ]
            vcs_args.append("+sdfverbose")

        if self.design.tb.parameters:
            gfile = f"{top}.params"
            with open(gfile, "w", encoding="utf-8") as f:
                for k, v in self.design.tb.parameters.items():
                    # kv = f"/{top}/{k}={v}"
                    if v is None:
                        continue
                    elif isinstance(v, bool):
                        v = "1" if v else "0"
                    elif isinstance(v, str):
                        v = f'"{v}"'
                    f.write(f"assign {v} {k}\n")
            vcs_args += ["-lca", "-gfile", gfile]
        if ss.nthreads is not None:
            vcs_args.append(f"-j{ss.nthreads}")
        if ss.vcs_log_file:
            vcs_args += ["-l", ss.vcs_log_file]
        # if ss.time_unit:
        #     vcs_args.append(f"-unit_timescale={ss.time_unit}")

        log.info("Running vcs")
        if top:
            vcs_args += ["-top", top]
        if ss.initreg is not None:
            vcs_args.append("+vcs+initreg+random")
            simv_args.append(f"+vcs+initreg+{ss.initreg}")

        if ss.ucli:
            common_run_args.append("-ucli")
            if ss.one_shot_run:
                self.vcs.console_colors = False

        if ss.ucli_script:
            common_run_args += ["-do", str(ss.ucli_script)]
        if ss.gui:
            if isinstance(ss.gui, str) and ss.gui.lower() not in ("true", "false", "1", "0"):
                common_run_args.append(f"-gui={ss.gui}")
            elif ss.gui is True:
                common_run_args.append("-gui")
        if ss.one_shot_run:
            vcs_args.append("-R")
            vcs_args += common_run_args
        self.vcs.run(*vcs_args)
        if ss.sim_no_save:
            simv_args.append("-no_save")
        if not ss.one_shot_run:
            self.simv.run(*simv_args, *common_run_args)
        if ss.to_vcd:
            if ss.fsdb:
                if not ss.fsdb.exists():
                    log.error(f"FSDB file {ss.fsdb} does not exist")
                else:
                    fsdb2vcd_args = [
                        "-consolidate_bus",
                        "-sv",
                        # "-keep_enum",
                    ]
                    self.fsdb2vcd.run(
                        ss.fsdb, *fsdb2vcd_args, "-o", ss.fsdb.with_suffix(".vcd"), check=True
                    )
            if ss.vpd:
                if not ss.vpd.exists():
                    log.error(f"VPD file {ss.vpd} does not exist")
                else:
                    vpd2vcd_args = [
                        "+morevhdl",
                    ]
                    self.vpd2vcd.run(ss.vpd, *vpd2vcd_args, ss.vpd.with_suffix(".vcd"), check=True)
