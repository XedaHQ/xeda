# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from ..suite import Suite


class Ghdl(Suite):
    name = 'ghdl'
    supported_flows = ['sim']

    def __init__(self, settings, args, logger):

        super().__init__(settings, args, logger)

    def __runflow_impl__(self, flow):
        # TODO synthesis, lint
        reports_dir = 'reports'

        warns = ['-Wbinding', '-Wreserved', '-Wlibrary', '-Wvital-generic',
                 '-Wdelayed-checks', '-Wbody', '-Wspecs', '-Wunused', '--warn-no-runtime-error']

        vhdl_std = str(self.settings.design['vhdl_std'])
        vhdl_std_opt = f'--std={"93c" if vhdl_std == "93" else vhdl_std}'
        optimize = ['-O3']
        analysis_options = ['-frelaxed-rules', '--warn-no-vital-generic',
                            '-frelaxed', '--mb-comments', vhdl_std_opt] + optimize

        vhdl_synopsys = self.settings.design['vhdl_synopsys'] if 'vhdl_synopsys' in self.settings.design else False

        if vhdl_synopsys:
            analysis_options += ['--ieee=synopsys', '-fsynopsys']

        elab_options = [vhdl_std_opt]
        if vhdl_synopsys:
            elab_options += ['-fsynopsys']

        run_options = ['--ieee-asserts=disable-at-0']  # TODO

        stop_time = self.settings.flow.get('stop_time')
        if stop_time:
            run_options.append(f'--stop-time={stop_time}')

        # --sdf=min=PATH=FILENAME
        # --sdf=typ=PATH=FILENAME
        # --sdf=max=PATH=FILENAME
        vital_sdf = self.settings.flow.get('sdf')
        if vital_sdf:
            if not isinstance(vital_sdf, list):
                vital_sdf = [vital_sdf]
            for s in vital_sdf:
                run_options.append(f'--sdf={s["delay"]}={s["inst_path"]}={s["file"]}')

        vcd = self.settings.flow.get('vcd')
        if vcd:
            run_options.append(f'--vcd={vcd}')

        tb_generics_opts = [f"-g{k}={v}" for k, v in self.settings.design["tb_generics"].items()]
        rtl_generics_opts = [f"-g{k}={v}" for k, v in self.settings.design["generics"].items()]

        sources = list(map(lambda x: str(x), self.settings.design['sources']))

        # self.run_process('ghdl', ['-a'] + analysis_options + warns + sources,
        #                  initial_step='Analyzing VHDL files',
        #                  stdout_logfile='ghdl_analyze.log',
        #                  check=True
        #                  )

        # self.run_process('ghdl', ['-e'] + elab_options + [self.settings.design['tb_top']],
        #                  initial_step='Elaborating design',
        #                  stdout_logfile='ghdl_elaborate.log',
        #                  check=True
        #                  )
        self.run_process('ghdl', ['-i'] + analysis_options + warns + sources,
                         initial_step='Analyzing VHDL files',
                         stdout_logfile='ghdl_analyze.log',
                         check=True
                         )

        self.run_process('ghdl', ['-m', '-f'] + elab_options + warns + [self.settings.design['tb_top']],
                         initial_step='Elaborating design',
                         stdout_logfile='ghdl_elaborate.log',
                         check=True
                         )

        self.run_process('ghdl', ['-r', vhdl_std_opt, self.settings.design['tb_top']] + run_options + tb_generics_opts,
                         initial_step='Running simulation',
                         stdout_logfile='ghdl_run.log',
                         force_echo=True
                         )

        self.reports_dir = self.run_dir / reports_dir

        if not self.reports_dir.exists():
            self.reports_dir.mkdir(parents=True)
