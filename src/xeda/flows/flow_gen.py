from .flow import Flow, Design, registered_flows
from ..utils import dict_merge, snakecase_to_camelcase, dump_json, try_convert
from pathvalidate import sanitize_filename
import logging
import time
from datetime import datetime
from typing import Mapping, Type, Dict, List, Any
import importlib
import hashlib
from pathlib import Path
import re

from .._version import get_versions
__version__ = get_versions()['version']
del get_versions


logger = logging.getLogger(__name__)

def get_flow_class(flow_name: str, module_name: str, package: str) -> Type[Flow]:
    (mod, flow_class) = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        logger.warn(
            f"Flow {flow_name} was not found in registered flows. Trying to load using importlib.import_module")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            logger.critical(
                f"Unable to import {module_name} from {package}")
            raise e from None
        assert module is not None, f"importlib.import_module returned None. module_name: {module_name}, package: {package}"
        flow_class_name = snakecase_to_camelcase(flow_name)
        try:
            flow_class = getattr(module, flow_class_name)
        except AttributeError as e:
            logger.critical(
                f"Unable to find class {flow_class_name} in {module}")
            raise e from None
    assert flow_class is not None and issubclass(flow_class, Flow)
    return flow_class

class FlowGen:
    @staticmethod
    def merge_overrides(overrides, settings):
        if overrides:
            if isinstance(overrides, str):
                overrides = re.split(r'\s*,\s*', overrides)

            if isinstance(overrides, list):
                for override in overrides:
                    key, val = override.split('=')
                    hier = key.split('.')
                    patch_dict = dict()
                    tmp = patch_dict
                    for field in hier[:-1]:
                        tmp[field] = dict()
                        tmp = tmp[field]
                    tmp[hier[-1]] = try_convert(val, convert_lists=True)
                    settings = dict_merge(settings, patch_dict, True)
                return settings
            if isinstance(overrides, Flow.Settings):
                overrides = overrides.__dict__
            assert isinstance(overrides, dict), f"overrides is of type {type(overrides)}"
            for k, v in overrides.items():
                settings[k] = v
        return settings

    @staticmethod
    def semantic_hash(data: Any) -> str:
        def get_digest(b: bytes):
            return hashlib.sha1(b).hexdigest()[:16]

        # data: JsonType, not adding type as Pylance does not seem to like recursive types :/
        def sorted_dict_str(data):
            if isinstance(data, Mapping):
                return {k: sorted_dict_str(data[k]) for k in sorted(data.keys())}
            elif isinstance(data, list):
                return [sorted_dict_str(val) for val in data]
            elif hasattr(data, '__dict__'):
                return sorted_dict_str(data.__dict__)
            else:
                return str(data)

        return get_digest(bytes(repr(sorted_dict_str(data)), 'UTF-8'))


    def get_settings_schema(self, flow_name: str, module_name: str, package: str = __package__):
        flow_class = get_flow_class(flow_name, module_name, package)
        return flow_class.Settings.schema(by_alias=False)

    @classmethod
    def generate(cls, flow_class, design: Design, xeda_run_dir: Path, override_settings: Mapping[str, Any]) -> Flow:
        flow_name = flow_class.name
        flow_settings = flow_class.Settings(**override_settings)
        design_hash = cls.semantic_hash(design)
        flowrun_hash = cls.semantic_hash(dict(
            flow_name=flow_name, flow_settings=flow_settings, xeda_version=__version__
        ))

        results_dir = xeda_run_dir / 'Results' / flow_name
        results_dir.mkdir(exist_ok=True, parents=True)
        design_subdir = f"{design.name}"
        flow_subdir = flow_name
        if flow_settings.unique_rundir:
            design_subdir += f'_{design_hash}'
            flow_subdir += f'_{flowrun_hash}'

        run_path = xeda_run_dir / sanitize_filename(design_subdir) / flow_subdir
        run_path.mkdir(parents=True, exist_ok=True)

        settings_json_path = run_path / f'settings.json'
        logger.info(f'dumping effective settings to {settings_json_path}')
        all_settings = dict(
            design=design,
            flow_name=flow_name,
            flow_settings=flow_settings,
            xeda_version=__version__
        )
        dump_json(all_settings, settings_json_path)

        reports_dir = run_path / flow_settings.reports_subdir_name
        reports_dir.mkdir(exist_ok=True)

        flow = flow_class(flow_settings, design, run_path)

        flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        flow.init_time = time.monotonic()

        return flow
