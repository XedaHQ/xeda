# Xeda Vivado Synthtesis flow
# ©2021 Kamyar Mohajerani and contributors

import logging
from typing import Union
from .vivado_synth import VivadoSynth
from ..flow import SynthFlow
from .vivado import vivado_generics

logger = logging.getLogger()

class VivadoPrjSynth(VivadoSynth, SynthFlow):
    default_settings = {**SynthFlow.default_settings, 'nthreads': 4,
                        'fail_critical_warning': False, 'fail_timing': False,
                        'optimize_power': False, 'optimize_power_postplace': False}

    required_settings = {'clock_period': Union[str, int]}

    synth_output_dir = 'output'
    checkpoints_dir = 'checkpoints'

    blacklisted_resources = ['latch']

    def run(self):
        rtl_settings = self.settings.design["rtl"]
        flow_settings = self.settings.flow
        generics_options = vivado_generics(
            rtl_settings.get("generics", {}), sim=False)

        input_delay = flow_settings.get('input_delay', 0)
        output_delay = flow_settings.get('output_delay', 0)
        constrain_io = flow_settings.get('constrain_io', False)
        # out_of_context = flow_settings.get('out_of_context', False)

        clock_xdc_path = self.copy_from_template(f'clock.xdc',
                                                 constrain_io=constrain_io,
                                                 input_delay=input_delay,
                                                 output_delay=output_delay,
                                                 )

        self.blacklisted_resources = flow_settings.get(
            'blacklisted_resources', self.blacklisted_resources)
        

        logger.info(f"blacklisted_resources: {self.blacklisted_resources}")

        options = dict(
            synth=dict(),
            impl=dict(),
        )

        #TODO find a suitable interface for getting more general args

        for x in ["synth", "impl"]:
            x_options = flow_settings.get(f"{x}_options")
            if x_options:
                if isinstance(x_options, dict):
                    options[x]=x_options
                elif isinstance(x_options, str):
                    options[x]= {k:v for (k,v) in [tuple(kv.split("=")[0:2]) for kv in x_options.split(",")]}
            # overrides
            strategy = flow_settings.get(f"{x}_strategy")
            if strategy:
                options[x]["strategy"] = strategy

        logger.info(f"options={options}")

        # FIXME
        # if 'bram_tile' in self.blacklisted_resources:
        #     # FIXME also add -max_uram 0 for ultrascale+
        #     options['synth'].append('-max_bram 0')
        # if 'dsp' in self.blacklisted_resources:
        #     options['synth'].append('-max_dsp 0')

        script_path = self.copy_from_template(f'vivado_project.tcl',
                                              xdc_files=[clock_xdc_path],
                                              options=options,
                                              generics_options=generics_options,
                                              synth_output_dir=self.synth_output_dir,
                                              checkpoints_dir=self.checkpoints_dir
                                              )
        return self.run_vivado(script_path)