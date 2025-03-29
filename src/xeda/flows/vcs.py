import logging
from copy import deepcopy
from typing import List, Optional

from ..tool import Tool
from ..flow import SimFlow
from ..design import SourceType

log = logging.getLogger(__name__)


class Vcs(SimFlow):
    """Synopsys VCS simulator"""

    vlogan = Tool("vlogan")
    vhdlan = Tool("vhdlan")
    vcs = Tool("vcs")

    class Settings(SimFlow.Settings):
        simv: str = "simv"
        simv_flags = ["-nc", "-lca"]
        work_dir: Optional[str] = "work"
        timescale: Optional[str] = "1ns/1ps"
        vlogan_flags: List[str] = ["-full64", "-nc", "+warn=all"]  # TODO
        vhdlan_flags: List[str] = ["-full64", "-nc"]  # "-cycle", "-event" "+warn=all"?
        vcs_flags: List[str] = ["-full64", "-nc"]
        vcs_log_file: Optional[str] = "vcs.log"

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        with open("synopsys_sim.setup", "w", encoding="utf-8") as f:
            f.writelines(
                [
                    "WORK  > default",
                    f"default : {ss.work_dir}",
                ]
            )

        vlogan_args = deepcopy(ss.vlogan_flags)
        vhdlan_args = deepcopy(ss.vhdlan_flags)
        if ss.work_dir:
            vlogan_args.extend(["-work", ss.work_dir])
            # vhdlan_args.extend(["-work", ss.work_dir]) # ???
        if ss.timescale:
            vlogan_args.append(f"-timescale={ss.timescale}")
            vhdlan_args.append(f"-timescale={ss.timescale}")
        # FIXME
        incdirs: List[str] = []
        for d in incdirs:
            vlogan_args.append(f"+incdir+{d}")
        # vhdlan_args.append("-cycle")  # ???
        # vhdlan_args.extend("-event")  # ???
        if self.design.language.vhdl.standard in ("08", "2008"):
            vhdlan_args.extend("-vhdl08")

        # FIXME
        vlogan_args += [, ]
        vlog_files = self.design.sources_of_type(SourceType.Verilog)
        if vlog_files:
            log.info("analyzing Verilog files")
            self.vlogan.run(*vlogan_args, "+v2k", *(str(f) for f in vlog_files))
        sv_files = self.design.sources_of_type(SourceType.SystemVerilog)
        if sv_files:
            log.info("analyzing SystemVerilog files")
            self.vlogan.run(*vlogan_args, "-sverilog", *(str(f) for f in sv_files))
        vhdl_files = self.design.sources_of_type(SourceType.Vhdl)
        if vhdl_files:
            log.info("analyzing VHDL files")
            self.vhdlan.run(*vhdlan_args, *(str(f) for f in vhdl_files))
        top = self.design.tb.top[0]
        vcs_args = [top, *ss.vcs_flags]
        vcs_args += [
            "-notice",
            "+lint=all,noVCDE,noTFIPC,noIWU,noOUDPE",
        ]
        if ss.nthreads is not None:
            vcs_args.append(f"-j{ss.nthreads}")
        if ss.vcs_log_file:
            vcs_args += ["-l", ss.vcs_log_file]
        log.info("Running vcs")
        self.vcs.run(*vcs_args)

        simv = Tool("./simv")  # ???
        simv_args = deepcopy(ss.simv_flags)
        for k, v in self.design.tb.parameters.items():
            simv_args.append(f"+define+{k}={v}")
        simv_args.extend(["-l", top])
        simv.run(*simv_args)
