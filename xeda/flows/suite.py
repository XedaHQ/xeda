# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import json
import os
import sys
import re
from pathlib import Path
import subprocess
from datetime import datetime
from xeda.plugins.lwc import LwcSimTiming
from jinja2 import Environment, PackageLoader, StrictUndefined
import multiprocessing
# import asyncio
from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner
import colored

from . import Settings, semantic_hash, try_convert, DesignSource


class Suite:
    """ A flow may run one or more tools and is associated with a single set of settings and a single design, after completion contains data """
    name = None
    executable = None
    supported_flows = []
    required_settings = {'synth': {'clock_period': float}}
    reports_subdir_name = 'reports'

    def __init__(self, settings, args, logger, **flow_defaults):
        self.args = args
        self.logger = logger
        self.nthreads = multiprocessing.cpu_count()
        # base flow defaults
        self.settings = Settings()
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
            autoescape=False,
            undefined=StrictUndefined
        )

        self.reports_dir = None
        self.timestamp = None
        self.run_hash = semantic_hash(self.settings)

        self.run_dir = self.get_run_dir(all_runs_dir=self.args.all_runs_dir,
                                        prefix='DSE_' if self.args.command == 'dse' else None, override=self.args.force_run_dir)

        # TODO implement plugins registration system, probably at a higher level
        self.plugins = []
        self.plugins.append(LwcSimTiming(self.run_dir, self.logger))

        if not isinstance(self.settings.design['sources'], list):
            sys.exit('`sources` section of the settings needs to be a list')
        for i, src in enumerate(self.settings.design['sources']):
            if not DesignSource.is_design_source(src):
                sys.exit(f'Entry `{src}` in `sources` needs to be a DesignSource JSON dictionary')
            self.settings.design['sources'][i] = DesignSource(**src)
            self.settings.design['sources'][i].file = self.conv_to_relative_path(
                self.settings.design['sources'][i].file)

        for gen_type in ['generics', 'tb_generics']:
            for gen_key, gen_val in self.settings.design[gen_type].items():
                if isinstance(gen_val, dict) and "file" in gen_val:
                    p = gen_val["file"]
                    assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
                    gen_val = self.conv_to_relative_path(p.strip())
                    self.logger.info(f'Converting generic `{gen_key}` marked as `file`: {p} -> {gen_val}')
                    self.settings.design[gen_type][gen_key] = gen_val

    def check_settings(self, flow):
        if flow in self.required_settings:
            for req_key, req_type in self.required_settings[flow].items():
                if req_key not in self.settings.flow:
                    self.logger.critical(f'{req_key} is required to be set for {self.name}:{flow}')
                    sys.exit(1)
                elif type(self.settings.flow[req_key]) != req_type:
                    self.logger.critical(f'{req_key} should have type `{req_type.__name__}` for {self.name}:{flow}')
                    sys.exit(1)
        else:
            self.logger.warn(f"{self.name} does not specify any required_settings for {flow}")

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
        # Must be implemented
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def parse_reports(self, flow):
        # Do nothing if not implemented
        pass

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
                                           nthreads=self.nthreads,
                                           debug=self.args.debug,
                                           reports_dir=self.reports_subdir_name,
                                           **attr)
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def conv_to_relative_path(self, src):
        path = Path(src).resolve()
        return os.path.relpath(path, self.run_dir)

    def fatal(self, msg):
        self.logger.critical(msg)
        sys.exit(1)

    def run(self, flow):
        if not flow:
            flow = self.supported_flows[0]  # first is the default flow
        else:
            if not (flow in self.supported_flows):
                sys.exit(f"Flow {flow} is not supported by {self.name}.")

        self.check_settings(flow)

        self.dump_settings()

        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")

        self.flow_stdout_log = f'{self.name}_{flow}_stdout.log'

        self.__runflow_impl__(flow)
        
        for plugin in self.plugins:
            plugin.post_run_hook()

        self.reports_dir = self.run_dir / self.reports_subdir_name
        if not self.reports_dir.exists():
            self.reports_dir.mkdir(parents=True)

        self.results = dict()
        self.parse_reports(flow)
        self.results['timestamp'] = self.timestamp
        if self.results:  # non empty
            self.print_results()
            self.dump_results(flow)

        for plugin in self.plugins:
            plugin.post_results_hook()

    def run_process(self, prog, prog_args, check=True, stdout_logfile=None, initial_step=None, force_echo=False):
        if not stdout_logfile:
            stdout_logfile = f'{prog}_stdout.log'
        proc = None
        spinner = None
        unicode = True
        verbose = not self.args.quiet and (self.args.verbose or force_echo)
        echo_instructed = False
        stdout_logfile = self.run_dir / stdout_logfile
        start_step_re = re.compile(r'^={12}=*\(\s*(?P<step>[^\)]+)\s*\)={12}=*')
        enable_echo_re = re.compile(r'^={12}=*\( \*ENABLE ECHO\* \)={12}=*')
        disable_echo_re = re.compile(r'^={12}=*\( \*DISABLE ECHO\* \)={12}=*')
        error_msg_re = re.compile(r'^\s*error:?\s+', re.IGNORECASE)
        warn_msg_re = re.compile(r'^\s*warning:?\s+', re.IGNORECASE)
        critwarn_msg_re = re.compile(r'^\s*critical\s+warning:?\s+', re.IGNORECASE)

        with open(stdout_logfile, 'w') as log_file:
            try:
                self.logger.info(f'Running `{prog} {" ".join(prog_args)}` in {self.run_dir}')
                self.logger.info(f'Standard output from the tool will be saved to {stdout_logfile}')
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
                    if initial_step:
                        spinner = Spinner('⏳' + initial_step if unicode else initial_step)
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
                            if error_msg_re.match(line) or critwarn_msg_re.match(line):
                                self.logger.error(line)
                            elif warn_msg_re.match(line):
                                self.logger.warning(line)
                            elif enable_echo_re.match(line):
                                if not self.args.quiet:
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
                f'Report file: {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check `{self.run_dir / "stdout.log"}` and other log files in {self.run_dir} to find out what errors occurred.'
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
        data_width = 32
        name_width = 80 - data_width
        hline = "-"*(name_width + data_width)
        print("\n" + hline)
        print(f"{'Results':^{name_width + data_width}s}")
        print(hline)
        for k, v in results.items():
            if not k.startswith('_'):
                if isinstance(v, float):
                    print(f'{k:{name_width}}{v:{data_width}.3f}')
                elif isinstance(v, bool):
                    bdisp = (colored.fg("green") + "✓" if v else colored.fg("red") + "✗") + colored.attr("reset")
                    print(f'{k:{name_width}}{bdisp:>{data_width}}')
                elif isinstance(v, int):
                    print(f'{k:{name_width}}{v:>{data_width}}')
                elif isinstance(v, list):
                    print(f'{k:{name_width}}{" ".join(v):<{data_width}}')
                else:
                    print(f'{k:{name_width}}{str(v):>{data_width}s}')
        print(hline + "\n")

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


class HasSimFlow():
    def simrun_match_regexp(self, regexp):
        success = False
        with open(self.run_dir / self.flow_stdout_log) as logf:
            if re.search(regexp, logf.read()):
                success = True
        self.results['success'] = success
