# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import json
import os
import sys
import re
from pathlib import Path
import subprocess
from datetime import datetime
from jinja2 import Environment, PackageLoader
import multiprocessing
# import asyncio

from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner

from . import Settings, semantic_hash, try_convert, DesignSource


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
        # default for optional design settings
        self.settings.design['generics'] = {}
        self.settings.design['tb_generics'] = {}
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
        self.timestamp = None
        self.run_hash = semantic_hash(self.settings)

        self.run_dir = self.get_run_dir(all_runs_dir=self.args.all_runs_dir,
                                        prefix='FMAX_' if self.args.command == 'fmax' else None, override=self.args.force_run_dir)

        if not isinstance(self.settings.design['sources'], list):
            sys.exit('`sources` section of the settings needs to be a list')
        for i, src in enumerate(self.settings.design['sources']):
            if not DesignSource.is_design_source(src):
                sys.exit(f'Entry `{src}` in `sources` needs to be a DesignSource JSON dictionay')
            self.settings.design['sources'][i] = DesignSource(**src)
            self.settings.design['sources'][i].file = self.conv_to_relative_path(
                self.settings.design['sources'][i].file)

    def get_run_dir(self, all_runs_dir=None, prefix=None, override=None):
        if not all_runs_dir:
            all_runs_dir = Path.cwd() / f'{self.name}_run'

        if override:
            run_dir = Path(override).resolve()
        else:

            subdir = self.run_hash
            if prefix:
                subdir = prefix + subdir

            run_dir = all_runs_dir / subdir

        if not run_dir.exists():
            run_dir.mkdir(parents=True)
        else:
            self.logger.warning(f'Using existing run directory: {run_dir}')

        assert run_dir.is_dir()

        return run_dir

    def __runflow_impl__(self, flow):
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def parse_reports(self, flow):
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def dump_settings(self):
        effective_settings_json = self.run_dir / f'settings.json'
        self.logger.info(f'dumping effective settings to {effective_settings_json}')
        self.dump_json(self.settings, effective_settings_json)

    def copy_from_template(self, resource_name, **attr):
        template = self.jinja_env.get_template(resource_name)
        script_path = self.run_dir / resource_name
        self.logger.debug(f'generating {script_path.resolve()} from template.')
        rendered_content = template.render(flow=self.settings.flow,
                                           design=self.settings.design,
                                           nthreads=multiprocessing.cpu_count(),
                                           debug=self.args.debug,
                                           **attr)
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def conv_to_relative_path(self, src):
        path = Path(src).resolve()
        return os.path.relpath(path, self.run_dir)

    def run(self, flow):
        if not flow:
            flow = self.supported_flows[0]  # first is the default flow
        else:
            if not (flow in self.supported_flows):
                sys.exit(f"Flow {flow} is not supported by {self.name}.")

        self.dump_settings()

        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.__runflow_impl__(flow)
        # clear results in case anything lingering
        self.results = dict()
        self.parse_reports(flow)
        self.results['timestamp'] = self.timestamp
        if self.results:  # non empty
            self.print_results()
            self.dump_results(flow)

    def run_process(self, prog, prog_args, check=True):
        proc = None
        spinner = None
        unicode = True
        verbose = self.args.verbose
        echo_instructed = False
        stdout_logfile = self.run_dir / 'stdout.log'
        start_step_re = re.compile(r'^={12}=*\(\s*(?P<step>[^\)]+)\s*\)={12}=*')
        enable_echo_re = re.compile(r'^={12}=*\( \*ENABLE ECHO\* \)={12}=*')
        disable_echo_re = re.compile(r'^={12}=*\( \*DISABLE ECHO\* \)={12}=*')
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
                        if verbose or echo_instructed:
                            if echo_instructed and disable_echo_re.match(line):
                                echo_instructed = False
                            else:
                                print(line, end='')
                        else:
                            if enable_echo_re.match(line):
                                echo_instructed = True
                                end_step()
                            else:
                                match = start_step_re.match(line)
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

    def parse_report(self, reportfile_path, re_pattern, *other_re_patterns, dotall=True):
        self.logger.debug(f"Parsing report file: {reportfile_path}")
        # TODO fix debug and verbosity levels!
        high_debug = self.args.verbose
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
                    if high_debug:
                        self.logger.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched = match_pattern(subpat)
                        if matched:
                            if high_debug:
                                self.logger.debug("subpat matched!")
                            break
                else:
                    if high_debug:
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
                    print(f'{k:32}{v:12.3f}')
                elif isinstance(v, int):
                    print(f'{k:32}{v:>12}')
                else:
                    print(f'{k:32}{v:>12s}')

    def dump_json(self, data, path, overwrite=True):
        if path.exists():
            if overwrite:
                self.logger.warning(f"Overwriting existing file: {path}!")
            else:
                self.logger.critical(f"{path} already exists! Not overwriting!")
                sys.exit("Exiting due to error!")
        with open(path, 'w') as outfile:
            json.dump(data, outfile, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else x.__str__, indent=4)

    def dump_results(self, flow):
        # write only if not exists
        path = self.run_dir / f'{flow}_results.json'
        self.dump_json(self.results, path)
        self.logger.info(f"Results written to {path}")
