from copy import deepcopy
import logging
from typing import List, Optional
from .flow import SimFlow
from ..tool import Tool

log = logging.getLogger(__name__)


class Vcs(SimFlow):
    """Synopsys VCS simulator"""

    dc_shell = Tool(executable="dc_shell-xg-t")  # type: ignore
    vlogan = Tool("vlogan")
    vhdlan = Tool("vhdlan")
    vcs = Tool("vcs")

    class Settings(SimFlow.Settings):
        simv: str = "simv"
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
        vhdlan_args.append("-cycle")  # ???
        vhdlan_args.extend("-event")  # ???
        if self.design.language.vhdl.standard == "2008":
            vhdlan_args.extend("-vhdl08")

        # FIXME
        vlogan_args += ["-sverilog", "+v2k"]
        top = self.design.tb.top[0]
        vcs_args = [top, *ss.vcs_flags]
        vcs_args += [
            f"-j{ss.nthreads}",
            "-notice",
            "+lint=all,noVCDE,noTFIPC,noIWU,noOUDPE",
        ]

        if ss.vcs_log_file:
            vcs_args += ["-l", ss.vcs_log_file]

        log.info("analyzing Verilog files")
        self.vlogan.run(*vlogan_args)
        log.info("analyzing VHDL files")
        self.vhdlan.run(*vhdlan_args)
        log.info("Running vcs")
        self.vcs.run(*vcs_args)

        simv = Tool("./simv")  # ???
        simv_args = ["-nc", "-lca"]
        for k, v in self.design.tb.parameters.items():
            simv_args.append(f"+define+{k}={v}")
        simv_args.extend(["-l", top])
        simv.run(*simv_args)
