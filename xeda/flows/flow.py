# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
from datetime import datetime
import json
from logging.handlers import QueueHandler
import os
import sys
import re
from pathlib import Path
import subprocess
import time
from .settings import Settings
from jinja2 import Environment, PackageLoader, StrictUndefined
import multiprocessing
import logging
from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner
import colored

from ..utils import camelcase_to_snakecase, try_convert

from pathlib import Path

from typing import Union, Dict, List
import hashlib

JsonType = Union[str, int, float, bool, List['JsonType'], 'JsonTree']
JsonTree = Dict[str, JsonType]
StrTreeType = Union[str, List['StrTreeType'], 'StrTree']
StrTree = Dict[str, StrTreeType]

class FlowFatalException(Exception):
    """Fatal error"""
    pass

logger = logging.getLogger()

def my_print(*args, **kwargs):
    print(*args, **kwargs)

class Flow():
    """ A flow may run one or more tools and is associated with a single set of settings and a single design. """
    required_settings = {}
    default_settings = {}
    reports_subdir_name = 'reports'

    def __init__(self, settings: Settings, args):

        
        self.args = args
        self.run_hash = None
        self.settings = settings

        # all design flow-critical settings are fixed from this point onwards
        self.set_run_dir(prefix=None, override=self.args.force_run_dir)

        self.results = dict()
        self.jinja_env = Environment(
            loader=PackageLoader(self.flow_module_path, 'templates'),
            autoescape=False,
            undefined=StrictUndefined
        )

        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.reports_dir = None

        self.flow_stdout_log = f'{self.name}_stdout.log'

        self.no_console = args.debug

        self.init_time = time.monotonic()

        self.post_run_hooks = []
        self.post_results_hooks = []


    def set_hash(self):
        skip_fields = {'author', 'url', 'comment', 'description', 'license'}

        def semantic_hash(data: JsonTree, hash_files=True, hasher=hashlib.sha1) -> str:
            def get_digest(b: bytes):
                return hasher(b).hexdigest()[:32]

            def file_digest(filename: str):
                try:
                    with open(filename, 'rb') as f:
                        return get_digest(f.read())
                except FileNotFoundError as e:
                    raise e  # TODO add logging here?

            def sorted_dict_str(data: JsonType) -> StrTreeType:
                if type(data) == dict:
                    return {k: sorted_dict_str(file_digest(data[k]) if hash_files and (k == 'file') else data[k]) for k in sorted(data.keys()) if not k in skip_fields}
                elif type(data) == list:
                    return [sorted_dict_str(val) for val in data]
                elif hasattr(data, '__dict__'):
                    return sorted_dict_str(data.__dict__)
                else:
                    return str(data)

            return get_digest(bytes(repr(sorted_dict_str(data)), 'UTF-8'))

        try:
            self.run_hash = semantic_hash(self.settings)
        except FileNotFoundError as e:
            self.fatal(f"Semantic hash failed: {e} ")

    def set_parallel_run(self, queue):
        self.no_console = True
        # while logger.hasHandlers():
        #     logger.removeHandler(logger.handlers[0])
        # logger = multiprocessing.get_logger()

        # logger.setLevel(logging.DEBUG)
        # formatter = logging.Formatter(
        #     '[%(asctime)s| %(levelname)s| %(processName)s] %(message)s')
        # handler = logging.FileHandler(self.run_dir / f'{self.name}_logger.log')
        # handler = NullHandler()
        # handler.setFormatter(formatter)
        # handler = QueueHandler(queue)

        # this bit will make sure you won't have
        # duplicated messages in the output
        # if not len(logger.handlers):
        # logger.addHandler(handler)
        # logger = logger

    @property
    def name(self):
        return camelcase_to_snakecase(self.__class__.__name__)

    @property
    def flow_module_path(self):
        return self.__module__

    def check_settings(self, flow):
        if flow in self.required_settings:
            for req_key, req_type in self.required_settings[flow].items():
                if req_key not in self.settings.flow:
                    self.fatal(f'{req_key} is required to be set for {self.name}:{flow}')
                elif type(self.settings.flow[req_key]) != req_type:
                    self.fatal(f'{req_key} should have type `{req_type.__name__}` for {self.name}:{flow}')
        else:
            logger.warn(f"{self.name} does not specify any required_settings for {flow}")

    def set_run_dir(self, prefix=None, override=None):
        # all design flow-critical settings are fixed from this point onwards
        self.set_hash()

        all_runs_dir = Path(self.args.xeda_run_dir) / self.name

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
            logger.warning(f'Using existing run directory: {run_dir}')

        assert run_dir.is_dir()

        self.run_dir = run_dir

    def run(self):
        # Must be implemented
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def parse_reports(self):
        # Do nothing if not implemented
        pass

    def dump_settings(self):
        effective_settings_json = self.run_dir / f'settings.json'
        logger.info(f'dumping effective settings to {effective_settings_json}')
        self.dump_json(self.settings, effective_settings_json)

    def copy_from_template(self, resource_name, **attr):
        template = self.jinja_env.get_template(resource_name)
        script_path = self.run_dir / resource_name
        logger.debug(f'generating {script_path.resolve()} from template.')
        rendered_content = template.render(flow=self.settings.flow,
                                           design=self.settings.design,
                                           nthreads=self.settings.nthreads,
                                           debug=self.args.debug,
                                           reports_dir=self.reports_subdir_name,
                                           **attr)
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def conv_to_relative_path(self, src):
        path = Path(src).resolve(strict=True)
        return os.path.relpath(path, self.run_dir)

    def fatal(self, msg):
        logger.critical(msg)
        raise FlowFatalException(msg)

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

        def make_spinner(step):
            if self.no_console:
                return None
            return Spinner('⏳' + step if unicode else step)
        with open(stdout_logfile, 'w') as log_file:
            try:
                logger.info(f'Running `{prog} {" ".join(prog_args)}` in {self.run_dir}')
                if not self.args.debug:
                    logger.info(f'Standard output from the tool will be saved to {stdout_logfile}')
                with subprocess.Popen([prog, *prog_args],
                                      cwd=self.run_dir,
                                      stdout=None if self.args.debug else subprocess.PIPE,
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
                    if not self.args.debug:
                        if initial_step:
                            spinner = make_spinner(initial_step)
                        while True:
                            line = proc.stdout.readline()
                            if not line:
                                end_step()
                                break
                            log_file.write(line)
                            log_file.flush()
                            if verbose or echo_instructed:
                                if disable_echo_re.match(line):
                                    echo_instructed = False
                                else:
                                    print(line, end='')
                            else:
                                if error_msg_re.match(line) or critwarn_msg_re.match(line):
                                    if spinner:
                                        print()
                                    logger.error(line)
                                elif warn_msg_re.match(line):
                                    if spinner:
                                        print()
                                    logger.warning(line)
                                elif enable_echo_re.match(line):
                                    if not self.args.quiet:
                                        echo_instructed = True
                                    end_step()
                                else:
                                    match = start_step_re.match(line)
                                    if match:
                                        end_step()
                                        step = match.group('step')
                                        spinner = make_spinner(step)
                                    else:
                                        if spinner:
                                            spinner.next()
            except FileNotFoundError as e:
                self.fatal(f"Cannot execute `{prog}`. Make sure it's properly instaulled and the executable is in PATH")
            except KeyboardInterrupt as e:
                if spinner:
                    print(SHOW_CURSOR)
                logger.critical("Received a keyboard interrupt!")
                if proc:
                    logger.critical(f"Terminating {proc.args[0]}[{proc.pid}]")
                    proc.terminate()
                    proc.kill()
                    proc.wait()
                raise e

        if spinner:
            print(SHOW_CURSOR)

        if proc.returncode != 0:
            logger.critical(
                f'`{proc.args[0]}` exited with returncode {proc.returncode}. Please check `{stdout_logfile}` for error messages!')
            if check:
                self.fatal('Non-zero exit code')
        else:
            logger.info(f'Process completed with returncode {proc.returncode}')
        return proc

    def parse_report(self, reportfile_path, re_pattern, *other_re_patterns, dotall=True):
        logger.debug(f"Parsing report file: {reportfile_path}")
        # TODO fix debug and verbosity levels!
        high_debug = self.args.verbose
        if not reportfile_path.exists():
            self.fatal(
                f'Report file: {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check `{self.run_dir / "stdout.log"}` and other log files in {self.run_dir} to find out what errors occurred.'
            )
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
                    logger.debug(f'{k}: {self.results[k]}')
                return True

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    if high_debug:
                        logger.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched = match_pattern(subpat)
                        if matched:
                            if high_debug:
                                logger.debug("subpat matched!")
                            break
                else:
                    if high_debug:
                        logger.debug(f"Matching: {pat}")
                    matched = match_pattern(pat)

                if not matched:
                    self.fatal(f"Error parsing report file: {rpt_file.name}\n Pattern not matched: {pat}\n")

    def print_results(self, results=None):
        if not results:
            results = self.results
            #init to print_results time:
            results['runtime_minutes'] = (time.monotonic() - self.init_time) / 60
        data_width = 32
        name_width = 80 - data_width
        hline = "-"*(name_width + data_width)

        my_print("\n" + hline)
        my_print(f"{'Results':^{name_width + data_width}s}")
        my_print(hline)
        for k, v in results.items():
            if not k.startswith('_'):
                if isinstance(v, float):
                    my_print(f'{k:{name_width}}{v:{data_width}.3f}')
                elif isinstance(v, bool):
                    bdisp = (colored.fg("green") + "✓" if v else colored.fg("red") + "✗") + colored.attr("reset")
                    my_print(f'{k:{name_width}}{bdisp:>{data_width}}')
                elif isinstance(v, int):
                    my_print(f'{k:{name_width}}{v:>{data_width}}')
                elif isinstance(v, list):
                    my_print(f'{k:{name_width}}{" ".join(v):<{data_width}}')
                else:
                    my_print(f'{k:{name_width}}{str(v):>{data_width}s}')
        my_print(hline + "\n")

    def dump_json(self, data, path, overwrite=True):
        if path.exists():
            if overwrite:
                logger.warning(f"Overwriting existing file: {path}")
            else:
                self.fatal(f"{path} already exists! Not overwriting!")
        with open(path, 'w') as outfile:
            json.dump(data, outfile, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else str(x), indent=4)

    def dump_results(self):
        # write only if not exists
        path = self.run_dir / f'{self.name}_results.json'
        self.dump_json(self.results, path)
        logger.info(f"Results written to {path}")

    def stdout_search_re(self, regexp):
        with open(self.run_dir / self.flow_stdout_log) as logf:
            if re.search(regexp, logf.read()):
                return True
        return False


class SimFlow(Flow):
    required_settings = {'vcd': str}

    # TODO FIXME move to plugin
    def parse_reports(self):
        failed = self.stdout_search_re('FAIL (1): SIMULATION FINISHED')
        passed = self.stdout_search_re('PASS (0): SIMULATION FINISHED')
        self.results['success'] = passed and not failed


class SynthFlow(Flow):
    required_settings = {'clock_period': float}

    # TODO FIXME set in plugin or elsewhere!!!
    default_settings = {'use_dsp': False, 'use_bram': False}
    pass


class DseFlow(Flow):
    pass


class DesignSource:
    @classmethod
    def is_design_source(cls, src):
        return isinstance(src, cls) or (isinstance(src, dict) and 'file' in src)

    def __init__(self, file: str, type: str = None, sim_only: bool = False, standard: str = None, variant: str = None, comment: str = None) -> None:
        def type_from_suffix(file: Path) -> str:
            type_variants_map = {
                ('vhdl', variant): ['vhd', 'vhdl'],
                ('verilog', variant): ['v'],
                ('verilog', 'systemverilog'): ['sv'],
                ('bsv', variant): ['bsv'],
                ('bs', variant): ['bs'],
            }
            for h, suffixes in type_variants_map.items():
                if file.suffix[1:] in suffixes:
                    return h
            return None, None

        file = Path(file)

        self.file: Path = file
        self.type, self.variant = (type, variant) if type else type_from_suffix(file)
        self.sim_only = sim_only
        self.standard = standard
        self.comment = comment

    def __str__(self):
        return str(self.file)

    def mk_relative(self, base):
        path = Path(self.file).resolve(strict=True)
        self.file = path  # os.path.relpath(path, base)
        return self
