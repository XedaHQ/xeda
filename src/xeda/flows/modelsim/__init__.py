# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
from ..flow import SimFlow


class Modelsim(SimFlow):

    def run(self):
        vcom_options = ['-lint']
        vlog_options = ['-lint']
        vsim_opts = []
        rtl_settings = self.settings.design['rtl']
        tb_settings = self.settings.design['tb']
        flow_settings = self.settings.flow
        sdf_conf = flow_settings.get('sdf')
        tb_uut = tb_settings.get('uut')
        libraries = flow_settings.get('libraries')
        if libraries:
            vsim_opts.extend([f'-L {l}' for l in libraries ])
        sim_top = tb_settings['top']
        if isinstance(sim_top, list):
            sim_top = ' '.join(sim_top)
        # sdf has two fields: file (path to sdf file), and type [min, max, typ]
        if sdf_conf and tb_uut:
            if not isinstance(sdf_conf, list):
                sdf_conf = [sdf_conf]
            for sdf in sdf_conf:
                if isinstance(sdf, str):
                    sdf = {'file': sdf, 'delay_type': 'max'}
                vsim_opts.append(f'-sdf{sdf["delay_type"]}')
                vsim_opts.append(f'{tb_uut}={sdf["file"]}')
            if not self.vcd:
                flow_settings['vcd'] = 'timing_sim.vcd'

        tb_generics_opts = ' '.join([f"-g{k}={v}" for k, v in tb_settings["generics"].items()])

        modelsimini = flow_settings.get('modelsimini')


        script_path = self.copy_from_template(f'run.tcl',
                                              generics_options=tb_generics_opts,
                                              vcom_opts=' '.join(vcom_options),
                                              vlog_opts=' '.join(vlog_options),
                                              vsim_opts=' '.join(vsim_opts),
                                              top=sim_top,
                                              vcd=self.vcd
                                              )

        modelsim_opts = ['-batch', '-do', f'do {script_path}']

        if modelsimini:
            modelsim_opts.extend(['-modelsimini', modelsimini])
        
        self.run_process('vsim', modelsim_opts,
                         stdout_logfile='modelsim_stdout.log',
                         check=True
                         )
