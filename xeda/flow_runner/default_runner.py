import multiprocessing
import sys
import re
import logging
import pkg_resources
import json
# import psutil

from ..flows.settings import Settings
from ..flows.flow import Flow, FlowFatalException, my_print
from ..utils import camelcase_to_snakecase, load_class, dict_merge, try_convert

logger = logging.getLogger()

def merge_overrides(overrides, settings):
    if overrides:
        if isinstance(overrides, str):
            overrides = [overrides]
        if len(overrides) == 1:
            overrides = re.split(r'\s*,\s*', overrides[0])
        for override in overrides:
            key, val = override.split('=')
            hier = key.split('.')
            patch_dict = dict()
            for field in hier[:-1]:
                new_dict = dict()
                patch_dict[field] = new_dict
                patch_dict = new_dict
            patch_dict[hier[-1]] = try_convert(val, convert_lists=True)
            settings = dict_merge(settings, patch_dict, True)
    return settings


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
                my_print(
                    f'{k:{name_width}}{"True" if v else "False":>{data_width}}')
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
        logger.exception(
            f'[Run Thread] Fatal exception during flow run in {f.flow_run_dir}: {e}')
    except KeyboardInterrupt as e:
        logger.exception(
            f'[Run Thread] Received KeyboardInterrupt during flow run in {f.flow_run_dir}: {e}')
    except KeyboardInterrupt as e:
        logger.exception(
            f'[Run Thread] Received KeyboardInterrupt during flow run in {f.flow_run_dir}: {e}')
    return {}


class FlowRunner:
    def __init__(self, args, xeda_project, timestamp) -> None:
        self.args = args
        self.timestamp = timestamp
        self.xeda_project = xeda_project

        if not hasattr(args, 'override_settings'):
            self.args.override_settings = None

        self.all_settings = self.get_all_settings()

    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string('xeda', "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            self.fatal(
                f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}", e)

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

    def get_all_settings(self):

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
                logger.critical(
                    f'Design "{dname}" not found in the current project.')
            else:
                logger.critical(
                    f'{len(d)} designs are availables in the current project. Please specify target design using --design')
            logger.critical(
                f'Available designs: {", ".join([x["name"] for x in d])}')
            sys.exit(1)

        design_settings = dict(design=get_design(
            self.xeda_project['design']), flows=self.xeda_project.get('flows', {}))

        settings = dict_merge(settings, design_settings)

        settings = merge_overrides(self.args.override_settings, settings)
        flow_settings = settings['flows'].get(self.args.flow, dict())
        settings['flows'][self.args.flow] = merge_overrides(
            self.args.override_flow_settings, flow_settings)

        settings['design']['xeda_version'] = self.xeda_project['xeda_version']

        return self.validate_settings(settings)

    # should not override
    def post_run(self, flow: Flow, print_failed=True):
        # Run post-run hooks
        for hook in flow.post_run_hooks:
            logger.info(
                f"Running post-run hook from from {hook.__class__.__name__}")
            hook(flow)

        flow.reports_dir = flow.flow_run_dir / flow.reports_subdir_name
        if not flow.reports_dir.exists():
            flow.reports_dir.mkdir(parents=True)

        flow.parse_reports()
        flow.results['timestamp'] = flow.timestamp
        flow.results['design.name'] = flow.settings.design['name']
        flow.results['flow.name'] = flow.name
        flow.results['flow.run_hash'] = flow.xedahash

        if print_failed or flow.results.get('success'):
            flow.print_results()
        flow.dump_results()

        # Run post-results hooks
        for hook in flow.post_results_hooks:
            logger.info(
                f"Running post-results hook from {hook.__class__.__name__}")
            hook(flow)

    def load_flowclass(self, name: str) -> Flow:
        splitted = name.split('.')
        package = ".flows"
        if len(splitted) > 1:
            name = splitted[-1]
            # FIXME TODO merge-in plugin code
            package = ".plugins." + ".".join(splitted[:-1]) + ".flows"
        try:
            return load_class(name, package)
        except AttributeError as e:
            self.fatal(
                f"Could not find Flow class corresponding to {name}. Make sure it's typed correctly.", e)

    def setup_flow(self, flow_settings, design_settings, flow_cls, completed_dependencies=[]) -> Flow:

        if isinstance(flow_cls, str):
            flow_cls = self.load_flowclass(flow_cls)

        assert issubclass(flow_cls, Flow)

        effective_settings = Settings()

        # override sections
        effective_settings.design = design_settings

        effective_settings.flow = dict_merge(
            flow_cls.default_settings, flow_settings)

        # create and initialize the flow object
        flow: Flow = flow_cls(effective_settings,
                              self.args, completed_dependencies)

        max_threads = effective_settings.flow.get('nthreads')
        if not max_threads:
            max_threads = multiprocessing.cpu_count()
        flow.nthreads = int(max(1, max_threads))

        flow.prepare()

        return flow

    def get_flow_settings(self, flow_name):
        return self.all_settings['flows'].get(flow_name, {})


class DefaultRunner(FlowRunner):
    def launch_flow(self, flow_name_or_class, flow_settings, design_settings, force_run):
        if force_run:
            logger.warning(f"Forced re-run of {flow_name_or_class}")

        flow_class = self.load_flowclass(flow_name_or_class) if isinstance(
            flow_name_or_class, str) else flow_name_or_class

        completed_dependencies = []

        prerequisite_flows = flow_class.prerequisite_flows(
            flow_settings, design_settings)

        for prereq, (flow_overrides, design_overrides) in prerequisite_flows.items():
            prereq_name = prereq if isinstance(
                prereq, str) else camelcase_to_snakecase(prereq.name)

            prereq_flowsettings = dict_merge(
                self.get_flow_settings(prereq_name), flow_overrides)
            prereq_design = dict_merge(design_settings, design_overrides)

            logger.warning(f"Prerequisite: {prereq.__name__}")
            # recursive call
            completed_prereq = self.launch_flow(
                prereq, prereq_flowsettings, prereq_design, self.args.force_rerun
            )
            completed_dependencies.append(completed_prereq)

        flow = self.setup_flow(flow_settings, design_settings, flow_class,
                               completed_dependencies)

        results_json = flow.flow_run_dir / 'results.json'

        if not force_run:
            try:
                with open(results_json) as f:
                    flow.results = json.load(f)
            except FileNotFoundError:
                force_run = True
                logger.info(
                    f"Running flow {flow.name} as {results_json} does not exist.")
            except Exception as e:
                force_run = True
                logger.warning(f"running flow {flow.name} due to {e}")

            if not force_run and not flow.results.get('success'):
                force_run = True
                logger.info(
                    f"Re-running flow {flow.name} as the previous run was not successful")

            prev_hash = flow.results.get('flow.run_hash')
            if not force_run and prev_hash != flow.xedahash:
                force_run = True
                logger.info(
                    f"Re-running flow {flow.name} as the previous run hash ({prev_hash}) did not match the current one ({flow.xedahash})")

        if force_run:
            flow.run_flow()
            self.post_run(flow)
            if not flow.results.get('success'):
                logger.critical(f"{flow.name} failed")
                exit(1)
        else:
            logger.warning(
                f"Previous results in {results_json} are already up-to-date. Will skip running {flow.name}.")
            flow.print_results()

        return flow

    def launch(self):
        flow_name = self.args.flow
        flow_settings = self.get_flow_settings(flow_name)
        self.launch_flow(flow_name, flow_settings,
                         self.all_settings['design'], True)


def nukemall():
    def on_terminate(proc):
        logger.warning(
            f"Child process {proc.info['name']}[{proc}] terminated with exit code {proc.returncode}")

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
