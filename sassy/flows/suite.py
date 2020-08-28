# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import json
import os
import sys
import re
from pathlib import Path
import random
import subprocess
from datetime import datetime
from jinja2 import Environment, PackageLoader
import psutil
# import asyncio

from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner

from . import Settings, try_convert


class Suite:
    """ A flow may run one or more tools and is associated with a single set of settings and a single design, after completion contains data """
    name = None
    executable = None
    supported_flows = []

    def __init__(self, settings, args, logger, **flow_defaults):
        self.args = args
        self.logger = logger
        # this is the actual run directory set (and possibily created) by run(), could be different from what's in the settings/args
        self.run_dir = None
        # base flow defaults
        self.settings = Settings()
        self.settings.flow['clock_period'] = None
        self.settings.flow['run_dir'] = None
        # specific flow defaults
        self.settings.flow.update(**flow_defaults)

        self.settings.design.update(settings['design'])
        if self.name in settings['flows']:
            self.settings.flow.update(settings['flows'][self.name])

        # TODO FIXME improve path change logic
        self.source_paths_fixed = False

        self.results = dict()
        self.jinja_env = Environment(
            loader=PackageLoader(__package__ + f'.{self.name}', 'templates'),
            autoescape=False
        )

        self.reports_dir = None

    def get_run_dir(self, override=None, subdir=None):

        prefix = ""

        # TODO move higher!
        if self.args.command == 'fmax':
            prefix = "FMAX_"

        if override:
            self.settings.flow['run_dir'] = Path(override).resolve()

        # TODO cleanup and simplify
        fresh_subdir = False

        if self.settings.flow['run_dir']:
            run_dir = Path(self.settings.flow['run_dir'])
        else:
            if subdir is None:
                subdir = prefix + datetime.now().strftime("%Y-%m-%d-%H%M%S") + f'-{random.randrange(0,2**16):04x}'
                fresh_subdir = True

            run_dir = Path.cwd() / f'{self.name}_run' / subdir

        self.settings.flow['run_dir'] = run_dir

        if fresh_subdir:
            assert not run_dir.exists(), f"Path {run_dir} already exists!"
        if not run_dir.exists():
            run_dir.mkdir(parents=True)
        assert run_dir.exists() and run_dir.is_dir()
        return run_dir

    def __runflow_impl__(self, flow):
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def parse_reports(self):
        pass

    def dump_data(self):
        with open('synth_results.json', 'w') as outfile:
            json.dump(self.results, outfile, indent=4)

    def dump_settings(self):
        with open(self.settings_file, 'w') as outfile:
            json.dump(self.settings, outfile, indent=4)

    def copy_from_template(self, resource_name, **attr):
        template = self.jinja_env.get_template(resource_name)
        script_path = self.run_dir / resource_name
        self.logger.debug(f'generating {script_path.resolve()} from template.')
        rendered_content = template.render(flow=self.settings.flow,
                                           design=self.settings.design,
                                           nthreads=psutil.cpu_count(),
                                           debug=self.args.debug,
                                           **attr)
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def run(self, flow):
        if not flow:
            flow = self.supported_flows[0]  # first is the default flow
        else:
            if not (flow in self.supported_flows):
                sys.exit(f"Flow {flow} is not supported by {self.name}.")

        self.run_dir = self.get_run_dir(override=self.args.run_dir)

        def make_rel_paths(section):
            sources = []
            for src in self.settings.design[section]:
                path = Path(src).resolve()
                src_path = os.path.relpath(path, self.run_dir)
                sources.append(src_path)
            self.settings.design[section] = sources

        source_categories = ['sources', 'tb_sources']
        # TODO improve logic
        if not self.source_paths_fixed:
            self.logger.debug("changing source path relative to run path")
            for cat in source_categories:
                make_rel_paths(cat)
            self.source_paths_fixed = True

        hdl_types = {'vhdl': ['vhd', 'vhdl'], 'verilog': ['v', 'sv']}
        for cat in source_categories:
            for h in hdl_types.keys():
                self.settings.design[h + '_' + cat] = []
            for s in self.settings.design[cat]:
                for h, suffixes in hdl_types.items():
                    if Path(s).suffix[1:] in suffixes:
                        self.settings.design[h + '_' + cat].append(s)
                        break

        self.__runflow_impl__(flow)
        self.parse_reports()
        if self.results:  # non empty
            self.print_results()
            self.dump_results(flow)

    def run_process(self, prog, prog_args, check=True):
        proc = None
        spinner = None
        unicode = True
        verbose = self.args.verbose
        stdout_logfile = self.run_dir / 'stdout.log'
        start_step_hint = re.compile(r'={10}=*\( (?P<step>[\w\s]+) \)={10}=*')
        with open(stdout_logfile, 'w') as log_file:
            try:
                self.logger.info(f'Running `{prog} {" ".join(prog_args)}` in {self.run_dir}')
                if not verbose:
                    self.logger.info(f'Standard output from the tools will be saved to {stdout_logfile}')
                with subprocess.Popen([prog, *prog_args],
                                      cwd=self.run_dir,
                                      stdout=subprocess.PIPE,
                                      bufsize=1,
                                      universal_newlines=True,
                                      encoding='utf-8',
                                      errors='replace'
                                      ) as proc:
                    def end_step():
                        if spinner:
                            if unicode:
                                print('\r✅', end='')
                            spinner.finish()
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            end_step()
                            break
                        log_file.write(line)
                        log_file.flush()
                        if verbose:
                            print(line, end='')
                        else:
                            match = start_step_hint.match(line)
                            if match:
                                end_step()
                                step = match.group('step')
                                spinner = Spinner('⏳' + step if unicode else step)
                            else:
                                if spinner:
                                    spinner.next()
            except KeyboardInterrupt:
                print(SHOW_CURSOR)
                self.logger.critical("Received a keyboard interrupt!")
                if proc:
                    self.logger.critical(f"Terminating {proc.args[0]}[{proc.pid}]")
                    proc.terminate()
                    proc.kill()
                    proc.wait()
                sys.exit(1)

        print(SHOW_CURSOR)

        if proc.returncode != 0:
            self.logger.critical(
                f'`{proc.args[0]}` exited with returncode {proc.returncode}. Please check `{stdout_logfile}` for error messages!')
            if check:
                sys.exit('Exiting due to errors.')
        else:
            self.logger.info(f'Process completed with returncode {proc.returncode}')
        return proc

    # async def run_process_async(self, prog, args):
    #     async def read_stderr(stderr):
    #         print('read_stderr')
    #         while True:
    #             buf = await stderr.readline()
    #             if not buf:
    #                 break

    #             print(f'stderr: { buf.decode("utf-8")}', file=sys.stderr, end='')
    #             sys.stderr.flush()

    #     async def read_stdout(stdout):
    #         print('read_stdout')
    #         while True:
    #             buf = await stdout.readline()
    #             if not buf:
    #                 break

    #             print(f'stdout: {buf.decode("utf-8")}', file=sys.stdout, end='')
    #             sys.stdout.flush()
    #     # async def write_stdin(stdin):
    #     #     print('write_stdin')
    #     #     for i in range(100):
    #     #         buf = f'line: { i }\n'.encode()
    #     #         print(f'stdin: { buf }')

    #     #         stdin.write(buf)
    #     #         await stdin.drain()
    #     #         await asyncio.sleep(0.5)

    #     process = await asyncio.create_subprocess_exec(
    #         prog,
    #         *args,
    #         # stdin=asyncio.subprocess.PIPE,
    #         stdout=asyncio.subprocess.PIPE,
    #         stderr=asyncio.subprocess.PIPE,
    #         cwd=self.run_dir
    #     )

    #     try:
    #         await asyncio.gather(
    #             read_stderr(process.stderr),
    #             read_stdout(process.stdout),
    #             # write_stdin(proc.stdin)
    #         )
    #     except KeyboardInterrupt as e:
    #         process.kill()
    #         sys.exit('Interrupted!')
    #     except:
    #         raise

    #     return process

    def find_fmax(self):
        wns_threshold = 0.006
        improvement_threshold = 0.002
        failed_runs = 0
        best_period = None
        best_results = None
        best_rundir = None
        tried_rundirs = set()

        while True:
            set_period = self.settings.flow['clock_period']
            self.logger.info(f'[FMAX] Trying clock_period = {set_period:0.3f}ns')
            # fresh directory for each run
            self.settings.flow['run_dir'] = None
            self.run('synth')
            tried_rundirs.add(self.run_dir)
            wns = self.results['wns']
            success = self.results['success'] and wns >= 0
            period = self.results['clock_period']

            next_period = set_period - wns - improvement_threshold/2

            if success:
                if best_period:
                    if wns < wns_threshold:
                        self.logger.warning(
                            f'[FMAX] Stopping attempts as wns={wns} is lower than the flow\'s improvement threashold: {wns_threshold}')
                        break
                    max_failed = self.args.max_failed_runs
                    if failed_runs >= max_failed:
                        self.logger.warning(
                            f'[FMAX] Stopping attempts as number of FAILED runs has reached maximum allowed value of {max_failed}.'
                        )
                        break
                if not best_period or period < best_period:
                    best_period = period
                    best_rundir = self.run_dir
                    best_results = {**self.results}
            else:
                if best_period:
                    failed_runs += 1
                    next_period = (best_period + set_period) / 2 - improvement_threshold/2

            # worse or not worth it
            if best_period and (best_period - next_period) < improvement_threshold:
                self.logger.warning(
                    f'[FMAX] Stopping attempts as expected improvement of period is less than the improvement threshold of {improvement_threshold}.'
                )
                break
            self.settings.flow['clock_period'] = next_period

        self.logger.info(f'[FMAX] best_period = {best_period}')
        self.logger.info(f'[FMAX] best_rundir = {best_rundir}')
        print(f'---- Best results: ----')
        self.print_results(best_results)

        self.logger.info(f'Run directories: {" ".join([str(os.path.relpath(d, Path.cwd())) for d in tried_rundirs])}')

    def parse_report(self, reportfile_path, re_pattern, *other_re_patterns, dotall=True):
        self.logger.debug(f"Parsing report file: {reportfile_path}")
        if not reportfile_path.exists():
            self.logger.critical(
                f'Report file: {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check `{self.run_dir / "stdout.log"}` and other log files in {self.run_dir} to find out what errors occured.'
            )
            sys.exit(1)
        with open(reportfile_path) as rpt_file:
            content = rpt_file.read()

            flags = re.MULTILINE | re.IGNORECASE
            if dotall:
                flags |= re.DOTALL

            def match_pattern(pat):
                match = re.search(pat, content, flags)
                if match is None:
                    return False
                match_dict = match.groupdict()
                for k, v in match_dict.items():
                    self.results[k] = try_convert(v)
                    self.logger.debug(f'{k}: {self.results[k]}')
                return True

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    self.logger.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched = match_pattern(subpat)
                        if matched:
                            self.logger.debug("subpat matched!")
                            break
                else:
                    self.logger.debug(f"Matching: {pat}")
                    matched = match_pattern(pat)

                if not matched:
                    sys.exit(f"Error parsing report file: {rpt_file.name}\n Pattern not matched: {pat}\n")

    def print_results(self, results=None):
        if not results:
            results = self.results
        for k, v in results.items():
            if not k.startswith('_'):
                if isinstance(v, float):
                    print(f'{k:20}{v:8.3f}')
                elif isinstance(v, int):
                    print(f'{k:20}{v:>8}')
                else:
                    print(f'{k:20}{v:>8s}')

    def dump_results(self, flow):
        # write only if not exists
        results_json = self.run_dir / f'{flow}_results.json'
        if results_json.exists():
            self.logger.warning(f"Overwriting existing results json file!")
        with open(results_json, 'w') as outfile:
            json.dump(self.results, outfile, indent=4)
        self.logger.info(f"Results written to {results_json}")
