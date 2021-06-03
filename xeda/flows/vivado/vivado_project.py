import logging
from typing import Dict, Any, Optional
from pydantic.main import BaseModel

from .vivado_synth import VivadoSynth
from ..flow import SynthFlow, Flow
from . import vivado_generics

logger = logging.getLogger()


class RunOptions(BaseModel):
    strategy: Optional[str] = None
    steps: Dict[str, Any] = {}


class VivadoPrjSynth(VivadoSynth, SynthFlow):

    class Settings(SynthFlow.Settings):
        nthreads: int = 4
        # fail_critical_warning = False
        fail_timing = True
        # optimize_power = False
        # optimize_power_postplace = False
        # synth_output_dir = 'output'
        # checkpoints_dir = 'checkpoints'
        blacklisted_resources = ['latch']

        input_delay = 0
        output_delay = 0
        constrain_io = False
        out_of_context = False
        synth: RunOptions = RunOptions()
        impl: RunOptions = RunOptions()

    def run(self):
        return
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

        # logger.info(f"options={options}")

        # FIXME
        # if 'bram_tile' in self.blacklisted_resources:
        #     # FIXME also add -max_uram 0 for ultrascale+
        #     options['synth'].append('-max_bram 0')
        # if 'dsp' in self.blacklisted_resources:
        #     options['synth'].append('-max_dsp 0')

        script_path = self.copy_from_template(f'vivado_project.tcl',
                                              xdc_files=[clock_xdc_path],
                                              )
        return self.run_vivado(script_path)
