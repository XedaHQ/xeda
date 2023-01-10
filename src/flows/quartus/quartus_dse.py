# TODO
# class QuartusDse(QuartusSynth, DseFlow):
#     def run(self):
#         self.create_project()
#         # 'explore': Exploration flow to use, if not specified in --config
#         #   configuration file. Valid flows: timing_aggressive,
#         #   all_optimization_modes, timing_high_effort, seed,
#         #   area_aggressive, power_high_effort, power_aggressive
#         # 'compile_flow':  'full_compile', 'fit_sta' and 'fit_sta_asm'.
#         # 'timeout': Limit the amount of time a compute node is allowed to run. Format: hh:mm:ss
#         if 'dse' not in self.settings.flow:
#             self.fatal('`flows.quartus.dse` settings are missing!')

#         dse = self.settings.flow['dse']
#         if 'nproc' not in dse or not dse['nproc']:
#             dse['nproc'] = self.nthreads

#         script_path = self.copy_from_template(f'settings.dse',
#                                               dse=dse
#                                               )
#         self.run_process('quartus_dse',
#                          ['--use-dse-file', script_path, self.settings.design['name']],
#                          stdout_logfile='dse_stdout.log',
#                          initial_step="Running Quartus DSE",
#                          )
# self.run_process('quartus_sh',
#                  ['--dse', '-project', self.settings.design['name'], '-nogui', '-concurrent-compiles', '8', '-exploration-space',
#                   "Extra Effort Space", '-optimization-goal', "Optimize for Speed", '-report-all-resource-usage', '-ignore-failed-base'],
#                  stdout_logfile='dse_stdout.log'
#                  )
# def quartus_gen_convert(k, x, sim):
#     if sim:
#         if isinstance(x, dict) and "file" in x:
#             p = x["file"]
#             assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
#             x = self.conv_to_relative_path(p.strip())
#             self.logger.info(f'Converting generic `{k}` marked as `file`: {p} -> {x}')
#     xl = str(x).strip().lower()
#     if xl == 'false':
#         return "1\\'b0"
#     if xl == 'true':
#         return "1\\'b1"
#     return x

# self.run_process('quartus_eda', [prj_name, '--simulation', '--functional', '--tool=modelsim_oem', '--format=verilog'],
#                         stdout_logfile='eda_1_stdout.log'
#                         )
# DSE:

# Available exploration spaces for this family are:
# "Seed Sweep"
# "Extra Effort Space"
# "Extra Effort Space for Quartus Prime Integrated Synthesis Projects"
# "Area Optimization Space"
# "Signature: Placement Effort Multiplier"
# "Custom Space"

# Valid optimization-goal options are:
# "Optimize for Speed"
# "Optimize for Area"
# "Optimize for Power"
# "Optimize for Negative Slack and Failing Paths"
# "Optimize for Average Period"
# "Optimize for Quality of Fit"
