import logging
from .vivado_synth import RunOptions, VivadoSynth

log = logging.getLogger(__name__)


class VivadoPrjSynth(VivadoSynth):
    """Synthesize with Xilinx Vivado using a project-based flow"""

    class Settings(VivadoSynth.BaseSettings):
        """Settings for Vivado synthesis in project mode settings"""

        # fail_critical_warning = False
        # optimize_power = False
        # optimize_power_postplace = False
        # synth_output_dir = 'output'
        # checkpoints_dir = 'checkpoints'

        synth: RunOptions = RunOptions(
            strategy="Flow_PerfOptimized_high",
            steps={
                "SYNTH_DESIGN": {},
                "OPT_DESIGN": {},
                "POWER_OPT_DESIGN": {},
            },
        )

        impl: RunOptions = RunOptions(
            strategy="Performance_ExploreWithRemap",
            steps={
                "PLACE_DESIGN": {},
                "POST_PLACE_POWER_OPT_DESIGN": {},
                "PHYS_OPT_DESIGN": {},
                "ROUTE_DESIGN": {},
                "WRITE_BITSTREAM": {},
            },
        )

    def run(self):
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
        settings.synth.steps = {
            **{
                "SYNTH_DESIGN": {},
                "OPT_DESIGN": {},
                "POWER_OPT_DESIGN": {},
            },
            **settings.synth.steps,
        }
        settings.impl.steps = {
            **{
                "PLACE_DESIGN": {},
                "POST_PLACE_POWER_OPT_DESIGN": {},
                "PHYS_OPT_DESIGN": {},
                "ROUTE_DESIGN": {},
                "WRITE_BITSTREAM": {},
            },
            **settings.impl.steps,
        }

        if not self.design.rtl.clock_port:
            log.critical(
                "No clocks specified for top RTL design. Continuing with synthesis anyways."
            )
        else:
            assert (
                settings.clock_period
            ), "`clock_period` must be specified and be positive value"
            freq = 1000 / settings.clock_period
            log.info(
                f"clock.port={self.design.rtl.clock_port} clock.frequency={freq:.3f} MHz"
            )
        clock_xdc_path = self.copy_from_template(f"clock.xdc")

        if settings.blacklisted_resources:
            log.info(f"blacklisted_resources: {self.settings.blacklisted_resources}")

        # for x in ["synth", "impl"]:
        #     x_options = flow_settings.get(f"{x}_options")
        #     if x_options:
        #         if isinstance(x_options, dict):
        #             options[x]=x_options
        #         elif isinstance(x_options, str):
        #             options[x]= {k:v for (k,v) in [tuple(kv.split("=")[0:2]) for kv in x_options.split(",")]}
        #     # overrides
        #     strategy = flow_settings.get(f"{x}_strategy")
        #     if strategy:
        #         options[x]["strategy"] = strategy

        if settings.synth.steps["SYNTH_DESIGN"] is None:
            settings.synth.steps["SYNTH_DESIGN"] = {}
        assert settings.synth.steps["SYNTH_DESIGN"] is not None
        if "bram_tile" in settings.blacklisted_resources:
            # FIXME also add -max_uram 0 for ultrascale+
            settings.synth.steps["SYNTH_DESIGN"]["MAX_BRAM"] = 0
        if "dsp" in settings.blacklisted_resources:
            settings.synth.steps["SYNTH_DESIGN"]["MAX_DSP"] = 0

        reports_tcl = self.copy_from_template(f"vivado_prj_report.tcl")
        script_path = self.copy_from_template(
            f"vivado_project.tcl", xdc_files=[clock_xdc_path], reports_tcl=reports_tcl
        )
        self.vivado.run("-source", script_path)
