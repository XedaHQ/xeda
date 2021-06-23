import logging

from .vivado_synth import RunOptions, VivadoSynth
from ..flow import SynthFlow

logger = logging.getLogger()


class VivadoPrjSynth(VivadoSynth, SynthFlow):

    class Settings(VivadoSynth.BaseSettings):
        # fail_critical_warning = False
        # optimize_power = False
        # optimize_power_postplace = False
        # synth_output_dir = 'output'
        # checkpoints_dir = 'checkpoints'

        synth: RunOptions = RunOptions(
            steps={
                'SYNTH_DESIGN': {}, 'OPT_DESIGN': {}, 'POWER_OPT_DESIGN': {},
            })

        impl: RunOptions = RunOptions(
            steps={
                'PLACE_DESIGN': {}, 'POST_PLACE_POWER_OPT_DESIGN': {},
                'PHYS_OPT_DESIGN': {}, 'ROUTE_DESIGN': {}, 'WRITE_BITSTREAM': {}
            })

    def run(self):
        settings = self.settings
        settings.synth.steps = {
            **{
                'SYNTH_DESIGN': {}, 'OPT_DESIGN': {}, 'POWER_OPT_DESIGN': {},
            }, **settings.synth.steps}
        settings.impl.steps = {
            **{
                'PLACE_DESIGN': {}, 'POST_PLACE_POWER_OPT_DESIGN': {},
                'PHYS_OPT_DESIGN': {}, 'ROUTE_DESIGN': {}, 'WRITE_BITSTREAM': {}
            }, **settings.impl.steps}

        print(f'settings={settings}')
        clock_xdc_path = self.copy_from_template(f'clock.xdc')

        logger.info(
            f"blacklisted_resources: {self.settings.blacklisted_resources}")

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

        if 'bram_tile' in settings.blacklisted_resources:
            # FIXME also add -max_uram 0 for ultrascale+
            settings.synth.steps['SYNTH_DESIGN']['MAX_BRAM'] = 0
        if 'dsp' in settings.blacklisted_resources:
            settings.synth.steps['SYNTH_DESIGN']['MAX_DSP'] = 0

        reports_tcl = self.copy_from_template(f'vivado_prj_report.tcl')
        script_path = self.copy_from_template(f'vivado_project.tcl',
                                              xdc_files=[clock_xdc_path],
                                              reports_tcl=reports_tcl
                                              )
        return self.run_vivado(script_path)
