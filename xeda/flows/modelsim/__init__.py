# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
from ..flow import SimFlow

class Modelsim(SimFlow):

    def run(self):
        vcom_options = []
        vlog_options = []


        tb_generics_opts = ' '.join([f"-g{k}={v}" for k, v in self.settings.design["tb_generics"].items()])

        script_path = self.copy_from_template(f'run.tcl',
                                              generics_options=tb_generics_opts,
                                              vcom_opts=vcom_options,
                                              vlog_opts=vlog_options,
                                              vcd=self.vcd
                                              )

        self.run_process('vsim', ['-batch', '-do', f'do {script_path}'],
                         stdout_logfile='modelsim_stdout.log',
                         check=True
                         )

            
