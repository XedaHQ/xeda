# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from ..flow import SynthFlow


class Dc(SynthFlow):

    def run(self):
        self.nthreads = min(self.nthreads, 16)
        script_path = self.copy_from_template(f'run.tcl',
                                              results_dir='results',
                                              adk=self.settings.flow['adk'],
                                              )

        self.run_process('dc_shell-xg-t', ['-64bit', '-topographical_mode', '-f', script_path],
                         stdout_logfile='dc_stdout.log',
                         check=True
                         )

    def parse_reports(self):
        reports_dir = self.reports_dir
        top_name = self.settings.design['top']

        failed = False  # TODO FIXME

        self.parse_report(reports_dir / f'{top_name}.mapped.area.rpt',
                          r'Number of ports:\s*(?P<num_ports>\d+)',
                          r'Number of nets:\s*(?P<num_nets>\d+)',
                          r'Number of cells:\s*(?P<num_cells>\d+)',
                          r'Number of combinational cells:\s*(?P<num_cells_combinational>\d+)',
                          r'Number of sequential cells:\s*(?P<num_cells_sequentual>\d+)',
                          r'Number of macros/black boxes:\s*(?P<num_macro_bbox>\d+)',
                          r'Number of buf/inv:\s*(?P<num_buf_inv>\d+)',
                          r'Number of references:\s*(?P<num_refs>\d+)',
                          r'Combinational area:\s*(?P<area_combinational>\d+(?:\.\d+)?)',
                          r'Buf/Inv area:\s*(?P<area_buf_inv>\d+(?:\.\d+)?)',
                          r'Noncombinational area:\s*(?P<area_noncombinational>\d+(?:\.\d+)?)',
                          r'Macro/Black Box area:\s*(?P<area_macro_bbox>\d+(?:\.\d+)?)',
                          r'Net Interconnect area:\s*(?P<area_interconnect>\S+.*$)',
                          r'Total cell area:\s*(?P<area_cell_total>\d+(?:\.\d+)?)',
                          r'Total area:\s*(?P<area_macro_bbox>\w+)',
                          r'Core Area:\s*(?P<area_core>\d+(?:\.\d+)?)',
                          r'Aspect Ratio:\s*(?P<aspect_ratio>\d+(?:\.\d+)?)',
                          r'Utilization Ratio:\s*(?P<utilization_ratio>\d+(?:\.\d+)?)',
                          dotall=False
                          )
        self.results['success'] = not failed
