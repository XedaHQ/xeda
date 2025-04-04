import logging
import os
from pathlib import Path
from typing import List, Literal, Optional, Union

from ..tool import Tool
from ..flow import SimFlow, FlowException
from ..dataclass import Field
from ..design import SourceType

log = logging.getLogger(__name__)


class Vcs(SimFlow):
    """Synopsys VCS simulator"""

    vlogan = Tool("vlogan", version_flag=None)
    vhdlan = Tool("vhdlan", version_flag=None)
    vcs = Tool("vcs")

    class Settings(SimFlow.Settings):
        clean: bool = Field(True, description="Clean the run path before running")
        simv_flags: List[str] = []
        work_dir: Optional[str] = "work"
        sim_no_save: bool = True
        generate_kdb: bool = False
        supress_banner: bool = True
        vhdl_xlrm: bool = Field(
            True, description="Enables VHDL features beyond those described in LRM"
        )
        one_shot_run: bool = Field(
            False,
            description="Run the simulation using the vcs step. If set to true, simv_flags will be ignored.",
        )
        ucli: bool = Field(
            False,
            description="Enable UCLI (Universal Command Line Interface) when running simulator executable (simv)",
        )
        gui: bool = False
        full64: bool = True
        time_unit: Optional[str] = "1ns"
        time_resolution: Optional[str] = "1ps"
        sdf_file: Optional[Union[str, Path]] = Field(
            None, description="SDF file for back-annotating delays using the unified SDF feature"
        )
        sdf_instance: Optional[str] = Field(
            None,
            description="Hierarchy instance to use as the root path for back-annotating delays",
        )
        sdf_type: Literal["min", "typ", "max"] = Field(
            "typ", description="SDF type for back-annotating delays"
        )
        warn: Optional[str] = "all,noTFIPC,noLCA_FEATURES_ENABLED"
        lint: Optional[str] = "all,TFIPC-L,noVCDE,noTFIPC,noIWU,noOUDPE,noUI"
        debug_access: Optional[str] = None
        timing_sim: bool = Field(True, description="Enable timing simulation for VITAL")
        init_std_logic: Optional[Literal["U", "X", "0", "1", "Z", "W", "L", "H", "-"]] = Field(
            None, description="Initialize std_logic to this value"
        )
        vlogan_flags: List[str] = []
        vhdlan_flags: List[str] = []
        vcs_flags: List[str] = []
        vcs_log_file: Optional[str] = "vcs.log"
        top_is_vhdl: Optional[bool] = Field(
            None, description="Top module is VHDL"
        )  # TODO: move to design?

    def clean(self):
        super().purge_run_path()

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.sdf_file:
            if ss.sdf_instance is None:
                raise FlowException(f"SDF instance is required when SDF file is provided")
            ss.sdf_file = self.process_path(ss.sdf_file, resolve_to=self.design.design_root)

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.top_is_vhdl is None:
            ss.top_is_vhdl = self.design.tb.sources[-1].type is SourceType.Vhdl
        os.environ["VCS_TARGET_ARCH"] = "amd64"
        os.environ["VCS_ARCH_OVERRIDE"] = "linux"
        vlogan_args = ss.vlogan_flags
        vhdlan_args = ss.vhdlan_flags
        vcs_args = ss.vcs_flags
        simv_args = ss.simv_flags
        if ss.gui:
            ss.generate_kdb = True
        if ss.supress_banner:
            vlogan_args.append("-nc")
            vhdlan_args.append("-nc")
            vcs_args.append("-nc")
            simv_args.append("-nc")
        if ss.full64:
            vlogan_args.append("-full64")
            vhdlan_args.append("-full64")
            vcs_args.append("-full64")
        if ss.generate_kdb:
            vlogan_args.append("-kdb")
            vhdlan_args.append("-kdb")
            vcs_args.append("-kdb")
        # vlogan_args.extend(["-work", "WORK"])
        # vhdlan_args.extend(["-work", "WORK"])
        vlogan_args.append(f"-timescale={ss.time_resolution}/{ss.time_resolution}")
        if ss.vhdl_xlrm:
            vhdlan_args.append("-xlrm")
        if not ss.timing_sim:
            vhdlan_args.append("-functional_vital")
        incdirs: List[str] = []
        for d in incdirs:
            vlogan_args.append(f"+incdir+{d}")
        if self.design.language.vhdl.standard in ("08", "2008"):
            vhdlan_args.append("-vhdl08")
        elif self.design.language.vhdl.standard in ("02", "2002"):
            vhdlan_args.append("-vhdl02")
        vlog_files = self.design.sources_of_type(SourceType.Verilog, rtl=True, tb=True)
        if vlog_files:
            log.info("analyzing Verilog files")
            self.vlogan.run(*vlogan_args, "+v2k", *(str(f) for f in vlog_files))
        sv_files = self.design.sources_of_type(SourceType.SystemVerilog, rtl=True, tb=True)
        if sv_files:
            log.info("analyzing SystemVerilog files")
            self.vlogan.run(*vlogan_args, "-sverilog", *(str(f) for f in sv_files))
        vhdl_files = self.design.sources_of_type(SourceType.Vhdl, rtl=True, tb=True)
        if vhdl_files:
            log.info("analyzing VHDL files")
            self.vhdlan.run(*vhdlan_args, *(str(f) for f in vhdl_files))
        top = self.design.tb.top[0]
        vcs_args += ["-notice"]
        if ss.lint:
            vcs_args.append(f"+lint={ss.lint}")
        if ss.warn:
            vcs_args.append(f"+warn={ss.warn}")
        if ss.debug_access:
            vcs_args.append(f"-debug_access+{ss.debug_access}")
        if ss.time_resolution:
            vcs_args.append(f"-sim_res={ss.time_resolution}")

        if ss.sdf_file and ss.sdf_instance:
            vcs_args += [
                "-sdf",
                f"{ss.sdf_type}:{ss.sdf_instance}:{ss.sdf_file}",
            ]

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

        one_shot_run = ss.one_shot_run
        common_run_args = []

        if ss.ucli:
            common_run_args.append("-ucli")
        if ss.gui:
            common_run_args.append("-gui")
        if one_shot_run:
            vcs_args.append("-R")
            vcs_args += common_run_args
        self.vcs.run(*vcs_args)
        if ss.sim_no_save:
            simv_args.append("-no_save")
        if not one_shot_run:
            simv = Tool("./simv", version_flag=None)
            simv.run(*simv_args, *common_run_args)
