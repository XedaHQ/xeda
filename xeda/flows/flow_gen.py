import logging
import time
from datetime import datetime
from typing import Mapping, Optional, Type, Union, Dict, List, Any
from xeda.utils import snakecase_to_camelcase
from .flow import Flow, Design, registered_flows
import importlib
import hashlib
from pathlib import Path

logger = logging.getLogger()


class FlowGen:
    # TODO more specific than Any?
    def __init__(self, all_flows_settings: Dict[str, Dict]):
        self.all_flows_settings: Dict[str, Dict] = all_flows_settings

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

    def generate(self, flow_name: str, module_name: str, design: Design, xeda_run_dir: Path, completed_dependencies: List[Flow], package: str = __package__) -> Flow:
        print(
            f"flow_name:{flow_name} module_name:{module_name} registered flows: {registered_flows}")
        full_module_name = "xeda" + \
            module_name if module_name.startswith('.') else module_name

        flow_class: Optional[Type[Flow]] = registered_flows.get(
            (flow_name, full_module_name))
        if flow_class is None:
            print("Not found in registered flows")
            module = importlib.import_module(module_name, package)
            flow_class = getattr(module, snakecase_to_camelcase(flow_name))
        assert flow_class is not None and issubclass(flow_class, Flow)
        flow_settings = flow_class.Settings(
            **self.all_flows_settings.get(flow_name, {}))
        # flow.settings = self.flow_settings

        xeda_hash = self.semantic_hash(
            dict(flow_name=flow_name, flow_settings=flow_settings, design=design))

        results_dir = xeda_run_dir / 'Results' / flow_name
        results_dir.mkdir(exist_ok=True, parents=True)
        run_path = xeda_run_dir / f"{flow_name}_{xeda_hash}"
        run_path.mkdir(exist_ok=True)
        reports_dir = run_path / flow_settings.reports_subdir_name
        reports_dir.mkdir(exist_ok=True)
        
        flow = flow_class(flow_settings, design, run_path,
                          completed_dependencies)
        flow.dump_settings()

        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.init_time = time.monotonic()

        return flow
