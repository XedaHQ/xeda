# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from typing import List, Optional

from ...dataclass import Field
from ...flow import SimFlow
from ...tool import Tool
from ...utils import SDF


class Modelsim(SimFlow):
    """Simulate a VHDL, Verilog, SystemVerilog or mixed-language design with Siemens ModelSim.

    Handles both RTL and gate-level netlist simulation; a netlist simulation can be annotated
    with timing from an SDF file via the `sdf` setting.
    """

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    class Settings(SimFlow.Settings):
        sdf: SDF = Field(
            SDF(),
            description="SDF timing-annotation files to back-annotate onto the netlist, per delay "
            "corner (min/typ/max) and optional instance root.",
        )
        modelsimini: Optional[str] = Field(
            None,
            description="Path to a `modelsim.ini` to use instead of the tool default, e.g. one "
            "with pre-compiled vendor libraries mapped.",
        )

    def run(self) -> None:
        """Compile design sources and execute the ModelSim script."""
        assert isinstance(self.settings, self.Settings)
        vcom_options = ["-lint"]
        vlog_options = ["-lint"]
        # vsim's arguments, one word each: the template writes each as a TCL word, so an SDF
        # path with a space is one argument
        vsim_opts: List[str] = []
        tb = self.design.tb
        ss = self.settings
        # TODO are library paths supported?
        for lib_name, _ in ss.lib_paths:  # (name, path) pairs; `-L` takes the name
            if lib_name:
                vsim_opts += ["-L", lib_name]
        sdf_root = ss.sdf.root if ss.sdf.root else tb.uut
        for dt, f in ss.sdf.delay_items():
            assert sdf_root, "Neither settings.sdf.root or design.tb.uut are provided"
            vsim_opts.extend([f"-sdf{dt}", f"{sdf_root}={f}"])
        vsim_opts += [f"-g{k}={v}" for k, v in tb.parameters.items()]

        script_path = self.copy_from_template(
            "run.tcl",
            vcom_opts=" ".join(vcom_options),
            vlog_opts=" ".join(vlog_options),
            vsim_opts=vsim_opts,
        )

        modelsim_opts = ["-batch", "-do", f"do {script_path}"]
        if ss.modelsimini:
            modelsim_opts.extend(["-modelsimini", ss.modelsimini])
        vsim = Tool("vsim")
        vsim.run(*modelsim_opts)
