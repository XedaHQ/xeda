# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
from ..flow import SimFlow


class Modelsim(SimFlow):

    def run(self):
        vcom_options = ['-lint']
        vlog_options = ['-lint']
        vsim_opts = []

        sdf_conf = self.settings.flow.get('sdf')
        tb_uut = self.settings.design.get('tb_uut')
        # sdf has two fields: file (path to sdf file), and type [min, max, typ]
        if sdf_conf and tb_uut:
            if not isinstance(sdf_conf, list):
                sdf_conf = [sdf_conf]
            for sdf in sdf_conf:
                if isinstance(sdf, str):
                    sdf = {'file': sdf, 'delay_type': 'max'}
                vsim_opts.append(f'-sdf{sdf["delay_type"]}')
                vsim_opts.append(f'tb_uut={sdf["file"]}')
            if not self.vcd:
                self.settings.flow['vcd'] = 'timing.vcd'

        tb_generics_opts = ' '.join([f"-g{k}={v}" for k, v in self.settings.design["tb_generics"].items()])

        script_path = self.copy_from_template(f'run.tcl',
                                              generics_options=tb_generics_opts,
                                              vcom_opts=vcom_options,
                                              vlog_opts=vlog_options,
                                              vsim_opts=vsim_opts,
                                              vcd=self.vcd
                                              )

        self.run_process('vsim', ['-batch', '-do', f'do {script_path}'],
                         stdout_logfile='modelsim_stdout.log',
                         check=True
                         )
