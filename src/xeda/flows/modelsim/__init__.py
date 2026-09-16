# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from typing import Optional

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
        assert isinstance(self.settings, self.Settings)
        vcom_options = ["-lint"]
        vlog_options = ["-lint"]
        vsim_opts = []
        tb = self.design.tb
        ss = self.settings
        # TODO are library paths supported?
        vsim_opts.extend([f"-L {lib_name}" for lib_name in ss.lib_paths])
        sdf_root = ss.sdf.root if ss.sdf.root else tb.uut
        for dt, f in ss.sdf.delay_items():
            assert sdf_root, "Neither settings.sdf.root or design.tb.uut are provided"
            vsim_opts.extend([f"-sdf{dt}", f"{sdf_root}={f}"])

        tb_generics_opts = " ".join([f"-g{k}={v}" for k, v in tb.parameters.items()])

        script_path = self.copy_from_template(
            "run.tcl",
            generics_options=tb_generics_opts,
            vcom_opts=" ".join(vcom_options),
            vlog_opts=" ".join(vlog_options),
            vsim_opts=" ".join(vsim_opts),
        )

        modelsim_opts = ["-batch", "-do", f"do {script_path}"]
        if ss.modelsimini:
            modelsim_opts.extend(["-modelsimini", ss.modelsimini])
        vsim = Tool("vsim")
        vsim.run(*modelsim_opts)
