# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from typing import Optional
from ...utils import SDF
from ..flow import SimFlow
from ...tool import Tool


class Modelsim(SimFlow):
    class Settings(SimFlow.Settings):
        sdf: SDF = SDF()
        modelsimini: Optional[str] = None

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
