import copy
from functools import partial
import multiprocessing
from multiprocessing import cpu_count
from types import SimpleNamespace
from typing import List, Mapping
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool
import os
import sys
import inspect
import logging
from concurrent.futures import CancelledError, TimeoutError
import time
import pkg_resources
from pathlib import Path
import json
import multiprocessing as mp
import traceback
# heavy, but will probably become handy down the road
import numpy
# import psutil
import tomlkit

from ..debug import DebugLevel
from ..plugins.lwc import LwcCheckTimingHook
from ..flows.settings import Settings
from ..flows.flow import DesignSource, FileResource, Flow, FlowFatalException, SynthFlow, my_print
from ..utils import camelcase_to_snakecase, load_class, dict_merge, try_convert

logger = logging.getLogger()


def tomlkit_to_popo(d):
    try:
        result = getattr(d, "value")
    except AttributeError:
        result = d

    if isinstance(result, list):
        result = [tomlkit_to_popo(x) for x in result]
    elif isinstance(result, dict):
        result = {
            tomlkit_to_popo(key): tomlkit_to_popo(val) for key, val in result.items()
        }
    elif isinstance(result, tomlkit.items.Integer):
        result = int(result)
    elif isinstance(result, tomlkit.items.Float):
        result = float(result)
    elif isinstance(result, tomlkit.items.String):
        result = str(result)
    elif isinstance(result, tomlkit.items.Bool):
        result = bool(result)

    return result


def print_results(results, title, subset):
    data_width = 32
    name_width = 80 - data_width
    hline = "-"*(name_width + data_width)

    my_print("\n" + hline)
    my_print(f"{title:^{name_width + data_width}s}")
    my_print(hline)
    for k, v in results.items():
        if not k.startswith('_') and (not subset or k in subset):
            if isinstance(v, float):
                my_print(f'{k:{name_width}}{v:{data_width}.6f}')
            elif isinstance(v, bool):
                my_print(f'{k:{name_width}}{"True" if v else "False":>{data_width}}')
            elif isinstance(v, int):
                my_print(f'{k:{name_width}}{v:>{data_width}}')
            elif isinstance(v, list):
                my_print(f'{k:{name_width}}{" ".join(v):<{data_width}}')
            else:
                my_print(f'{k:{name_width}}{str(v):>{data_width}s}')
    my_print(hline + "\n")


def run_flow(f: Flow):
    try:
        f.run()
        return f.results
    except FlowFatalException as e:
        logger.critical(f'Fatal exception during flow run in {f.flow_run_dir}: {e}')
        traceback.print_exc()
    except KeyboardInterrupt as e:
        logger.critical(f'KeyboardInterrupt received during flow run in {f.flow_run_dir}: {e}')
        traceback.print_exc()


def run_flow_fmax(arg):
    idx, f = arg
    try:
        f.run_flow()
        return idx
    except FlowFatalException as e:
        logger.critical(f'Fatal exception during flow run in {f.run_dir}: {e}')
        traceback.print_exc()
    except KeyboardInterrupt as e:
        logger.critical(f'KeyboardInterrupt recieved during flow run in {f.run_dir}: {e}')
        traceback.print_exc()


class FlowRunner():
    @classmethod
    def register_subparser(cls, subparsers):
        raise NotImplementedError

    def __init__(self, args, timestamp) -> None:
        self.args = args
        self.timestamp = timestamp
        # in case super().add_common_args(plug_parser) was not called in a subclass
        if not hasattr(args, 'override_settings'):
            self.args.override_settings = None

        self.parallel_run = None

    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string('xeda', "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            self.fatal(f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}", e)

    def fatal(self, msg=None, exception=None):
        if msg:
            logger.critical(msg)
        if exception:
            raise exception
        else:
            raise Exception(msg)

    def validate_settings(self, settings):
        assert 'design' in settings
        # design = settings['design']
        # assert 'sources' in design
        # assert 'vhdl_std' in design
        # if design['vhdl_std'] == 8:
        #     design['vhdl_std'] = "08"
        # elif design['vhdl_std'] == 2:
        #     design['vhdl_std'] = "02"

        return settings

    def get_design_settings(self, toml_path=None):
        if not toml_path:
            toml_path = self.args.xeda_project if self.args.xeda_project else Path.cwd() / 'xedaproject.toml'

        settings = self.get_default_settings()

        def get_design(d):
            if not isinstance(d, list):
                return d
            if len(d) == 1:
                return d[0]
            dname = self.args.design
            if dname:
                if isinstance(dname, list):
                    dname = dname[0]  # TODO FIXME match dname !!!!
                for x in d:
                    if x['name'] == dname:
                        return x
                logger.critical(f'Design "{dname}" not found in the current project.')
            else:
                logger.critical(
                    f'{len(d)} designs are availables in the current project. Please specify target design using --design')
            logger.critical(f'Available designs: {", ".join([x["name"] for x in d])}')
            self.fatal()

        try:
            with open(toml_path) as f:
                xeda_project_settings = tomlkit_to_popo(tomlkit.loads(f.read()))

            design_settings = dict(design=get_design(
                xeda_project_settings['design']), flows=xeda_project_settings.get('flows', {}))

            settings = dict_merge(settings, design_settings)
            logger.info(f"Using design settings from {toml_path}")

        except FileNotFoundError as e:
            self.fatal(
                f'Cannot open project file: {toml_path}. Please specify the correct path using --xeda-project', e)
        except IsADirectoryError as e:
            self.fatal(f'The specified design json is not a regular file.', e)

        if self.args.override_settings:
            for override in self.args.override_settings:
                key, val = override.split('=')
                hier = key.split('.')
                patch = dict()
                current_dict = patch
                for field in hier[:-1]:
                    new_dict = dict()
                    current_dict[field] = new_dict
                    current_dict = new_dict
                current_dict[hier[-1]] = try_convert(val, convert_lists=True)
                settings = dict_merge(settings, patch, True)

        # settings = SimpleNamespace(**settings)

        return self.validate_settings(settings)

    # should not override
    def post_run(self, flow: Flow, print_failed=True):
        # Run post-run hooks
        for hook in flow.post_run_hooks:
            logger.info(f"Running post-run hook from from {hook.__class__.__name__}")
            hook(flow)

        flow.reports_dir = flow.flow_run_dir / flow.reports_subdir_name
        if not flow.reports_dir.exists():
            flow.reports_dir.mkdir(parents=True)

        flow.parse_reports()
        flow.results['timestamp'] = flow.timestamp
        flow.results['design.name'] = flow.settings.design['name']
        flow.results['flow.name'] = flow.name
        flow.results['flow.run_hash'] = flow.design_run_hash

        if print_failed or flow.results.get('success'):
            flow.print_results()
        flow.dump_results()

        # Run post-results hooks
        for hook in flow.post_results_hooks:
            logger.info(f"Running post-results hook from {hook.__class__.__name__}")
            hook(flow)

    def load_flow_class(self, flow_name_or_class):
        if inspect.isclass(flow_name_or_class) and issubclass(flow_name_or_class, Flow):
            return flow_name_or_class
        try:
            return load_class(flow_name_or_class, ".flows")
        except AttributeError as e:
            self.fatal(
                f"Could not find Flow class corresponding to {flow_name_or_class}. Make sure it's typed correctly.", e)

    def setup_flow(self, settings, flow_name_or_class, max_threads=None):
        if not max_threads:
            max_threads = multiprocessing.cpu_count()
        # settings is a ref to a dict and its data can change, take a snapshot
        settings = copy.deepcopy(settings)

        flow_cls = self.load_flow_class(flow_name_or_class)

        flow_name = flow_name_or_class if isinstance(flow_name_or_class, str) else flow_name_or_class.name

        flow_settings = Settings()

        # flow defaults
        flow_settings.flow.update(**flow_cls.default_settings)

        # override sections
        flow_settings.design.update(settings['design'])

        if flow_cls.depends_on:
            for dep,subsettings in flow_cls.depends_on.items():
                flow_settings.flow_depends[dep.name] = settings['flows'].get(dep.name, {})
                flow_settings.flow_depends[dep.name].update(subsettings)
                
        # override entire section if available in settings
        if flow_name in settings['flows']:
            flow_settings.flow.update(settings['flows'][flow_name])
            logger.info(f"Using {flow_name} settings")
        else:
            logger.warning(f"No settings found for {flow_name}")


                

        flow: Flow = flow_cls(flow_settings, self.args)

        flow.nthreads = int(max(1, max_threads))

        return flow

    def add_common_args(parser):
        # TODO load flow and plugin classes from custom packages, i.e. xeda_plugins.flows etc
        flow_classes = inspect.getmembers(sys.modules['xeda.flows'],
                                          lambda cls: inspect.isclass(cls) and issubclass(cls, Flow))
        registered_flows = [camelcase_to_snakecase(n) for n, c in flow_classes]
        parser.add_argument(
            '--xeda-project',
            default=None,
            help='Path to Xeda project file. By default will use xeda.toml in the current directory.'
        )
        parser.add_argument('flow', metavar='FLOW_NAME', choices=registered_flows,
                            help=f'Flow name. Registered flows are: {registered_flows}')
        parser.add_argument('--override-settings', nargs='+',
                            help='Override certain setting value. Use <hierarchy>.key=value format'
                            'example: --override-settings flows.vivado_run.stop_time=100us')
        parser.add_argument(
            '--design',
            nargs='+',
            help='Specify design.name in case multiple designs are available in the Xeda project.'
        )


class DefaultFlowRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        run_parser = subparsers.add_parser('run', help='Run a flow')
        super().add_common_args(run_parser)

    def launch(self):
        settings = self.get_design_settings()
        flow = self.setup_flow(settings, self.args.flow)
        if flow.depends_on:
            assert isinstance(
                flow.depends_on, Mapping), "flow.depends_on should be a mapping of DependentFlowClass -> settings"

            flow.set_run_dir()

            for fcls, sub_settings in flow.depends_on.items():
                gen_rtl_sources = sub_settings.get('rtl.sources')
                if gen_rtl_sources:
                    run_required = False
                    for gen_src in gen_rtl_sources:
                        gen_src_path = flow.run_path / fcls.name / gen_src
                        if not gen_src_path.exists():
                            run_required = True
                            logger.warning(f'Need to run {fcls.__name__} to generate {gen_src_path}')
                            break

                    dep_flow = self.setup_flow(settings, fcls)
                    dep_flow.run_path = flow.run_path
                    dep_flow.set_run_dir()
                    if run_required:
                        dep_flow.run_flow()
                        self.post_run(dep_flow)
                        assert dep_flow.results['success']

                    flow.settings.design['rtl']['sources'] = [
                        DesignSource(dep_flow.flow_run_dir / src) for src in gen_rtl_sources
                    ]

                    logger.info(
                        f"Setting {flow.name}.rtl.sources to {[ str(x) for x in flow.settings.design['rtl']['sources']]}")
        flow.run_flow()
        self.post_run(flow)


# TODO as a plugin
class LwcVariantsRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        plug_parser = subparsers.add_parser('run_variants', help='Run All LWC variants in variants.json')
        super().add_common_args(plug_parser)
        plug_parser.add_argument(
            '--variants-json',
            default='variants.json',
            help='Path to LWC variants JSON file.'
        )
        # TODO optionally get nproc from user
        plug_parser.add_argument(
            '--parallel-run',
            action='store_true',
            help='Use multiprocessing to run multiple flows in parallel'
        )
        plug_parser.add_argument(
            '--gmu-kats',
            action='store_true',
            help='Run simulation with different GMU KAT files'
        )
        plug_parser.add_argument(
            '--auto-copy',
            action='store_true',
            help='In gmu-kats mode, automatically copy files. Existing files will be silently REPLACED, so use with caution!'
        )
        plug_parser.add_argument(
            '--no-reuse-key',
            action='store_true',
            help='Do not inlucde reuse-key testvectors'
        )
        plug_parser.add_argument(
            '--no-timing',
            action='store_true',
            help='disable timing mode'
        )
        plug_parser.add_argument(
            '--variants-subset',
            nargs='+',
            help='The list of variant IDs to run from all available variants loaded from variants.json.'
        )

    def launch(self):
        args = self.args
        self.parallel_run = args.parallel_run
        if args.parallel_run and args.debug >= DebugLevel.MEDIUM:
            self.parallel_run = False
            logger.warning("parallel_run disabled due to the debug level")

        total = 0
        num_success = 0

        variants_json = Path(args.variants_json).resolve()
        variants_json_dir = os.path.dirname(variants_json)

        logger.info(f'LwcVariantsRunner: loading variants data from {variants_json}')
        with open(variants_json) as vjf:
            variants = json.load(vjf)

        if args.variants_subset:
            variants = {vid: vdat for vid, vdat in variants.items() if vid in args.variants_subset}

        flows_to_run: List[Flow] = []

        nproc = max(1, multiprocessing.cpu_count() // 4)

        common_kats = ['kats_for_verification', 'generic_aead_sizes_new_key']
        if not args.no_reuse_key:
            common_kats += ['generic_aead_sizes_reuse_key']

        hash_kats = ['basic_hash_sizes', 'blanket_hash_test']

        def add_flow(settings, variant_id, variant_data):
            flow = self.setup_flow(settings, args.flow, max_threads=multiprocessing.cpu_count() // nproc // 2)
            if not args.no_timing:
                flow.post_results_hooks.append(LwcCheckTimingHook(variant_id, variant_data))
            flows_to_run.append(flow)

        for variant_id, variant_data in variants.items():
            logger.info(f"LwcVariantsRunner: running variant {variant_id}")
            # path is relative to variants_json

            design_json_path = Path(variants_json_dir) / variant_data["design"]  # TODO also support inline design
            settings = self.get_design_settings(design_json_path)

            if self.parallel_run:
                args.quiet = True

            if not args.no_timing:
                settings['design']['tb_generics']['G_TEST_MODE'] = 4
            settings['design']['tb_generics']['G_FNAME_TIMING'] = f"timing_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_TIMING_CSV'] = f"timing_{variant_id}.csv"
            settings['design']['tb_generics']['G_FNAME_RESULT'] = f"result_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_FAILED_TVS'] = f"failed_test_vectors_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_LOG'] = f"lwctb_{variant_id}.log"

            if args.gmu_kats:
                kats = common_kats
                if "HASH" in variant_data["operations"]:
                    kats = common_kats + hash_kats
                for kat in kats:
                    settings["design"]["tb_generics"]["G_FNAME_DO"] = {"file": f"KAT_GMU/{variant_id}/{kat}/do.txt"}
                    settings["design"]["tb_generics"]["G_FNAME_SDI"] = {"file": f"KAT_GMU/{variant_id}/{kat}/sdi.txt"}
                    settings["design"]["tb_generics"]["G_FNAME_PDI"] = {"file": f"KAT_GMU/{variant_id}/{kat}/pdi.txt"}

                    this_variant_data = copy.deepcopy(variant_data)
                    this_variant_data["kat"] = kat
                    add_flow(settings, variant_id, this_variant_data)
            else:
                add_flow(settings, variant_id, variant_data)

        if not flows_to_run:
            self.fatal("flows_to_run is empty!")

        proc_timeout_seconds = flows_to_run[0].settings.flow.get('timeout', flows_to_run[0].timeout)

        if self.parallel_run:
            try:
                with mp.Pool(processes=min(nproc, len(flows_to_run))) as p:
                    p.map_async(run_flow, flows_to_run).get(proc_timeout_seconds)
            except KeyboardInterrupt as e:
                logger.critical(f'KeyboardInterrupt received parallel execution of runs: {e}')
                traceback.print_exc()
                logger.warning("trying to recover completed flow results...")
        else:
            for flow in flows_to_run:
                flow.run()

        try:
            for flow in flows_to_run:
                self.post_run(flow)
                total += 1
                if flow.results.get('success'):
                    num_success += 1
        except Exception as e:
            logger.critical("Exception during post_run")
            raise e
        finally:
            for flow in flows_to_run:
                logger.info(f"Run: {flow.flow_run_dir} {'[PASS]' if flow.results.get('success') else '[FAIL]'}")
            logger.info(f'{num_success} out of {total} runs succeeded.')


class Best:
    def __init__(self, freq, results, settings):
        self.freq = freq
        self.results = copy.deepcopy(results)
        self.settings = copy.deepcopy(settings)


def nukemall():
    def on_terminate(proc):
        logger.warning(f"Child process {proc.info['name']}[{proc}] terminated with exit code {proc.returncode}")

    try:
        pass
        # procs = psutil.Process().children(recursive=True)
        # print(f"killing {len(procs)} child processes")
        # for p in procs:
        #     p.terminate()
        # gone, alive = psutil.wait_procs(procs, timeout=3, callback=on_terminate)
        # for p in alive:
        #     p.kill()
        # on nix: negative number means the process group with that PGID
        # os.kill(-os.getpgid(0), signal.SIGINT)
    except:
        logger.exception('exception during killing')


class LwcFmaxRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        # command should be set automatically from top and using class help, etc
        plug_parser = subparsers.add_parser('run_fmax', help='find fmax')
        super().add_common_args(plug_parser)
        plug_parser.add_argument(
            '--max-failed-runs',
            default=10, type=int,
            help='Maximum consequetive failed runs allowed. Give up afterwards.'
        )
        plug_parser.add_argument(
            '--start-max-freq',
            default=600, type=float,
        )
        plug_parser.add_argument(
            '--max-cpus',
            default=max(1, cpu_count()), type=int,
        )

    def launch(self):
        start_time = time.monotonic()

        args = self.args
        settings = self.get_design_settings()

        flow_name = args.flow

        flow_settings = settings['flows'].get(flow_name)

        # will try halfing max_no_improvements iterations if all runs have failed
        lo_freq = float(flow_settings.get('fmax_low_freq', 1.0))
        # can go higher
        hi_freq = float(flow_settings.get('fmax_high_freq', 600.0))
        resolution = 0.5
        max_no_improvements = 2
        delta_increment = resolution / 2

        ONE_THOUSAND = 1000.0

        nthreads = int(flow_settings.get('nthreads', 4))

        num_workers = max(2, args.max_cpus // nthreads)
        logger.info(f'nthreads={nthreads} num_workers={num_workers}')
        self.parallel_run = True
        args.quiet = True

        best = None
        rundirs = []
        all_results = []
        future = None
        num_iterations = 0
        pool = None
        no_improvements = 0

        def round_freq_to_ps(freq: float) -> float:
            period = round(ONE_THOUSAND / freq, 3)
            return ONE_THOUSAND / period
        try:
            with ProcessPool(max_workers=num_workers) as pool:
                while hi_freq - lo_freq >= resolution:
                    frequencies_to_try, freq_step = numpy.linspace(
                        lo_freq, hi_freq, num=num_workers, dtype=float, retstep=True)

                    frequencies_to_try = [round_freq_to_ps(f) for f in frequencies_to_try]

                    logger.info(
                        f"[Fmax] Trying following frequencies (MHz): {[f'{freq:.2f}' for freq in frequencies_to_try]}")

                    flows_to_run = []
                    for freq in frequencies_to_try:
                        flow_settings['clock_period'] = round(ONE_THOUSAND / freq, 3)
                        flow = self.setup_flow(settings, flow_name, max_threads=nthreads)
                        flow.set_parallel_run()
                        flows_to_run.append(flow)

                    proc_timeout_seconds = flow_settings.get('timeout', flows_to_run[0].timeout)

                    logger.info(f'[Fmax] Timeout set to: {proc_timeout_seconds} seconds.')

                    future = pool.map(run_flow_fmax, enumerate(flows_to_run), timeout=proc_timeout_seconds)
                    num_iterations += 1

                    improved_idx = None

                    try:
                        iterator = future.result()
                        while True:
                            try:
                                idx = next(iterator)
                                flow = flows_to_run[idx]
                                freq = frequencies_to_try[idx]
                                self.post_run(flow, print_failed=False)
                                results = flow.results
                                rundirs.append(flow.run_dir)
                                if results['success'] and (not best or freq > best.freq):
                                    all_results.append(results)
                                    best = Best(freq, results, flow.settings)
                                    improved_idx = idx
                            except StopIteration:
                                break
                            except TimeoutError as e:
                                logger.critical(
                                    f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks.")
                                future.cancel()
                            except ProcessExpired as e:
                                logger.critical(f"{e}. Exit code: {e.exitcode}")
                    except CancelledError:
                        logger.warning("[Fmax] CancelledError")
                    except KeyboardInterrupt:
                        pool.stop()
                        pool.join()
                        raise

                    if freq_step < resolution * 0.9:
                        break

                    if not best or improved_idx is None:
                        no_improvements += 1
                        if no_improvements >= max_no_improvements:
                            logger.info(
                                f"Stopping as there were no improvements in {no_improvements} consequetive iterations.")
                            break
                        logger.info(f"No improvements during this iteration.")

                        shrink_factor = 1 + no_improvements

                        next_range = (hi_freq - lo_freq) / shrink_factor
                        # smaller increment to lo_freq
                        if not best:
                            lo_freq /= shrink_factor
                        else:
                            lo_freq = best.freq + delta_increment / shrink_factor
                        hi_freq = lo_freq + next_range
                    else:
                        lo_freq = best.freq + delta_increment
                        no_improvements = 0
                        # last or one before last
                        if improved_idx == num_workers - 1 or frequencies_to_try[-1] - best.freq <= freq_step:
                            min_plausible_period = round((ONE_THOUSAND / best.freq) - best.results['wns'] - 0.001, 3)
                            lo_point_choice = frequencies_to_try[1] if len(
                                frequencies_to_try) > 4 else frequencies_to_try[0]
                            hi_freq = max(2 * best.freq - lo_point_choice,  ONE_THOUSAND / min_plausible_period)
                        else:
                            hi_freq = frequencies_to_try[improved_idx + 1]
                        hi_freq += resolution + 2 * freq_step

                    logger.info(f'[Fmax] End of iteration #{num_iterations}')
                    logger.info(f'[Fmax] Execution Time so far: {int(time.monotonic() - start_time) // 60} minute(s)')

        except KeyboardInterrupt:
            logger.exception('Received Keyboard Interrupt')
        except:
            logger.exception('Received exception')
        finally:
            if future and not future.cancelled():
                future.cancel()
            if pool:
                pool.close()
                pool.join()
            runtime_minutes = int(time.monotonic() - start_time) // 60
            if best:
                best.iterations = num_iterations
                best.runtime_minutes = runtime_minutes
                print_results(best.results, title='Best Results', subset=[
                    'clock_period', 'frequency', 'lut', 'ff', 'slice'])
                best_json_path = Path(args.xeda_run_dir) / \
                    f'fmax_{settings["design"]["name"]}_{flow_name}_{self.timestamp}.json'
                logger.info(f"Writing best result to {best_json_path}")

                with open(best_json_path, 'w') as f:
                    json.dump(best, f, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else str(x), indent=4)
            else:
                logger.warning("No successful results.")
            logger.info(f'[Fmax] Total Execution Time: {runtime_minutes} minute(s)')
            logger.info(f'[Fmax] Total Iterations: {num_iterations}')
