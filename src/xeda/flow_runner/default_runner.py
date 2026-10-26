"""The launcher: the one procedure that launches every flow, and the requests that reach it."""

from __future__ import annotations

import difflib
import functools
import importlib
import logging
import os
import sys
import time
import types
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from pprint import PrettyPrinter
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)


from ..artifacts import iter_artifact_paths
from ..dataclass import XedaBaseModel, model_validator
from ..frozen import freeze, thaw
from ..local_settings import LocalSettings
from ..deliver import (
    OUTPUTS_TO,
    Conflict,
    ConfirmedReplacements,
    DeliveredFiles,
    Deliveries,
    Delivery,
    DeliveryError,
    ReadInputs,
    outputs_to_deliveries,
    recorded_artifacts,
    refuse_shared_destinations,
    split_deliveries,
)
from ..design import (
    NO_SINGULAR_TARGET,
    Design,
    DesignValidationError,
    _shown_dependencies,
    names_a_design_file,
)
from ..flow import (
    Flow,
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsException,
    registered_flows,
)
from ..flow import flowrun_hash as flow_run_hash
from .. import log_context
from ..proc_utils import recording_programs
from ..run_dir import (
    DIR_NAME_HASH_LEN,
    RunDirectory,
)
from ..run_root import DEFAULT_RUN_ROOT, ensure_run_root
from ..utils import (
    backup_existing,
    dump_json,
    settings_to_dict,
    unique,
)
from ..version import __version__
from ..errors import FlowNotFoundError as PublicFlowNotFoundError
from ..errors import ProjectError
from ..project import Project
from ..types import UNSET, Unset
from ..xedaproject import PROJECT_FILE_NAMES
from .bindings import (
    LOCAL_REQUESTS_ONLY,
    BindingLayer,
    NodeKey,
    default_nodes,
    effective_bindings,
    input_origins,
    node_identity,
    require_no_bindings,
    split_bindings,
)
from . import (
    launch_generation,
    launch_identity,
    launch_input,
    launch_producers,
    launch_report,
    launch_run,
)
from .chains import ChainElement, FlowRequest, parse_request
from .layer_paths import anchored_sections
from .code_digest import CodeDigests
from .configuration import configuration_hash, design_configuration_hash
from .error_codes import error_info
from .launch_identity import RunDirPolicy
from .launch_input import refuse_outputs_to_a_programmer
from .launch_report import (
    report,
    reuse_results,
    warn_outputs_to_delivered_nothing,
)
from .launch_result import LaunchCollector
from .launch_run import refuse_inputs_inside
from .outputs import declared_output_files
from . import dependencies
from .resolver import (
    Plan,
    PlanNode,
    check_launchable,
    check_settings_without_design,
    planned_configuration,
    planned_design_of,
    resolve as resolve_plan,
)
from .plan_freshness import (
    EVERY_FLOW_RUNS_CLEAN,
    EVERY_FLOW_RUNS_REBUILD_ALL,
    NodeFreshness,
    judge_plan,
)
from .scrub import ScrubConfirmation, ScrubDone, run_directory_names, scrub_runs
from .run_lock import CompletedRun, run_dir_lock, storage_lock
from .settings_layers import (
    API_ORIGIN,
    COMMAND_LINE_ORIGIN,
    SUPPLIED_SECTIONS_ORIGIN,
    FlowNotFoundError,
    check_flow_name,
    command_line_sections,
    compose_flow_settings,
    host_profile_origin,
    project_local_origin,
    user_config_origin,
    merge_flow_sections,
    registered_flow,
    split_flow_sections,
    transitive_dependencies,
)
from .trace import (
    as_recorded,
    check_trace,
    ran_again_note,
    locate_program,
    previous_trace,
    remove_trace,
    write_trace,
)
from .trace_inputs import (
    artifact_files,
    build_trace,
    design_files,
    expectation,
    register_read_settings,
    located_input_files,
    registered_input_files,
    setting_files,
    snapshot_inputs,
)

# The launcher looks this name up here, so a test can replace it on this module and intercept
# the filter of a failed run's artifacts.
_drop_unwritten_artifacts = launch_report.drop_unwritten

#: Names that the stage modules define, each with its module and its name there. Importing one
#: from here works (`__getattr__`), but the launcher does not look them up here: a test that
#: replaces one on this module changes nothing. `tests/test_launcher_modules.py` checks the table.
MOVED_NAMES: dict[str, tuple[str, str]] = {
    "COMPLETION_DOCUMENTS": ("scrub", "COMPLETION_DOCUMENTS"),
    "LAUNCH_DOCUMENTS": ("scrub", "LAUNCH_DOCUMENTS"),
    "ScrubResult": ("scrub", "ScrubResult"),
    "scrub_design": ("scrub", "scrub_design"),
    "_Listed": ("scrub", "_Listed"),
    "_Removal": ("scrub", "_Removal"),
    "_Scan": ("scrub", "_Scan"),
    "_Skipped": ("scrub", "_Skipped"),
    "_completion_records": ("scrub", "_completion_records"),
    "_distinct": ("scrub", "_distinct"),
    "_is_gone": ("scrub", "_is_gone"),
    "_listed": ("scrub", "_listed"),
    "_lock_path": ("scrub", "_lock_path"),
    "_problem_now": ("scrub", "_problem_now"),
    "_refuse_a_change": ("scrub", "_refuse_a_change"),
    "_remove_confirmed": ("scrub", "_remove_confirmed"),
    "_run_directories_in": ("scrub", "_run_directories_in"),
    "_target_parents": ("scrub", "_target_parents"),
    "_warn_outputs_to_delivered_nothing": ("launch_report", "warn_outputs_to_delivered_nothing"),
    "_refuse_outputs_to_a_programmer": ("launch_input", "refuse_outputs_to_a_programmer"),
    "_setting_naming": ("launch_input", "setting_naming"),
    "_free_working_locations": ("launch_run", "free_working_locations"),
    "_refuse_inputs_inside": ("launch_run", "refuse_inputs_inside"),
}


def __getattr__(name: str) -> Any:
    moved = MOVED_NAMES.get(name)
    if moved is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{moved[0]}", __package__), moved[1])


class _LauncherModule(types.ModuleType):
    """The class of this module. It refuses to have a moved name set or deleted.

    A name in `MOVED_NAMES` is found through `__getattr__`, so assigning it (a
    `monkeypatch.setattr` in a test) would make a real global that no launcher code reads, and
    the replacement would change nothing."""

    def __setattr__(self, name: str, value: Any) -> None:
        self._refuse_moved(name)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        self._refuse_moved(name)
        super().__delattr__(name)

    @staticmethod
    def _refuse_moved(name: str) -> None:
        moved = MOVED_NAMES.get(name)
        if moved is not None:
            raise AttributeError(
                f"`{name}` is defined in `xeda.flow_runner.{moved[0]}.{moved[1]}`, and nothing in "
                f"`default_runner` looks it up: replace it on `xeda.flow_runner.{moved[0]}`"
            )


sys.modules[__name__].__class__ = _LauncherModule


__all__ = [
    "DIR_NAME_HASH_LEN",
    "DefaultRunner",
    "FlowNotFoundError",
    "FlowRunner",
    "ProjectError",
    "add_file_logger",
    "remove_file_logger",
    "get_flow_class",
]

log = logging.getLogger(__name__)


#: Launcher settings that Xeda refuses, with what to use instead. Giving one is an error that
#: says so, in the words `Flow.Settings.removed_settings` uses for flow settings.
_REMOVED_LAUNCHER_SETTINGS = {
    "xeda_run_dir": "run_root",
    "cached_dependencies": "the default, which reuses unchanged runs, and hashed_run_dirs=True to "
    "keep settings variants side by side",
    "skip_if_previous_run_exists": "the default, which reuses unchanged runs",
    "incremental": "clean=True to empty run directories before running (they are otherwise "
    "always reused)",
    "cleanup_before_run": "clean=True",
    "run_path": "run_root to choose where runs go (a flow always runs in a directory Xeda "
    "creates under it), and outputs_to to receive its outputs elsewhere",
}


@dataclass(frozen=True)
class _Request:
    """Captured request layers. Workers transport these contributions, not model defaults."""

    flow_class: type[Flow]
    #: The design of the plan and the launch: its dependencies merged in (`dependencies`).
    design: Design
    settings: dict[str, Any]
    sections: dict[str, Any]
    origins: tuple[tuple[str, Mapping[str, Any]], ...]
    command_line: dict[str, Any]
    api_overrides: dict[str, Any]
    flow_request: FlowRequest
    binding_layers: tuple[BindingLayer, ...]
    #: The design as the caller gave it, before planning merged its dependencies. A remote
    #: request packages it with the Git dependencies as references.
    declared: Design


@dataclass(frozen=True)
class PreparedRequest:
    """A request that is loaded, resolved and checked, and not yet launched.

    `FlowLauncher.prepare_request` returns it. `sections` are the merged `flows` sections of all
    layers, flow by flow, as a launch is handed them."""

    design: Design
    sections: dict[str, Any]
    plan: Plan
    flow_request: FlowRequest
    target: str | None
    #: The design as the caller gave it (`_Request.declared`).
    declared: Design


def _flow_name_suggestions(flow_name: str, limit: int = 3) -> List[str]:
    """Return the canonical names of registered flows most similar to `flow_name`."""
    canonical = unique([cls.name for _mod, cls in registered_flows.values()])
    return difflib.get_close_matches(flow_name, canonical, n=limit, cutoff=0.6)


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__ or "xeda"
) -> Type[Flow]:
    """Return the registered flow whose name is `flow_name`.

    A flow has one name, `Flow.name`. The name of a removed flow, and any other spelling of the
    name of a flow, is an error that says what to use (`check_flow_name`)."""
    flow_class = registered_flow(flow_name)
    if flow_class is None:
        log.debug(
            "Flow %s was not found in registered flows. Trying to load using `importlib`.",
            flow_name,
        )
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise FlowNotFoundError(flow_name, _flow_name_suggestions(flow_name)) from e
        flow_class = registered_flow(flow_name)
    if flow_class is None:
        check_flow_name(flow_name)
        raise FlowNotFoundError(flow_name, _flow_name_suggestions(flow_name))
    return flow_class


def _get_flow_class_if_known(flow_name: str) -> Type[Flow] | None:
    """Resolve a flow-section key without rejecting sections for unavailable plugin flows."""
    try:
        return get_flow_class(flow_name)
    except FlowNotFoundError:
        return None


FlowLauncherType = TypeVar("FlowLauncherType", bound="FlowLauncher")


_Method = TypeVar("_Method", bound=Callable[..., Any])


def with_node_context(method: _Method) -> _Method:
    """Wrap `_launch` so that the log records of one node carry its label (`xeda_node`)."""

    @functools.wraps(method)
    def wrapper(
        self: FlowLauncher,
        flow_class: Union[str, Type[Flow]],
        *args: Any,
        plan_node: NodeKey | None = None,
        **kwargs: Any,
    ) -> Any:
        if plan_node is not None:
            label = plan_node.label
        else:
            label = (get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class).name
        with log_context.running_node(label):
            return method(self, flow_class, *args, plan_node=plan_node, **kwargs)

    return cast(_Method, wrapper)


@dataclass
class _LaunchGeneration:
    """The design of a launch, as the caller gave it and as the flows get it."""

    declared: Design
    generation: launch_generation.Generations | None
    #: The declared design, with the files that its generator wrote in place of its generated
    #: sources (`launch_generation.execution_design`).
    execution: Design


class FlowLauncher:
    """Launch flows together with their producers, in the manner of make: see `launch_flow`."""

    #: Whether `run` takes a chain or a request with explicit input bindings. A launcher that
    #: hands requests to workers (`Dse`) refuses them before anything is submitted.
    accepts_bindings: ClassVar[bool] = True

    class Settings(XedaBaseModel):
        """The settings of a launcher."""

        debug: bool = False
        dump_settings_json: bool = True
        #: Whether the command line shows the tables of a run. The library prints nothing.
        display_results: bool = True
        dump_results_json: bool = True
        #: Run every flow, even one whose trace still matches. Without it, runs are make-like.
        rebuild_all: bool = False
        #: A run directory per run hash (`<design>/<flow>_<hash>`: settings and input origins)
        #: instead of one per flow (`<design>/<flow>`).
        hashed_run_dirs: bool = False
        #: Empty the run directory of each flow before it runs, and run every flow (implies
        #: `rebuild_all`).
        clean: bool = False
        backups: bool = False
        #: Remove the files of a flow after the run. The prune keeps settings.json, results.json,
        #: the artifacts and the declared output files, with their parent directories. It
        #: removes trace.json first.
        post_cleanup: bool = False
        #: Remove the run directory of a flow, with all of its contents, after the run. It
        #: needs no `post_cleanup`: `_run_dir_policy` makes one imply the other.
        post_cleanup_purge: bool = False
        #: Remove the previous run directories of a flow before the run.
        scrub_old_runs: bool = False
        #: Copy the artifacts of the requested flow into this directory once it succeeded.
        outputs_to: Optional[Path] = None
        #: Replace a file at an output path that the user named when it is not an earlier,
        #: unchanged copy made by Xeda (`xeda.deliver`). Never replace an input, a directory or
        #: a run root.
        overwrite_outputs: bool = False

        @model_validator(mode="before")
        @classmethod
        def _removed_settings(cls, values):
            if isinstance(values, dict):
                for old, replacement in _REMOVED_LAUNCHER_SETTINGS.items():
                    if old in values:
                        raise ValueError(f"`{old}` was removed: use {replacement}")
            return values

        def __setattr__(self, name: str, value: Any) -> None:
            if name in _REMOVED_LAUNCHER_SETTINGS:
                raise ValueError(f"`{name}` was removed: use {_REMOVED_LAUNCHER_SETTINGS[name]}")
            super().__setattr__(name, value)

    def __init__(self, run_root: Union[str, Path, None] = None, **kwargs) -> None:
        self.settings = self.Settings(**kwargs)
        if self.settings.outputs_to is not None:
            self.settings.outputs_to = Path(os.path.abspath(self.settings.outputs_to))
        # Refused here, before anything runs, if it is not Xeda's. Created when a flow first
        # needs it (`run_root`), so a launch that fails at its input leaves none behind.
        self._start = Path.cwd()
        self._run_root = Path(run_root or DEFAULT_RUN_ROOT).resolve()
        self._run_root_ready = (
            ensure_run_root(self._run_root, start=self._start, create=False) is not None
        )
        log.debug("%s run_root=%s", self.__class__.__name__, self._run_root)
        self.debug = self.settings.debug
        #: Every flow launched through this launcher, producers included, in completion order.
        self.launched: List[Flow] = []
        self._launch_depth = 0
        #: Post-run clean-ups waiting for the flow the current launch was asked for to complete.
        self._pending_clean_ups: List[Tuple[Flow, Path, Path, RunDirPolicy]] = []
        #: The run directories the current launch has entered.
        self._claims: set[Path] = set()
        #: How many flows `launched` held when the current launch began.
        self._launched_before = 0
        #: The replacements the user confirmed in the current launch, each as the file was then.
        self._confirmed_replacements = ConfirmedReplacements()
        #: The deliveries of the current launch's producers, checked when it started, by node:
        #: each producer checks again, at its turn, with its own.
        self._deliveries_ahead: dict[NodeKey, Deliveries] = {}
        #: The files that `--outputs-to` expects to deliver from the last run of the requested
        #: flow, as `(flow, delivery, destination)`, kept from the check ahead.
        self._predicted_copies: list[tuple[str, Delivery, Path]] = []
        #: Asked, at an interactive terminal, whether to replace files in the way of named
        #: outputs (`xeda.deliver.Deliveries.check`); None: only `overwrite_outputs` counts.
        self.confirm_overwrite: Optional[Callable[[Sequence[Conflict]], bool]] = None
        #: Asked whether to remove the older runs of `scrub_old_runs` (`xeda.flow_runner.scrub`).
        #: None: nothing is removed, and the launch logs that.
        self.confirm_scrub: Optional[ScrubConfirmation] = None
        #: Told what that scrub did, when it ends (`xeda.flow_runner.scrub.ScrubDone`).
        self.scrub_done: Optional[ScrubDone] = None
        #: The design and project file `run()` was given: never an output's destination.
        self._launch_inputs: List[Path] = []
        #: Every file the flows of the current launch read (`xeda.deliver.ReadInputs`).
        self._read_inputs = ReadInputs()
        #: The code digests of the current launch (`code_digest.CodeDigests`).
        self._code_digests = CodeDigests()
        self._delivered_files = DeliveredFiles()
        #: The deliveries of the current launch's flows, made when it has finished.
        self._pending_deliveries: List[Tuple[Flow, Deliveries]] = []
        self._request_context: _Request | None = None
        #: The selected target and the name of the design last loaded for a request, for
        #: documents to report; None until a request has loaded one.
        self.target: str | None = None
        self.design_name: str | None = None
        #: The generation of the current launch: the design as the caller gave it, what its
        #: generator wrote, and the design that the flows get (`launch_generation`). Set by the
        #: requested flow, after every declarative check, and read by its producers.
        self._generation: _LaunchGeneration | None = None
        #: What the current launch holds until it ends: the shared lease of its storage.
        self._leases = ExitStack()
        self._plans: dict[int, tuple[Plan, Any, Any]] = {}
        self._planned_completed: dict[tuple[int, NodeKey], Flow] = {}
        #: What the current launch reports as it goes on (`launch_result.launch_result` reads it).
        self.collector = LaunchCollector()
        self._completed_runs: dict[Path, tuple[Flow, CompletedRun]] = {}

    @property
    def run_root(self) -> Path:
        """The run root (`xeda.run_root`), resolved: created and marked when first used."""
        if not self._run_root_ready:
            root = ensure_run_root(self._run_root, start=self._start)
            assert root is not None  # created when absent
            self._run_root, self._run_root_ready = root, True
        return self._run_root

    @property
    def launch_id(self) -> str | None:
        """The id of the launch that this thread runs through the launcher, or None outside a
        launch. It is 32 hexadecimal digits, made once when the launch starts. Each log record of
        the launch carries it as `xeda_launch_id`. The launcher keeps no state for it, so launches
        that run at once in several threads each see their own."""
        return log_context.current_launch_id()

    @property
    def xeda_run_dir(self) -> Path:
        """Removed: the run root is `run_root`."""
        raise AttributeError("`xeda_run_dir` was removed: use run_root")

    def run_path_of(
        self,
        design_name: str,
        node_name: str,
        identity: Optional[str] = None,
        *,
        target: str | None = None,
    ) -> Path:
        """Return the path of a run directory (`launch_identity.run_path_of`).

        The path is `<run root>/<design>[/<target>]/<node>`, or `<node>_<identity>` with hashed
        run directories. It lies strictly inside the run root and is resolved."""
        return launch_identity.run_path_of(
            self._run_root,
            self._start,
            self.settings.hashed_run_dirs,
            design_name,
            node_name,
            identity,
            target=target,
        )

    def get_flow_run_path(
        self,
        design_name: str,
        node_name: str,
        identity: str | None = None,
        *,
        target: str | None = None,
    ) -> Path:
        """Create and mark the run root, then revalidate the path of the run at operation time.

        A name that is refused (of a design or a target) is refused before the root is touched."""
        self.run_path_of(design_name, node_name, identity, target=target)
        self.run_root
        return self.run_path_of(design_name, node_name, identity, target=target)

    @log_context.with_launch_context
    def resolve(
        self,
        flow_class: type[Flow],
        design: Design,
        flow_settings: Mapping[str, Any] | Flow.Settings | None,
        all_flows_settings: Mapping[str, Any] | None = None,
        *,
        origins: Sequence[tuple[str, Mapping[str, Any]]] = (),
        command_line: Mapping[str, Mapping[str, Any]] | None = None,
        api_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        flow_request: FlowRequest | None = None,
        binding_layers: Sequence[BindingLayer] = (),
    ) -> Plan:
        """Resolve one request without constructing a flow, probing tools or writing files.

        A request composes its files itself and passes them as `origins`, lowest precedence
        first (`_request`: the sections of the project, then those of the design; the remote
        runner does the same). A launch that is only handed a built design (`run_flow`,
        `launch_flow`, a direct call) composes none. Then the `flows` sections of the design
        itself are its file origin, the sections it was handed come after them, and the
        settings after those. So planning a design and launching it from the same arguments
        resolve the same plan.

        A design that still has dependencies to merge gets them merged first, after the checks
        that the settings alone allow."""
        self._refuse_before_merging(
            flow_class, design, flow_settings, all_flows_settings, flow_request
        )
        design = self._merged(design)
        recorded_settings = as_recorded(flow_settings or {})
        recorded_sections = as_recorded(all_flows_settings or {})
        direct = not origins
        design_label = f"the design {design.name}"
        # Capture reserved keys before settings composition: `inputs` is wiring the resolver
        # selects edges by, never a setting.
        layers: list[BindingLayer] = []
        clean_origins = []
        if direct:
            clean, bindings = split_bindings(thaw(design.flows), location=design_label)
            clean_origins.append((design_label, clean))
            layers.append(bindings)
        layers.extend(binding_layers)
        for location, values in origins:
            clean, bindings = split_bindings(values, location=location)
            clean_origins.append((location, clean))
            layers.append(bindings)
        all_flows_settings, bindings = split_bindings(
            all_flows_settings or {}, location=SUPPLIED_SECTIONS_ORIGIN
        )
        if bindings.entries or bindings.invalid_inputs or (not origins and not binding_layers):
            layers.append(bindings)
        if direct:
            clean_origins.append((SUPPLIED_SECTIONS_ORIGIN, all_flows_settings))
        clean_cli, bindings = split_bindings(
            command_line or {}, location=COMMAND_LINE_ORIGIN, kind="cli"
        )
        layers.append(bindings)
        clean_api, bindings = split_bindings(api_overrides or {}, location=API_ORIGIN, kind="api")
        layers.append(bindings)
        clean_root, bindings = split_bindings(
            {flow_class.name: flow_settings or {}}, location=API_ORIGIN, kind="api"
        )
        layers.append(bindings)
        # Preserve the context/full value of a Settings instance on ordinary direct calls.
        if not isinstance(flow_settings, Flow.Settings):
            flow_settings = clean_root[flow_class.name]
        plan = resolve_plan(
            flow_class,
            design,
            flow_settings,
            all_flows_settings,
            runner_cwd=Path.cwd(),
            run_root=self._run_root,
            hashed_run_dirs=self.settings.hashed_run_dirs,
            run_path=self.run_path_of,
            origins=clean_origins,
            command_line=clean_cli,
            api_overrides=clean_api,
            debug=self.settings.debug,
            flow_request=flow_request,
            binding_layers=layers,
        )
        # The resolver validates final agreed settings. The original request may contain
        # partial shared values that become valid only along a declared edge.
        self._plans[id(plan)] = (
            plan,
            recorded_settings,
            recorded_sections,
        )
        return plan

    def _resolve_request(self, request: _Request) -> Plan:
        return self.resolve(
            request.flow_class,
            request.design,
            request.settings,
            request.sections,
            origins=request.origins,
            command_line=request.command_line,
            api_overrides=request.api_overrides,
            flow_request=request.flow_request,
            binding_layers=request.binding_layers,
        )

    def _validate_plan(
        self,
        plan: Plan,
        flow_class: type[Flow],
        design: Design,
        settings: Mapping[str, Any] | Flow.Settings | None,
        sections: Mapping[str, Any] | None,
        node_key: NodeKey | None = None,
    ) -> PlanNode:
        """Validate a plan that this launcher made. Plans from elsewhere are not supported."""
        captured = self._plans.get(id(plan))
        context = plan.context
        # The whole design, without content: the plan is of this request, not of one node's
        # identity. The declared design is given here, whatever the generator has written.
        design_hash = design_configuration_hash(design)
        if (
            captured is None
            or captured[0] is not plan
            or (
                context.design_hash != design_hash
                or context.design_root != design.root
                or context.runner_cwd != Path.cwd()
                or context.run_root != self._run_root
                or context.hashed_run_dirs != self.settings.hashed_run_dirs
                or context.debug != self.settings.debug
                or context.target != design.target
            )
        ):
            raise FlowFatalError("The plan does not match this request's context")
        node_key = node_key or NodeKey(flow_class.name)
        if node_key not in plan:
            raise FlowFatalError(f"The plan has no node for this request: {node_key.label}")
        node = plan.node(node_key)
        supplied = as_recorded(settings or {})
        supplied_context = settings.context if isinstance(settings, Flow.Settings) else {}
        if (
            node.flow_class is not flow_class
            or any(
                value is not None and node.settings.context.get(key) != value
                for key, value in supplied_context.items()
            )
            or (
                supplied != as_recorded(node.settings)
                and (node.name != plan.requested or supplied != captured[1])
            )
            or as_recorded(sections or {}) != captured[2]
        ):
            raise FlowFatalError("The plan does not match this request's settings")
        # Every node's identity is recomputed from its settings, its design configuration and its
        # ordered input origins, producers first, by the helpers the resolver froze it with.
        identities: dict[str, str] = {}
        for planned in plan.nodes:
            try:
                origins = input_origins(planned.inputs, identities.__getitem__, design)
            except KeyError as error:
                raise FlowFatalError(
                    f"The plan's node {planned.name} reads a node that does not precede it"
                ) from error
            settings_hash = flow_run_hash(planned.flow_class.name, planned.settings, design.name)
            configuration = freeze(
                planned_configuration(
                    plan,
                    planned,
                    design,
                    self.run_path_of,
                    planned.flow_class,
                    planned.settings,
                )
            )
            identities[planned.name] = node_identity(
                settings_hash, configuration_hash(configuration), origins
            )
            if (
                planned.settings_hash != settings_hash
                or planned.origins != origins
                or planned.configuration != configuration
                or planned.flowrun_hash != identities[planned.name]
                or planned.run_path
                != self.run_path_of(
                    design.name, planned.name, planned.flowrun_hash, target=design.target
                )
            ):
                raise FlowFatalError("The plan does not match this request's identity or path")
        return node

    @log_context.with_launch_context
    def launch_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        *,
        depender: Optional[Flow] = None,
        consumer: str | None = None,
        all_flows_settings: Union[Dict, None] = None,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
    ) -> Flow:
        """Launch `flow_class` on `design`.

        This is the one procedure that every run of a flow, and every run of a producer, goes
        through. It works in the order of make: bring every prerequisite up to date, then judge
        this flow against them. The stages, in order:

        1. Input (`launch_input.input_settings`). Validate the settings in their context
           (design root, start directory) and apply the `debug` setting of the launcher. The
           result is the input of the run, never modified afterwards. A missing required
           setting, or a design that the flow cannot run, fails the launch here.
        2. Identity (`launch_identity.run_identity`). Compute the `flowrun_hash` of the node,
           which holds no content of the design. Decide the run directory, `<design>[/<target>]/<flow>`
           (with `hashed_run_dirs`, `<flow>_<flowrun_hash>`). The flow must be able to work in
           it (`Flow.check_run_directory`, judged on the path before anything is created).
           Lock the run directory from here until the trace of the run is written
           (`run_lock`). With `clean`, empty the directory now (or back it up, with `backups`).
        2a. Generation (`launch_generation`). The requested flow, once every declarative check
           has passed and before it takes the lock of any run directory, runs the generator of the
           design into its storage under the run root, unless the storage is current. It then
           holds the shared lease of the storage until the last delivery. From here the flows get
           the execution copy of the design, whose generated sources name their files in the
           storage, and the hash of the design is that of the copy. A failure is the error of the
           whole launch (`generator_failed`).
        3. Design. A flow whose `design` input comes from a converter (bsc turns Bluespec into
           Verilog) gets that converter launched first (`launch_producers.run_design_producer`),
           under the lock of this flow and before the flow exists. The record of the converter is
           verified under its read lease, and the design it derived replaces the loaded design
           for the rest of the launch: it is checked again, hashed, given to the flow, and
           recorded in the trace. A failure of the converter is recorded as the failure of this
           flow, which is then built on the design the plan foresaw and not initialized.
        4. Prepare. Construct the flow with its own copy of the input and call its `init()`.
           `init()` runs even for a flow that turns out to be up to date, so it must not change
           a file in the run directory: every file there is an output of the last run.
        5. Producers (`launch_producers.run_producers`). Launch each other producer that the plan
           names through this same procedure, in a sibling run directory, with the settings
           that the plan holds for it.
        6. Prepare inputs (`Flow.prepare_inputs`). After the hand-over, the flow registers the
           files that it resolves, before the check whether it is up to date.
        7. Freshness. Without `rebuild_all` or `clean` (the default), a flow whose trace still
           matches what it would consume now (`trace.check_trace`) is not run again: its
           recorded results are reused. If the trace says its checks rejected the run
           (`Flow.reject`), and `results.json` agrees (`launch_report.adoptable_document`), the
           launch reports that rejection again: it gives the results, not the outputs, and
           hands nothing over. Otherwise `flow.stale_reason` says why it runs. A flow
           that can never be reused (`Flow.always_runs`: it programs a device, draws a new
           random seed, ...) always runs, with that reason, and no trace records it.
        8. Run. Delete the trace and the reports that the last run read (`Trace.reports`: a
           report of a previous run is never this run's). Then record every expected input as
           the run finds it (`trace_inputs.snapshot_inputs`: a file edited while the run goes on
           then no longer matches its trace), record `settings.json`, and execute
           (`launch_run.execute`: `run()`, `parse_reports()`, `check_results()`, results). A
           failed run reports the artifacts that it wrote (`Flow.wrote_output`) and no others.
        9. Report (`launch_report.report`). Write the artifacts and `results.json`. For a
           passed run, or a rejected run whose `results.json` was written, then write the
           trace (`trace_inputs.build_trace`: its verdict, the inputs of the
           snapshot; the files that the depfiles and settings of the run name, an input unless
           the run directory holds it; and the outputs of the run, every file of its run
           directory). The post-run clean-up (`_clean_up`) of every flow that ran waits until
           the flow that this launch was asked for has completed. Until then, a flow may still
           read files that its producers wrote without declaring them.

        A launch enters each run directory once: the plan has one node per flow, and a second
        entry is a `FlowFatalError`. Every flow launched is appended to `launched` as it
        completes, whether it succeeded, failed or raised. A flow that did not run, because the
        delivery of one of its producers was refused before the tool of the producer ran, is
        not appended.

        The outputs that the user named (`xeda.deliver`) are checked before any tool of the
        launch runs. The requested flow checks the deliveries of every flow of the plan when the
        launch starts (`_check_deliveries_ahead`): first what no answer could allow, then the
        questions, with the questions of the requested flow last. All of it comes before the
        launch scrubs older runs (`scrub_old_runs`) and before it takes the lock of its run
        directory. Deliveries are noted when their flow succeeded or was found up to date. A
        rejected flow notes none, whether it ran or its rejection was reported again. The
        deliveries are made once the whole launch has finished, and only when the requested flow
        succeeded or was found up to date. A launch that raised, or whose requested flow reports
        failure, delivers nothing, not even the outputs of a producer that succeeded
        (`_finish_launch`).
        """
        if plan is None:
            self._refuse_before_merging(
                get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class,
                design,
                flow_settings,
                all_flows_settings,
            )
        design = self._merged(design)
        top_level = self._launch_depth == 0
        if top_level:
            self._claims = set()
            self._launched_before = len(self.launched)
            self._confirmed_replacements = ConfirmedReplacements()
            self._deliveries_ahead = {}
            self._predicted_copies = []
            self._planned_completed = {}
            self._completed_runs = {}
            # Every file that the flows of this launch read: the requested flow registers the
            # settings of the whole plan, and each flow the files it prepares.
            self._read_inputs = ReadInputs(self._launch_inputs)
            self._code_digests = CodeDigests()  # read the code once per launch, never longer
            self._delivered_files = DeliveredFiles()
            self._pending_deliveries = []
            self._generation = None
            self.collector = LaunchCollector()
        self._launch_depth += 1
        attempts = self.collector.depth()
        try:
            flow = self._launch(
                flow_class,
                design,
                flow_settings,
                depender,
                all_flows_settings,
                plan=plan,
                plan_node=plan_node,
                consumer=consumer,
            )
            if plan is not None:
                self._planned_completed[(id(plan), plan_node or NodeKey(flow.name))] = flow
            self.collector.attempt_closed(attempts, None)
        except BaseException as raised:
            self.collector.attempt_closed(attempts, raised)
            self._launch_depth -= 1
            if top_level:
                # A launch that failed delivers nothing. The failures of its clean-ups are
                # logged, and the error of the launch propagates.
                try:
                    self._finish_launch(deliver=False)
                finally:
                    self._release_generation()
            raise
        self._launch_depth -= 1
        if top_level:
            # Only a requested flow that succeeded, or was found up to date, delivers. One whose
            # results report failure returns here, and delivers nothing, not even what its
            # producers noted.
            try:
                error = self._finish_launch(deliver=flow.succeeded)
            finally:
                self._release_generation()
            if error is not None:
                self.collector.finish_raised = error
                raise error
            if (
                self.settings.outputs_to is not None
                and flow.succeeded
                and not any(d.key == OUTPUTS_TO and d.state != "skipped" for d in flow.deliveries)
            ):
                warn_outputs_to_delivered_nothing(flow, self.settings.outputs_to)
        return flow

    def _enter_generation(self, design: Design) -> None:
        """Generate the sources of `design` and hold their storage until the launch ends.

        This is the generation stage of the requested flow, after every declarative check and
        before its first producer. It runs the generator unless the storage of its configuration
        is current, under the exclusive lock of the storage, and then keeps the shared lease of
        the storage, so nothing regenerates or removes it while a flow reads it. A design
        without a generator has nothing to generate. A failure is a `GenerationError`, an error of
        the whole launch.

        The launch holds the shared lease of the storage of each Git dependency that the design
        merged, from here on, so no fetch replaces its files while a tool reads them."""
        for record in design.resolved_dependencies:
            if record.storage is not None:
                self._leases.enter_context(
                    storage_lock(record.storage, exclusive=False, run_root=self.run_root)
                )
        generation = self._leases.enter_context(
            launch_generation.generated(
                design,
                run_root=self.run_root,
                rebuild_all=self.settings.rebuild_all or self.settings.clean,
                environ=dict(os.environ),
            )
        )
        execution = launch_generation.execution_design(design, generation)
        self._generation = _LaunchGeneration(design, generation, execution)
        if execution is not design:
            self._read_inputs.add(design_files(execution))

    def _release_generation(self) -> None:
        """Release the lease of the storage, once the launch has delivered everything."""
        self._generation = None
        self._leases.close()
        self._leases = ExitStack()

    def _execution_of(self, design: Design) -> Design:
        """Return the design that the flows of this launch get for the declared `design`."""
        current = self._generation
        return current.execution if current is not None and current.declared is design else design

    def _finish_launch(self, deliver: bool) -> Optional[Exception]:
        """Finish a top-level launch, and return the first failure, if any.

        With `deliver` (the launch completed, and its requested flow succeeded or was up to
        date), deliver the outputs noted for its flows (`_defer_delivery`), each under the lock
        of its run directory, in completion order. Then run the deferred clean-ups, which may
        remove a file that is delivered. A flow whose run directory a clean-up deletes delivers
        by moving its files. Each delivery and each clean-up is made on its own: a failure is
        logged and the next goes on."""
        self._claims = set()
        self._deliveries_ahead = {}
        self._predicted_copies = []
        deliveries, self._pending_deliveries = self._pending_deliveries, []
        # A flow whose run directory the clean-ups below delete gives its files away by rename. A
        # flow found up to date has no clean-up pending, so it is copied from and keeps its files.
        purged = {
            id(flow)
            for flow, _settings, _results, policy in self._pending_clean_ups
            if policy.purges
        }
        first_error: Optional[Exception] = None
        noted = [(flow, list(delivery.pending)) for flow, delivery in deliveries]
        # A file moves only if exactly one delivery of the whole launch reaches it, so the files
        # are counted before the first delivery is made: one flow's move must not leave another
        # flow's artifact, a link to that file, nothing to read.
        reached = (
            Deliveries.sources_reached(d for _flow, d in deliveries) if deliver and purged else None
        )
        for flow, delivery in deliveries if deliver else []:
            try:
                with run_dir_lock(flow.run_path, self.run_root):
                    flow.deliveries = delivery.deliver(move=id(flow) in purged, reached=reached)
            except Exception as e:  # noqa: BLE001 - every delivery gets its turn
                # `deliver()` records, and reports through `delivery.delivered`, whatever it
                # copied before raising (an `OSError` partway through), so a `--json` reader still
                # sees the copies that actually reached disk.
                flow.deliveries = delivery.delivered
                log.error("Delivering the outputs of %s failed: %s", flow.name, e)
                self.collector.finish_failed("delivery", e, flow.run_path)
                first_error = first_error or e
        for (flow, delivery), (_, pending_files) in zip(deliveries, noted):
            self.collector.deliveries_noted(flow, pending_files, delivery.delivered)
        pending, self._pending_clean_ups = self._pending_clean_ups, []
        for flow, settings_json, results_json, policy in pending:
            try:
                with run_dir_lock(flow.run_path, self.run_root):
                    self._clean_up(flow, settings_json, results_json, policy)
            except Exception as e:  # noqa: BLE001 - every clean-up gets its turn
                log.error("Cleaning up %s failed: %s", flow.run_path, e, exc_info=True)
                self.collector.finish_failed("clean-up", e, flow.run_path)
                first_error = first_error or e
        return first_error

    def _confirm_replacing(self, conflicts: Sequence[Conflict]) -> bool:
        """Ask `confirm_overwrite` whether to replace the files in the way.

        A file that the user said yes to is asked about once in a launch, while it stays as it
        was: the deliveries of a producer are checked ahead and again at its turn."""
        if self.confirm_overwrite is None:
            return False
        return self._confirmed_replacements.confirm(self.confirm_overwrite, conflicts)

    def _check_deliveries_ahead(
        self,
        plan: Plan,
        requested: PlanNode,
        design: Design,
        deliveries: Sequence[Delivery],
        run_path: Path,
        start_directory: Path,
    ) -> None:
        """Make now the checks that every flow of the plan makes of its deliveries at its turn.

        These are the checks of `Deliveries.check`. Making them now means that a refusal, or a
        question, never follows the run of an earlier flow.

        First comes what no answer could allow, for every flow, the requested flow included: a
        file that a flow reads and that lies in its own run directory, `--outputs-to`, and
        `Deliveries.refuse`. A destination that two deliveries name, or one inside another, is
        refused too (`refuse_shared_destinations`). It covers every destination known before
        the run: every named delivery of the launch, and the files that `--outputs-to` is
        expected to deliver from the last run of the requested flow. The files that only this
        run reveals are compared once it has run. Then come the questions of the producers, in
        the order they run, each asked once. The questions of the requested flow, which runs
        last, come last.

        So the launch has asked everything before any tool runs, before it scrubs older runs
        (`--scrub`) and before it takes the lock of its run directory. A launch that the user
        declines has removed nothing, and a question that waits for the user holds up no other
        launch or scrub of that directory. Each flow keeps the object it checked, and checks
        again with it at its turn. That finds the record that this check anchored, so the
        destination is read once in the launch, and the check asks again only about a file that
        changed meanwhile. At its turn, under its lock, the flow reads the delivery record again
        if another launch wrote it meanwhile.

        The copies of generated sources (`rtl.generator.outputs_to`) belong to the first flow
        that reads them. The destinations that the declared names give are checked now, before
        the generator runs. The names that a pattern matches are known once it has run
        (`_place_generated_deliveries`)."""
        # What no answer could allow comes first, for every flow of the plan: a file that a flow
        # reads and that lies in its own run directory, which Xeda empties and rewrites. A flow
        # checks it again at its turn, for a file made since.
        for planned in plan.nodes:
            refuse_inputs_inside(
                planned.run_path,
                planned.name,
                [*design_files(design), *setting_files(planned.settings)],
            )
        outputs_to = self.settings.outputs_to
        own = self._deliveries_of(run_path, deliveries, requested.name)
        generated = self._generated_by_node(
            plan, launch_generation.planned_deliveries(design, start_directory), design
        )
        own.generated = generated.get(requested.node_key, [])
        own.check_outputs_to(outputs_to)
        predicted = (
            outputs_to_deliveries(recorded_artifacts(run_path, self.run_root), run_path, outputs_to)
            if outputs_to is not None
            else []
        )
        own.refuse(predicted)
        #: Kept for the check after the generator ran (`_place_generated_deliveries`).
        self._predicted_copies = [(requested.name, d, d.destination) for d in predicted]
        ahead: dict[NodeKey, Deliveries] = {}
        copies: list[tuple[str, Delivery, Path]] = []  # in the order the flows run
        for planned in plan.nodes:
            if planned.node_key == requested.node_key:
                known = [*deliveries, *predicted]
            else:
                known = split_deliveries(planned.settings, design.name)
                if known or planned.node_key in generated:
                    ahead[planned.node_key] = self._deliveries_of(
                        planned.run_path, known, planned.name
                    )
                    ahead[planned.node_key].generated = generated.get(planned.node_key, [])
            copies += [
                (planned.name, d, d.destination)
                for d in [*known, *generated.get(planned.node_key, [])]
            ]
        for producer in ahead.values():
            producer.refuse()
        refuse_shared_destinations(copies, before_run=True)
        for producer in ahead.values():
            producer.check()
        own.check(predicted)
        ahead[requested.node_key] = own
        self._deliveries_ahead = ahead

    @staticmethod
    def _generated_by_node(
        plan: Plan, by_owner: Mapping[str | None, Sequence[Delivery]], design: Design
    ) -> dict[NodeKey, list[Delivery]]:
        """Group the copies of generated sources (by the owner of their generator) by the node
        that delivers them: the first node of the plan that reads sources of that generator."""
        readers = launch_generation.reading_nodes(plan.nodes, design)
        grouped: dict[NodeKey, list[Delivery]] = {}
        for owner, copies in by_owner.items():
            reader = readers.get(owner)
            if reader is not None and copies:
                grouped.setdefault(reader.node_key, []).extend(copies)
        return grouped

    def _place_generated_deliveries(
        self, plan: Plan, design: Design, start_directory: Path
    ) -> None:
        """Give each node its copies of the generated sources, now that the generator has run, and
        check them as the other deliveries are checked.

        The copies are the files that the declarations matched in the storage. They replace the
        destinations that the declared names gave (`_check_deliveries_ahead`), and add those of
        the patterns. Nothing of the plan has run yet, so a refusal or a question still comes
        before any tool of a flow."""
        generation = self._generation.generation if self._generation else None
        if generation is None:
            return
        by_node = self._generated_by_node(
            plan,
            launch_generation.generated_deliveries(design, generation, start_directory),
            design,
        )
        touched: list[Deliveries] = []
        for node in plan.nodes:
            copies = by_node.get(node.node_key)
            ahead = self._deliveries_ahead.get(node.node_key)
            if copies is None and (ahead is None or not ahead.generated):
                continue
            if ahead is None:
                ahead = self._deliveries_ahead[node.node_key] = self._deliveries_of(
                    node.run_path, [], node.name
                )
            ahead.generated = copies or []
            touched.append(ahead)
        if not touched:
            return
        refuse_shared_destinations(
            [
                *(
                    (other.owner, copy, copy.destination)
                    for other in self._deliveries_ahead.values()
                    for copy in [*other.named, *other.generated]
                ),
                *self._predicted_copies,
            ],
            before_run=True,
        )
        for ahead in touched:
            ahead.refuse()
        for ahead in touched:
            ahead.check()

    def _deliveries_of(self, run_path: Path, named: Sequence[Delivery], owner: str) -> Deliveries:
        """Return the deliveries `named` of the flow `owner` that runs in `run_path`.

        They are the deliveries for this launch."""
        return Deliveries(
            run_path,
            self.run_root,
            named,
            inputs=self._read_inputs,  # the launch's
            overwrite=self.settings.overwrite_outputs,
            confirm=self._confirm_replacing,
            owner=owner,
            files=self._delivered_files,  # the launch's
        )

    def _claim_run_dir(self, run_path: Path) -> None:
        """Record that this launch entered `run_path` (`launch_identity.claim_run_dir`)."""
        launch_identity.claim_run_dir(self._claims, run_path)

    def _run_dir_policy(self) -> RunDirPolicy:
        """Return what this launch does with its run directory.

        See `launch_identity.run_dir_policy`."""
        return launch_identity.run_dir_policy(self.settings)

    def _producer_read_lease(self, producer: Flow) -> AbstractContextManager[Flow]:
        """Take a shared lock on the completed run of `producer`, and verify it.

        This comes before any consumer reads the files of the producer
        (`launch_producers.producer_read_lease`)."""
        return launch_producers.producer_read_lease(producer, self.run_root, self._completed_runs)

    def _entered(self, run_path: Path) -> bool:
        """Return whether this launch built a flow for `run_path`.

        If it did, the `results.json` there is its own and not one that an earlier launch left."""
        where = Path(os.path.abspath(run_path))
        return any(
            Path(os.path.abspath(f.run_path)) == where
            for f in self.launched[self._launched_before :]
        )

    @with_node_context
    def _launch(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        depender: Optional[Flow],
        all_flows_settings: Union[Dict, None],
        *,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
        consumer: str | None = None,
    ) -> Flow:
        """Run the stages of `launch_flow` for one flow.

        A launch serves a consumer when `depender` (a producer of a declared input) or `consumer`
        (the label of the node whose design this flow converts: the consumer has no flow yet)
        is given. Such a launch is a producer launch: it delivers nothing to `--outputs-to`,
        refreshes no trace, takes a completion token and suggests no node for a failing
        setting."""
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        runner_cwd = Path.cwd()
        if plan is None:
            request = self._request_context
            run_flows = {flow_name, *transitive_dependencies(flow_class, design)}
            plan = self.resolve(
                flow_class,
                design,
                flow_settings,
                all_flows_settings,
                origins=request.origins if request else (),
                command_line=(
                    {
                        name: values
                        for name, values in request.command_line.items()
                        if name in run_flows
                    }
                    if request
                    else None
                ),
                api_overrides=request.api_overrides if request else None,
                binding_layers=request.binding_layers if request else (),
            )
        node = self._validate_plan(
            plan, flow_class, design, flow_settings, all_flows_settings, plan_node
        )
        if plan_node is None:
            self.collector.planned(plan)
        producer_launch = depender is not None or consumer is not None
        if not producer_launch and self.settings.outputs_to is not None:
            # Before the run root, a lock or a producer.
            refuse_outputs_to_a_programmer(plan, node, self.settings.outputs_to)
        flow_settings = node.settings
        # The design the node is planned to read: the plan checked the flow against it.
        planned_view = planned_design_of(plan, node, design, self.run_path_of)
        input_settings = launch_input.input_settings(
            flow_class, flow_settings, planned_view, runner_cwd, producer_launch, debug=self.debug
        )
        # A deliverable given as a location becomes its conventional name here, before the
        # identity; the tools write that, and a delivery copies it to the location.
        deliveries = split_deliveries(input_settings, design.name)
        # From here the launch acts on the node, and the node reports its end whatever raises.
        self.collector.attempt_begun(node)
        flowrun_hash, run_path, settings_hash = launch_identity.run_identity(
            self, flow_class, flow_name, design, input_settings, node
        )
        # What the flows read is an input that no delivery of the launch may replace, or land
        # beside in a directory that a setting names. That covers every file that their
        # settings name, and every file under a directory that one names, as the trace lists
        # it. The requested flow registers the reads of the whole plan, and makes the checks of
        # the deliveries of every flow, before any flow is entered: a refusal never comes after
        # the tool of an earlier flow ran.
        self._read_inputs.add(design_files(design))
        self._read_inputs.add_source_entries(design.source_entries())
        if plan_node is None:
            for planned in plan.nodes:
                register_read_settings(
                    self._read_inputs, planned.settings, planned.run_path, self.run_root
                )
            self._check_deliveries_ahead(plan, node, design, deliveries, run_path, runner_cwd)
        refuse_inputs_inside(
            run_path, flow_name, [*design_files(design), *setting_files(input_settings)]
        )
        if plan_node is None:
            # Every declarative check has passed. The generator of the design runs now, before
            # the first producer and before any lock of a run directory (a lock of the storage
            # is taken first by every launch, so two launches cannot wait for each other).
            self._enter_generation(design)
            self._place_generated_deliveries(plan, design, runner_cwd)
        # What the flows read: the design with the files of its generated sources in place.
        execution = self._execution_of(design)
        design_hash = execution.parts_hash(
            flow_class.design_parts, flow_class.design_scope(input_settings)
        )
        policy = self._run_dir_policy()
        self._claim_run_dir(run_path)
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        # Scrub siblings before taking the lock of this run directory: concurrent hashed
        # variants must not each hold their directory while waiting to delete the other one.
        if policy.scrub_old_runs:
            scrub_runs(
                flow_name,
                run_path.parent,
                [run_path],
                run_root=self.run_root,
                confirm=self.confirm_scrub,
                on_result=self.scrub_done,
            )
        with run_dir_lock(run_path, self.run_root), ExitStack() as read_leases:
            run_path.mkdir(parents=True, exist_ok=True)
            run_directory = RunDirectory.claimed(run_path, self.run_root)
            # The deliveries are checked again before `--clean`, before the `init()` of this
            # flow and before any producer is launched, so before any tool of the plan runs.
            # Each flow, the requested one included, was asked about its files before the scrub
            # above and before this lock was taken (`_check_deliveries_ahead`). A file that
            # changed since is asked about again.
            outputs_to = None if producer_launch else self.settings.outputs_to
            delivery = self._deliveries_ahead.pop(node.node_key, None)
            if delivery is None:
                delivery = self._deliveries_of(run_path, deliveries, flow_name)
            else:
                # Made ready before this lock was taken: the record may have been written since.
                delivery.reread_record()
            # The directory itself, not only a file predicted from the artifacts of the last run.
            # A location becomes a concrete `Delivery` only once its tool has run and reported
            # an artifact. Without this check, a run root or an input named by `--outputs-to` is
            # refused only after the tools of the requested flow (those of its producers
            # included) have already run.
            delivery.check_outputs_to(outputs_to)
            delivery.check(
                outputs_to_deliveries(
                    recorded_artifacts(run_path, self.run_root), run_path, outputs_to
                )
            )
            # The design stage. A flow that reads a design a converter derives gets its producer
            # launched here, before the flow exists: the derived design decides what the flow is
            # built with and how its design is hashed. A failure is the failure of this flow
            # too, and is recorded as one below; the flow is then built on the planned design.
            assert plan is not None and node is not None
            handover: launch_producers.DesignHandOver | None = None
            design_failure: Exception | None = None
            derived = execution
            try:
                # A generated source that the node reads and the generator did not write fails
                # the node.
                launch_generation.refuse_missing(
                    design,
                    self._generation.generation if self._generation else None,
                    flow_class,
                    input_settings,
                )
                handover = launch_producers.run_design_producer(
                    flow_class,
                    design,
                    execution,
                    node,
                    plan,
                    all_flows_settings,
                    read_leases,
                    launch_flow=self.launch_flow,
                    read_lease=self._producer_read_lease,
                    completed=self._planned_completed,
                    entered=self._entered,
                )
                if handover is not None:
                    derived = handover.design
                    # The derived design is checked as the plan checked the planned one. One the
                    # plan did not foresee is refused before the flow exists.
                    check_launchable(flow_class, input_settings, derived)
                    design_hash = derived.parts_hash(
                        flow_class.design_parts, flow_class.design_scope(input_settings)
                    )
                    derived_files = design_files(derived)
                    self._read_inputs.add(derived_files)
                    refuse_inputs_inside(
                        run_path, flow_name, [*derived_files, *setting_files(input_settings)]
                    )
            except Exception as e:  # noqa: BLE001 - recorded below, as any producer failure
                design_failure = e
                derived = planned_design_of(plan, node, execution, self.run_path_of)
                design_hash = ""
            log.debug("Instantiating flow from %s", flow_class)
            # A copy of each, which the flow completes for itself. The design, like the
            # settings, has been hashed already, is recorded beside that hash, and goes on to
            # the producers of the flow, none of which may see what the flow does to it.
            flow = flow_class(
                input_settings.model_copy(deep=True),
                derived.model_copy(deep=True),
                run_path,
                runner_cwd=runner_cwd,
                run_directory=run_directory,
            )
            untouched = False
            failure: BaseException | None = None
            try:
                flow.design_hash = design_hash
                flow.configuration = node.configuration
                flow.generation_keys = (
                    dict(self._generation.generation.keys)
                    if self._generation is not None and self._generation.generation is not None
                    else {}
                )
                flow.flow_hash = flowrun_hash
                flow.settings_hash = settings_hash
                flow.started = datetime.now(timezone.utc)
                flow.timestamp = launch_run.timestamp_text(flow.started)
                # The run time of the flow includes init() and the execution of its producers.
                flow.init_time = time.monotonic()
                # One id for this execution, whether it ends well or not: the record, the trace and
                # the consumers of the run name it.
                flow.run_id = uuid.uuid4().hex
                try:
                    if design_failure is not None:
                        raise design_failure
                    flow.init()
                    producers = launch_producers.run_producers(
                        flow,
                        design,
                        node,
                        plan,
                        all_flows_settings,
                        read_leases,
                        launch_flow=self.launch_flow,
                        read_lease=self._producer_read_lease,
                        completed=self._planned_completed,
                        read_inputs=self._read_inputs,
                        entered=self._entered,
                        design_handover=handover,
                        located=lambda path: launch_generation.located_paths(
                            self._generation.generation if self._generation else None, path
                        ),
                    )
                    flow.producer_run_ids = {
                        str(producer.run_path): str(producer.run_id) for producer in producers
                    }
                    self.collector.producers_done(node)
                    if policy.clean:
                        # Only now: a flow that a failed producer blocks does not run, so it
                        # keeps what its run directory holds, its record included.
                        if self.settings.backups:
                            backup_existing(run_path)
                            run_path.mkdir(parents=True, exist_ok=True)
                        else:
                            run_directory.clear()
                    # Producers have their own program records. Preparation belongs to this
                    # consumer and happens before its execution recording scope/snapshot.
                    with recording_programs() as programs:
                        flow.prepare_inputs()
                    prepared = registered_input_files(flow)
                    located = located_input_files(flow)
                    if located != prepared:
                        raise FlowFatalError(
                            f"{flow.name} registered {[str(p) for p in prepared]} as its prepared "
                            f"inputs, and locate_prepared_inputs() says "
                            f"{[str(p) for p in located] if located is not None else None}: a "
                            "dry run would judge it by the latter"
                        )
                    self._read_inputs.add(prepared)
                    refuse_inputs_inside(flow.run_path, flow.name, prepared)
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    if isinstance(e, DeliveryError) and e.before_run:
                        # The delivery of a producer was refused before its tool ran. The directory
                        # of this flow still describes its last run, and an earlier producer that
                        # ran meanwhile makes the next launch stale through its new run id. The
                        # flow did not run, and the launch does not list it as launched.
                        untouched = True
                        raise
                    if isinstance(e, FlowDependencyFailure) and e.producer:
                        # A producer did not pass, so this flow is blocked: it does not run and
                        # writes nothing. Its run directory keeps its last record and trace.
                        launch_run.record_identity(flow, run_path)
                        flow.results.success = False
                        flow.results["error"] = error_info(
                            e, node=flow.name, flow=flow
                        ).as_json_value()
                        flow.blocked_by = e.producer
                        raise
                    # The flow could not start: a problem with what its producers hand over, or
                    # with the flow itself. Its directory no longer vouches for a run.
                    remove_trace(run_path)
                    run_directory.remove(results_json)
                    launch_run.record_identity(flow, run_path)
                    flow.results.success = False
                    flow.results["error"] = error_info(e, node=flow.name, flow=flow).as_json_value()
                    report(
                        flow,
                        derived,
                        results_json,
                        self.settings,
                        record=True,
                        run_root=self.run_root,
                    )
                    raise

                # Only now: what the flow consumes includes the outputs and runs of its producers.
                always = flow.always_runs()
                expected = (
                    None
                    if always is not None
                    else expectation(
                        flow, derived, input_settings, self.run_root, producers, self._code_digests
                    )
                )
                if expected is None:
                    flow.stale_reason = always
                    log.info("Running %s: %s", flow.name, always)
                elif not self.settings.rebuild_all and not policy.clean:
                    # A second consumer may be acquiring a lease on this same run. Reuse must
                    # not change its trace or directory through a clock marker.
                    freshness = check_trace(
                        run_path, expected, locate_program, refresh=not producer_launch
                    )
                    if freshness.fresh and reuse_results(flow, self.run_root, freshness):
                        if freshness.refreshed is not None:
                            write_trace(run_path, freshness.refreshed)
                        flow.run_id = freshness.run_id
                        launch_run.record_identity(flow, run_path)
                        if flow.rejected_again:
                            log.warning(
                                "%s. Its last run with these inputs and settings was rejected: "
                                "Xeda reports that again and does not run it. To run it again, "
                                "use --rebuild-all. A file that the run reads and that Xeda "
                                "does not track is no input: declare it as a source of the "
                                "design.",
                                flow.rejection,
                            )
                        else:
                            note = ran_again_note(
                                expected.declared_inputs,
                                {str(p.run_path) for p in producers if not p.reused},
                            )
                            log.info("%s is up to date (%s)", flow.name, note or run_path)
                        for name in freshness.equivalent:
                            log.info(
                                "%s: `%s` differs from the one it consumed only in what its "
                                "type ignores (for a JSON netlist, source locations): its "
                                "results are those of the run on the earlier one",
                                flow.name,
                                name,
                            )
                        report(
                            flow,
                            derived,
                            results_json,
                            self.settings,
                            record=False,
                            run_root=self.run_root,
                        )
                        if flow.succeeded:
                            self._defer_delivery(flow, delivery, outputs_to)
                        return flow
                    flow.stale_reason = (
                        freshness.reason or f"no completed results in {results_json}"
                    )
                    log.info("Running %s: %s", flow.name, flow.stale_reason)
                else:
                    flow.stale_reason = (
                        EVERY_FLOW_RUNS_CLEAN if policy.clean else EVERY_FLOW_RUNS_REBUILD_ALL
                    )
                    log.info("Running %s: %s", flow.name, flow.stale_reason)

                previous = previous_trace(run_path, flow.name)
                # The records of Xeda itself never write through a link at their names (`writable`).
                if self.settings.dump_settings_json:
                    run_directory.writable(settings_json)
                # The results of the last run are never this run's. They are gone before this one
                # starts, like the trace, so a run that fails in any way cannot leave them behind.
                run_directory.remove(results_json)
                run_directory.writable(results_json)
                # From here until a new trace is written, nothing vouches for this directory.
                remove_trace(run_path)
                if previous is not None:
                    # The reports that the last run read are not this run's, whatever their mtime
                    # (`Flow.report_file` is a second line of defense).
                    run_directory.remove(*(p for p in previous.reports if run_directory.holds(p)))
                # The run starts now: its inputs are recorded as it finds them, so that a file
                # edited while it runs no longer matches what its trace says it consumed.
                snapshot = (
                    None if expected is None else snapshot_inputs(expected, previous, run_path)
                )
                all_settings = dict(
                    design=derived,
                    design_hash=design_hash,
                    rtl_fingerprint=derived.rtl_fingerprint,
                    rtl_hash=derived.rtl_hash,
                    flow_name=flow.name,
                    flow_settings=input_settings,
                    effective_flow_settings=flow.settings,
                    xeda_version=__version__,
                    flowrun_hash=flowrun_hash,
                    settings_hash=settings_hash,
                    deliveries=[
                        {"setting": d.key, "name": str(d.name), "to": str(d.destination)}
                        for d in deliveries
                    ],
                    outputs_to=outputs_to,
                )
                if self.settings.dump_settings_json:
                    log.info("writing prepared settings to %s", settings_json)
                    dump_json(all_settings, settings_json, backup=self.settings.backups)
                # A tool does not make the directory of a file it is told to write: the directory
                # of the conventional name, inside the run directory (relative, never `..`).
                for d in deliveries:
                    (run_path / d.name).parent.mkdir(parents=True, exist_ok=True)
                # Never through a link that a tool left leading out of the run directory.
                launch_run.free_working_locations(flow)
                # From now on, what the run directory holds is this run's own only if the run
                # writes it (`Flow.wrote_output`, by identity): a report left from before is a
                # previous run's (`Flow.report_file`).
                flow.start_run()
                try:
                    with recording_programs() as execution_programs:
                        launch_run.execute(flow, run_path, input_settings)
                        if not flow.succeeded:
                            # A run that ended without raising, its tool's failure or its checks'
                            # included, reports only the artifacts it wrote; a run that raised
                            # gets the same filter in the handler below.
                            _drop_unwritten_artifacts(flow)
                    for name in execution_programs:
                        if name not in programs:
                            programs.append(name)
                            if name in execution_programs.before:
                                programs.before[name] = execution_programs.before[name]
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    flow.results.success = False
                    flow.results["error"] = error_info(e, node=flow.name, flow=flow).as_json_value()
                    if flow.init_time is not None:
                        flow.results.runtime = time.monotonic() - flow.init_time
                    for k, v in flow.artifacts.items():
                        flow.results.artifacts.setdefault(k, v)
                    _drop_unwritten_artifacts(flow)
                    launch_run.drop_bad_artifacts(flow)  # the error of the exception stays
                    try:
                        report(
                            flow,
                            derived,
                            results_json,
                            self.settings,
                            record=True,
                            run_root=self.run_root,
                        )
                    except Exception as problem:  # noqa: BLE001 - the run's own error comes first
                        log.error("The record of %s could not be written: %s", flow.name, problem)
                    raise
                if self.settings.dump_settings_json:
                    # `run()` may finish resolving generated files or derived switches. Keep the
                    # pre-run write above so a crash still leaves useful diagnostics, then replace
                    # its effective snapshot after a completed execution without backing up that
                    # transient snapshot.
                    dump_json(all_settings, settings_json, backup=False)
                report(
                    flow, derived, results_json, self.settings, record=True, run_root=self.run_root
                )
                if (
                    expected is not None
                    and snapshot is not None
                    and (
                        flow.outcome == "passed"
                        or (flow.outcome == "rejected" and self.settings.dump_results_json)
                    )
                ):
                    # A rejected run keeps a trace only if `results.json` was written: a replay
                    # reads the rejection from it. Recorded once the run has written everything,
                    # `results.json` included, and before the clean-up, which may remove files the
                    # trace names (a depfile). The clean-up only removes files: the trace is the
                    # last thing a run writes.
                    trace = build_trace(
                        expected,
                        flow,
                        programs,
                        snapshot,
                        input_settings,
                        previous,
                        run_id=str(flow.run_id),
                    )
                    write_trace(run_path, trace)
                if flow.succeeded:
                    self._defer_delivery(flow, delivery, outputs_to)
                if policy.post_cleanup:
                    self._pending_clean_ups.append((flow, settings_json, results_json, policy))
            except BaseException as raised:
                failure = raised
                raise
            finally:
                if not untouched:
                    # Before anything below that can raise: the node reports its end once.
                    self.launched.append(flow)
                    self.collector.node_ended(node, flow, failure)
                if flow.succeeded and producer_launch:
                    # Only producers need a hand-over token. A requested run may succeed with
                    # unknown output evidence, whose trace correctly requires another run.
                    recorded = previous_trace(run_path, flow.name)
                    token = CompletedRun.capture(
                        run_path,
                        [
                            p
                            for p in artifact_files(flow)
                            if not p.is_relative_to(run_path.resolve())
                        ],
                        recorded.outputs if recorded else None,
                        recorded.outputs_recorded_ns if recorded else None,
                    )
                    self._completed_runs[run_path.resolve()] = (flow, token)
        return flow

    def _defer_delivery(self, flow: Flow, delivery: Deliveries, outputs_to: Optional[Path]) -> None:
        """Note what `flow` delivers: its named outputs, and its artifacts for `--outputs-to`.

        The files are noted one by one as its run left them (`Deliveries.collect`, under the lock
        of its run directory). The copies are made when the launch has finished
        (`_finish_launch`), once every flow of it has read its inputs."""
        artifacts = [
            flow.results.get("artifacts") or flow.artifacts,
            declared_output_files(flow.results),
        ]
        delivery.collect(flow.run_path, outputs_to_deliveries(artifacts, flow.run_path, outputs_to))
        if not delivery.pending and not delivery.skipped:
            return
        # What only a run reveals (the artifacts for `--outputs-to`, the files of a directory
        # output) is compared with every copy noted so far, before any copy is made.
        refuse_shared_destinations(
            (
                (other.name, named, destination)
                for other, noted in [*self._pending_deliveries, (flow, delivery)]
                for named, _source, destination, _sha in noted.pending
            ),
            before_run=False,
        )
        self._pending_deliveries.append((flow, delivery))

    def _clean_up(
        self, flow: Flow, settings_json: Path, results_json: Path, policy: RunDirPolicy
    ) -> None:
        """Clean up after a run, as `policy` says.

        Either purge the run directory, or prune it to what the run reports and records
        (`settings.json`, `results.json`, artifacts).

        Pruning removes the trace first. Until every flow declares every output it writes, a
        pruned directory may lack a file that a consumer reads, so the run must not be reused,
        not even if the pruning is interrupted halfway."""
        if policy.post_cleanup:
            remove_trace(flow.run_path)
            if policy.purges:
                log.warning("Deleting run directory %s", flow.run_path)
                # With every name of it, the link that the launch named it by included, so none
                # is left leading nowhere.
                flow.run_directory.delete(
                    *run_directory_names(flow.run_path, flow.name, self.run_root)
                )
            else:
                log.warning("Cleaning up %s", flow.run_path)
                named_run_path = Path(os.path.abspath(flow.run_path))
                run_path = flow.run_path.resolve()
                kept: set[Path] = set()
                for raw_path in (
                    settings_json,
                    results_json,
                    *iter_artifact_paths(flow.results.artifacts),
                    *declared_output_files(flow.results),
                ):
                    path = Path(os.path.abspath(named_run_path / raw_path))
                    if path.is_relative_to(named_run_path):
                        # Resolve the run-directory alias without resolving an artifact's
                        # own symlink: both that link and its internal target must survive.
                        path = run_path / path.relative_to(named_run_path)
                    target = path.resolve()
                    if path.is_relative_to(run_path):
                        kept.add(path)
                        # A retained symlink to an internal file needs its target as well.
                        if target.is_relative_to(run_path):
                            kept.add(target)
                    elif target.is_relative_to(run_path):
                        # Absolute paths may spell the run directory through a symlink.
                        kept.add(target)

                ancestors = {
                    parent for path in kept for parent in path.parents if parent != run_path
                }
                removed: list[Path] = []

                def prune(directory: Path) -> None:
                    for path in directory.iterdir():
                        if path in kept:
                            continue
                        if path in ancestors and path.is_symlink():
                            continue
                        if path in ancestors and path.is_dir() and not path.is_symlink():
                            prune(path)
                        else:
                            removed.extend(flow.run_directory.remove(path))

                if run_path.is_relative_to(self.run_root) and run_path not in kept:
                    prune(run_path)
                log.warning("Removed the following files: %s", " ".join(str(p) for p in removed))

    @log_context.with_launch_context
    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None] = None,
        *,
        depender: Optional[Flow] = None,
        all_flows_settings: Union[Dict, None] = None,
        plan: Plan | None = None,
    ) -> Optional[Flow]:
        """Launch `flow_class` on `design`, which the caller has built.

        Its settings come from the layers that `run()` composes, lowest first: the `flows`
        sections of the design itself, then `all_flows_settings` (more `flows` sections,
        composed into `flow_settings` by `compose_flow_settings`), then `flow_settings`. A
        `Flow.Settings` instance is taken as final. `run()` and `plan()` also read a project
        file. A built design names none, so hand its sections in as `all_flows_settings`."""
        if plan is None and depender is None:
            flow_cls = get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class
            sections, section_bindings = split_bindings(
                all_flows_settings or {}, location=SUPPLIED_SECTIONS_ORIGIN
            )
            own, own_bindings = split_bindings(
                {flow_cls.name: flow_settings or {}}, location=API_ORIGIN, kind="api"
            )
            layers = (section_bindings, own_bindings)
            # A flow that declares no inputs has none to bind: an `inputs` key is an error.
            effective_bindings(layers, default_nodes([flow_cls]))
            all_flows_settings = sections
            if not isinstance(flow_settings, Flow.Settings):
                flow_settings = own[flow_cls.name]
            if any(layer.entries or layer.invalid_inputs for layer in layers):
                plan = self.resolve(
                    flow_cls, design, flow_settings, sections, binding_layers=layers
                )
        if (
            plan is None
            and all_flows_settings
            and depender is None
            and not isinstance(flow_settings, Flow.Settings)
        ):
            flow_cls = get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class
            sections = merge_flow_sections(
                all_flows_settings, flow_class_for=_get_flow_class_if_known
            )
            flow_settings = compose_flow_settings(flow_cls, [sections], flow_settings or {})
            all_flows_settings = sections
        return self.launch_flow(
            flow_class,
            design,
            flow_settings,
            depender=depender,
            all_flows_settings=all_flows_settings,
            plan=plan,
        )

    def _refuse_unaccepted_request(
        self,
        flow: Union[Type[Flow], str, FlowRequest],
        flow_settings: Any,
        flow_overrides: Any,
        xedaproject: str | Path | None,
        design: Any = None,
    ) -> None:
        """Refuse, before anything is loaded, what a launcher that takes no requests refuses.

        Such a launcher is `Dse`. What it refuses here needs no
        design: a chain, and an input binding of the requested node given on the command line,
        through the API or in the project file (`LOCAL_REQUESTS_ONLY`). Planning fetches the
        Git dependencies of a design into the run root, so a refusal that came after it would
        leave the fetch behind.

        What does need the loaded design is refused later, with the same error: a binding of the
        requested node saved in the design file, and a binding of any other node (whether the
        request reaches that node depends on its resolved graph, so an unrelated saved binding is
        no refusal). Every request reaches the requested node itself. `plan` and `run_flow` do
        not call this: `run_flow` takes a design that is already built."""
        if isinstance(flow, FlowRequest):
            if len(flow.elements) > 1:
                raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
            requested = flow.requested
        elif isinstance(flow, str):
            if "+" in flow:
                raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
            requested = parse_request(flow).requested
        else:
            requested = flow
        from .resolver import _explicit

        # The layers that a request reads bindings from, taken out as `_request` takes them.
        explicit = (
            _explicit(flow_settings)
            if isinstance(flow_settings, Flow.Settings)
            else settings_to_dict(flow_settings)
        )
        cli_sections, cli_own = split_flow_sections(
            explicit, requested.name, flow_class_for=_get_flow_class_if_known
        )
        cli_sections = merge_flow_sections(
            cli_sections, {requested.name: cli_own}, flow_class_for=_get_flow_class_if_known
        )
        layers = [
            split_bindings(cli_sections, location=COMMAND_LINE_ORIGIN, kind="cli")[1],
            split_bindings(
                {
                    requested.name: (
                        flow_overrides
                        if isinstance(flow_overrides, dict)
                        else settings_to_dict(flow_overrides)
                    )
                },
                location=API_ORIGIN,
                kind="api",
            )[1],
        ]
        # Only a design that the project selects gets the project layer.
        from_project = not (
            isinstance(design, (Design, dict, Path))
            or (isinstance(design, str) and names_a_design_file(design))
        )
        project_path = Path(xedaproject) if xedaproject else None
        if project_path is not None and from_project:
            try:
                project: Project | None = Project.load(project_path)
            except ProjectError:
                project = None  # `_request` reports it, before it loads any design
            if project is not None:
                layers.append(
                    split_bindings(project.flows, location=f'the project file "{project_path}"')[1]
                )
        require_no_bindings(layers, [NodeKey(requested.name)])

    @log_context.with_launch_context
    def run(
        self,
        flow: Union[Type[Flow], str],
        design: Union[str, Path, Design, Dict[str, Any], None] = None,
        xedaproject: Optional[str] = None,
        flow_settings: Union[  # the command line's `-s`: overrides the design and project
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
            Flow.Settings,
        ] = [],
        flow_overrides: Union[
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
        ] = [],
        design_updates: Union[Iterable[str], Dict[str, Any], None] = None,
        design_allow_extra: bool = False,
        design_remove_fields: List[str] = [],
        target: str | None | Unset = UNSET,
        local_settings: LocalSettings | None = None,
    ) -> Optional[Flow]:
        """Load and compose a request, then execute its resolved declared graph.

        `target` selects one of the `targets` of the design, as `Design.load` does.
        `local_settings` are the private settings of the user and of the checkout, as
        `xeda.local_settings()` reads them: two layers of the settings (`settings_layers`). The
        launcher reads no private file."""
        request, plan = self.resolve_run(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target=target,
            local_settings=local_settings,
        )
        return self.run_resolved(request, plan)

    @log_context.with_launch_context
    def resolve_run(
        self,
        flow: Union[Type[Flow], str],
        design: Union[str, Path, Design, Dict[str, Any], None] = None,
        xedaproject: Optional[str] = None,
        flow_settings: Union[
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
            Flow.Settings,
        ] = [],
        flow_overrides: Union[
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
        ] = [],
        design_updates: Union[Iterable[str], Dict[str, Any], None] = None,
        design_allow_extra: bool = False,
        design_remove_fields: List[str] = [],
        target: str | None | Unset = UNSET,
        local_settings: LocalSettings | None = None,
    ) -> Tuple[_Request, Plan]:
        """Load, compose and check a request as `run()` does before it launches, and launch nothing.

        Return the request, which holds the loaded design, and its plan. `run_resolved` launches
        them."""
        # Where requests are not taken (`Dse`), refuse before loading what needs no design.
        # What does need it is listed there, and refused below, once the request is resolved.
        if not self.accepts_bindings:
            self._refuse_unaccepted_request(
                flow, flow_settings, flow_overrides, xedaproject, design
            )
        request, plan = self._judged_request(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target=target,
            local_settings=local_settings,
        )
        self._refuse_unaccepted_plan(request, plan)
        return request, plan

    @log_context.with_launch_context
    def run_resolved(self, request: _Request, plan: Plan) -> Optional[Flow]:
        """Execute the request and plan that `resolve_run` returned."""
        previous, self._request_context = self._request_context, request
        try:
            return self.run_flow(
                request.flow_class,
                request.design,
                request.settings,
                all_flows_settings=request.sections,
                plan=plan,
            )
        finally:
            self._request_context = previous

    @log_context.with_launch_context
    def plan(
        self,
        flow: type[Flow] | str,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any] = [],
        design_updates: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        design_remove_fields: list[str] = [],
        target: str | None | Unset = UNSET,
        local_settings: LocalSettings | None = None,
    ) -> Plan:
        """Plan what `run()` would execute.

        Fetch the Git dependencies of the design into the run root, and refuse what `run()`
        would refuse before anything runs: a run directory that a flow refuses, and
        `--outputs-to` for a programmer."""
        return self._plan(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target,
            local_settings,
        )[1]

    @log_context.with_launch_context
    def prepare_request(
        self,
        flow: type[Flow] | str | FlowRequest,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        design_updates: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        target: str | None | Unset = UNSET,
        *,
        host_flows: Mapping[str, Mapping[str, Any]] | None = None,
        host: str | None = None,
        local_settings: LocalSettings | None = None,
        before_fetch: Callable[[Design], None] | None = None,
    ) -> PreparedRequest:
        """Load, resolve and check a request as `run()` does before it launches, and launch nothing.

        The checks are those of `run()`: what the design declarations show, the plan of a launcher
        that takes no chain or binding, and what a launch refuses of the plan (a run directory
        that a flow refuses, `--outputs-to` for a programmer). Like `plan()`, it fetches the Git
        dependencies of the design into the run root (`dependencies.merged_design`).

        `host_flows` is the `flows` table of a host profile (flow name to settings), and `host`
        names that profile in messages ("the host profile of <host>"). The table is the lowest
        layer of the settings of every flow, below the `flows` section of the project file. Only
        this method takes it.

        `before_fetch` is a check of the design as the caller gave it. It runs after the checks of
        the settings and before any dependency is read, so before a Git dependency is resolved or
        fetched. A remote request refuses here what needs no value of a dependency."""
        if not self.accepts_bindings:
            self._refuse_unaccepted_request(flow, flow_settings, [], xedaproject, design)
        request, plan = self._judged_request(
            flow,
            design,
            xedaproject,
            flow_settings,
            [],
            design_updates,
            design_allow_extra,
            [],
            target=target,
            before_fetch=before_fetch,
            host_layer=(host_profile_origin(host), host_flows) if host_flows is not None else None,
            local_settings=local_settings,
        )
        self._refuse_unaccepted_plan(request, plan)
        self._refuse_planned(plan, run_directories_here=host_flows is None)
        return PreparedRequest(
            request.design,
            request.sections,
            plan,
            request.flow_request,
            request.design.target,
            request.declared,
        )

    @log_context.with_launch_context
    def plan_with_freshness(
        self,
        flow: type[Flow] | str,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any] = [],
        design_updates: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        design_remove_fields: list[str] = [],
        target: str | None | Unset = UNSET,
        local_settings: LocalSettings | None = None,
    ) -> tuple[Plan, dict[str, NodeFreshness]]:
        """Return `plan()` and what `run()` would do with each node.

        The state of a node is `fresh` (reused), `stale` (run, for the reason it logs) or
        `unknown` (it cannot be told without running a producer, running the generator of the
        design, or looking up something that only a process can). This is read-only: nothing is created, locked, refreshed or started
        (`plan_freshness`). `--rebuild-all` and `--clean` make every node stale."""
        request, plan = self._plan(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target,
            local_settings,
        )
        states = judge_plan(
            plan,
            request.design,
            run_root=self._run_root,
            runner_cwd=Path.cwd(),
            rebuild_all=self.settings.rebuild_all,
            clean=self._run_dir_policy().clean,
            debug=self.debug or self.settings.debug,
            run_path=self.run_path_of,
        )
        return plan, states

    def _plan(
        self,
        flow: type[Flow] | str,
        design: str | Path | Design | dict[str, Any] | None,
        xedaproject: str | None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings,
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any],
        design_updates: Iterable[str] | dict[str, Any] | None,
        design_allow_extra: bool,
        design_remove_fields: list[str],
        target: str | None | Unset,
        local_settings: LocalSettings | None = None,
    ) -> tuple[_Request, Plan]:
        request, plan = self._judged_request(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target=target,
            local_settings=local_settings,
        )
        self._refuse_planned(plan)
        return request, plan

    def _refuse_planned(self, plan: Plan, *, run_directories_here: bool = True) -> None:
        """Refuse what a launch refuses of a resolved plan before anything runs.

        A plan reports it too: a run directory that a flow cannot work in, and `--outputs-to`
        for a programmer. A remote run builds in run directories on the remote host, and the
        launch there judges them (`run_directories_here` is false)."""
        # The launch refuses these too, where it decides the directory
        # (`launch_identity.run_identity`).
        for node in plan.nodes if run_directories_here else ():
            node.flow_class.check_run_directory(node.settings, node.run_path)
        if self.settings.outputs_to is not None:
            refuse_outputs_to_a_programmer(
                plan, plan.node(plan.requested), self.settings.outputs_to
            )

    def _refuse_unaccepted_plan(self, request: _Request, plan: Plan) -> None:
        """Refuse a request whose plan has a chain or a binding, for a launcher that takes none.

        `Dse` is such a launcher. The refusal comes once the plan shows the chain or binding."""
        if not self.accepts_bindings and (
            len(request.flow_request.elements) > 1
            or any(i.binding_origin for n in plan.nodes for i in n.inputs)
        ):
            raise FlowSettingsException(LOCAL_REQUESTS_ONLY)

    def _judged_request(
        self,
        flow: type[Flow] | str | FlowRequest,
        design: str | Path | Design | dict[str, Any] | None,
        xedaproject: str | None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings,
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any],
        design_updates: Iterable[str] | dict[str, Any] | None,
        design_allow_extra: bool,
        design_remove_fields: list[str],
        *,
        target: str | None | Unset,
        before_fetch: Callable[[Design], None] | None = None,
        host_layer: tuple[str, Mapping[str, Any]] | None = None,
        local_settings: LocalSettings | None = None,
    ) -> tuple[_Request, Plan]:
        """Load and resolve a request.

        The design loads without running its generator and without reading its dependencies: it
        lists the sources that the generator writes as the design declares them, typed, whether
        or not they exist yet. Planning does the rest in this order. First come the checks that
        read nothing that a dependency supplies (the request, the settings of the flow, the
        design file). Then the Git dependencies are fetched into the run root and merged in
        (`dependencies.merged_design`), the one case in which planning writes. Then come the
        checks that read the merged design (a source in a language that a flow cannot read, a
        missing testbench top, a run directory that a flow refuses), the identities and the
        resolved plan. All of it comes before the generator runs."""
        if design_updates is not None and not isinstance(design_updates, dict):
            design_updates = list(design_updates)
        request = self._request(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            design_updates,
            design_allow_extra,
            design_remove_fields,
            target=target,
            host_layer=host_layer,
            local_settings=local_settings,
        )
        if request.design.dependencies:
            check_settings_without_design(
                request.flow_request,
                request.settings,
                request.sections,
                request.design,
                design_root=request.design.root,
                runner_cwd=self._start,
            )
        if before_fetch is not None:
            before_fetch(request.design)
        request = self._with_dependencies(request)
        return request, self._resolve_request(request)

    def _refuse_before_merging(
        self,
        flow_class: type[Flow],
        design: Design,
        flow_settings: Mapping[str, Any] | Flow.Settings | None,
        sections: Mapping[str, Any] | None,
        flow_request: FlowRequest | None = None,
    ) -> None:
        """Refuse what the settings alone refuse, before a dependency of `design` is read.

        It is for a caller that hands the launcher a design and the settings on top of the
        `flows` of the design (`resolve`, `launch_flow`): a request composes its settings itself
        and checks them in `_judged_request`."""
        if not design.dependencies:
            return
        own, _ = split_bindings(thaw(design.flows), location=f"the design {design.name}")
        given, _ = split_bindings(sections or {}, location=SUPPLIED_SECTIONS_ORIGIN)
        requested: Mapping[str, Any] | Flow.Settings = (
            flow_settings
            if isinstance(flow_settings, Flow.Settings)
            else compose_flow_settings(flow_class, [own, given], flow_settings or {})
        )
        check_settings_without_design(
            flow_request or FlowRequest((ChainElement(flow_class),)),
            requested,
            merge_flow_sections(own, given, flow_class_for=_get_flow_class_if_known),
            design,
            design_root=design.root,
            runner_cwd=self._start,
        )

    def _merged(self, design: Design) -> Design:
        """Return `design` with its dependencies merged in (`dependencies.merged_design`).

        A design without a dependency is returned as it is. The run root is made and marked when
        the first Git dependency, at any depth, needs it, so planning any other design writes
        nothing."""
        if not design.dependencies:
            return design
        return dependencies.merged_design(design, run_root=lambda: self.run_root)

    def _with_dependencies(self, request: _Request) -> _Request:
        """Return `request` with the dependencies of its design merged in. The design of the
        plan and the launch is the merged one, and `declared` stays as the caller gave it."""
        merged = self._merged(request.design)
        return request if merged is request.design else replace(request, design=merged)

    @staticmethod
    def _private_layer(
        table: Mapping[str, Any], label: str, file: Path | None, design: Design
    ) -> tuple[str, dict[str, Any], BindingLayer]:
        """Return the label, the sections and the bindings of the `flows` table of a private file.

        A relative path of the file resolves against the directory of the file, once. A table
        that no file gave resolves against the design root, like the design's own. A private
        file is one file for many runs, so it may hold a section for any flow, but a name that is
        no flow is an error that names the file."""
        for name in table:
            check_flow_name(name)
            if _get_flow_class_if_known(name) is None:
                close = _flow_name_suggestions(name)
                raise PublicFlowNotFoundError(
                    f"Flow '{name}' in {label} was not found."
                    + (f" Did you mean: {', '.join(close)}?" if close else "")
                    + " Run `xeda list-flows` to see all available flows.",
                    code="flow_not_found",
                    name=name,
                    suggestions=close,
                )
        sections, bindings = split_bindings(thaw(table), location=label)
        if file is not None and sections and file.parent.resolve() != design.root:
            sections = anchored_sections(
                sections,
                file.parent.resolve(),
                lambda name: getattr(_get_flow_class_if_known(name), "Settings", None),
            )
        return label, sections, bindings

    def _request(
        self,
        flow: type[Flow] | str | FlowRequest,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any] = [],
        design_updates: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        design_remove_fields: list[str] = [],
        *,
        target: str | None | Unset = UNSET,
        host_layer: tuple[str, Mapping[str, Any]] | None = None,
        local_settings: LocalSettings | None = None,
    ) -> _Request:
        """Load the design and the layers of settings of a request, and return the request.

        `host_layer` is the label and the `flows` table of a host profile: a layer of the
        settings of every flow, above the user file (`prepare_request`). `local_settings` gives
        the layer of the user file, below the project, and that of the project-local file, above
        the design."""
        # Nothing is loaded yet: a request that fails before its design loads reports none.
        self.target = None
        self.design_name = None
        # The request is read from the flow alone, before anything is loaded, as the command line
        # parses it first too.
        if isinstance(flow, FlowRequest):
            flow_request = flow
            flow_class = flow.requested
        elif isinstance(flow, str):
            # One flow name, or a chain `a+b.out+c`: the same parser as the command line's.
            flow_request = parse_request(flow)
            flow_class = flow_request.requested
        else:
            flow_class = flow
            flow_request = FlowRequest((ChainElement(flow_class),))
        if not design_updates:
            design_updates = {}
        if not isinstance(design_updates, dict):
            design_updates = list(design_updates)
            design_updates = settings_to_dict(design_updates)
        # The project is the file that the caller names, read to select a design from it. The
        # launcher looks for no file: the command line discovers the project file. A design that
        # is not selected from the project (a file, a table, a built design) never gets the
        # project layer.
        project_path = Path(xedaproject) if xedaproject else None
        given_file = (
            Path(design)
            if isinstance(design, (str, Path))
            and (isinstance(design, Path) or names_a_design_file(design))
            else None
        )
        if given_file is not None or isinstance(design, dict):
            if given_file is not None:
                # An invalid design file raises, as a design from a project does: the caller
                # reports it (`xeda run --json` names the error instead of "did not complete").
                design = Design._load_file(
                    given_file,
                    updates=(design_updates,) if design_updates else (),
                    allow_extra=design_allow_extra,
                    remove_extra=design_remove_fields,
                    target=None if target is UNSET else target,
                )
            else:
                # The keyword arguments of `Design(...)`, `root` among them. A dictionary
                # `target` selects the design's target.
                keywords = dict(cast(dict, design))
                if "target" in keywords:
                    raise DesignValidationError(
                        [("target", NO_SINGULAR_TARGET, "", "value_error")], data=keywords
                    )
                design = Design(**keywords, target=None if target is UNSET else target)
                if design_updates:
                    design = design.with_updates(design_updates)
        elif isinstance(design, Design):
            if target is not UNSET and target is not None:
                raise ValueError(
                    f"target {target!r} was asked for, but the design is already built: "
                    "select the target where the design is loaded (`Design.load(..., "
                    "target=...)`)"
                )
        else:
            if project_path is None:
                given = (
                    f"'{design}' is not a design file, and"
                    if design
                    else "No design was given, and"
                )
                raise ProjectError(
                    f"{given} there is no project file to take a design from. Give one with "
                    f"--xedaproject, or keep one ({', '.join(PROJECT_FILE_NAMES)}) in the "
                    f"directory where you start Xeda ({self._start}). Or give a design file "
                    "(.yaml, .yml, .toml or .json).",
                    code="project_file_not_found",
                )
            design = Project.load(project_path).design(
                str(design) if design else UNSET, target=target
            )
            if design_updates:
                design = design.with_updates(design_updates)
        # Recover supplied contributions without treating model-created nested defaults as
        # explicit producer settings. Actual nested edits still count.
        from .resolver import _explicit

        settings_instance = isinstance(flow_settings, Flow.Settings)
        self.target = design.target if isinstance(design, Design) else None
        self.design_name = design.name if isinstance(design, Design) else None
        if isinstance(flow_settings, Flow.Settings):
            explicit_flow_settings = _explicit(flow_settings)
            flow_settings = flow_settings.model_dump()
        else:
            assert isinstance(
                flow_settings, (list, tuple, Mapping)
            ), "flow_settings should be a list, tuple or dict"
            flow_settings = settings_to_dict(flow_settings)
            explicit_flow_settings = flow_settings
        if not isinstance(flow_overrides, dict):
            flow_overrides = settings_to_dict(flow_overrides)
        assert isinstance(
            flow_settings, dict
        ), f"flow_settings should be a dict at this stage, but was {type(flow_settings)}"
        assert isinstance(
            flow_overrides, dict
        ), f"flow_overrides should be a dict at this stage, but was {type(flow_overrides)}"
        if not design or not flow_class:
            log.critical("Failed to parse design and/or flow")
            raise ValueError(f"design={design} flow_class={flow_class}")
        assert isinstance(
            design, Design
        ), f"BUG: design should be of type Design but was {type(design)}"

        # Merge the own settings of an embedded
        # or project-selected design too. One origin per file, each normalized on its own:
        # origin decides first, nesting only within one origin (`compose_flow_settings`).
        # The project layer is the one the design carries: only a design that a project selected
        # has it.
        layer = design._project_layer
        project_label, flows_settings = (layer[0], thaw(layer[1])) if layer else ("", {})
        if given_file is not None:
            design_label = str(given_file.absolute())
        elif layer is not None and design._source_file is not None:
            design_label = str(design._source_file)
        elif layer is not None:
            design_label = f"{project_label}: design {design.name}"
        else:
            design_label = f"the design {design.name}"
        host_label, host_table = host_layer if host_layer is not None else ("", {})
        host_sections, host_bindings = split_bindings(host_table, location=host_label)
        project_sections, project_bindings = split_bindings(flows_settings, location=project_label)
        if layer is not None and Path(project_label).parent.resolve() != design.root:
            # The project file is another file than the design file, in another directory: its
            # relative paths are relative to it. (Where the two directories are one, a flow
            # resolves them against the design root, as it does for every other layer.)
            project_sections = anchored_sections(
                project_sections,
                Path(project_label).parent.resolve(),
                lambda name: getattr(_get_flow_class_if_known(name), "Settings", None),
            )
        design_sections, design_bindings = split_bindings(thaw(design.flows), location=design_label)
        user_label, user_sections, user_bindings = self._private_layer(
            local_settings.user if local_settings else {},
            user_config_origin(local_settings.user_file if local_settings else None),
            local_settings.user_file if local_settings else None,
            design,
        )
        local_label, local_sections, local_bindings = self._private_layer(
            local_settings.local if local_settings else {},
            project_local_origin(local_settings.local_file if local_settings else None),
            local_settings.local_file if local_settings else None,
            design,
        )
        # The `-s flows.<node>.key` items of the command line are the third origin. They may only
        # name a flow of this run, and `-s key` is a leaf of the requested flow.
        cli_sections, cli_own = split_flow_sections(
            explicit_flow_settings, flow_class.name, flow_class_for=_get_flow_class_if_known
        )
        cli_sections = merge_flow_sections(
            cli_sections, {flow_class.name: cli_own}, flow_class_for=_get_flow_class_if_known
        )
        cli_sections, cli_bindings = split_bindings(
            cli_sections, location=COMMAND_LINE_ORIGIN, kind="cli"
        )
        api_sections, api_bindings = split_bindings(
            {flow_class.name: flow_overrides}, location=API_ORIGIN, kind="api"
        )
        binding_layers = (
            *((user_bindings,) if user_sections else ()),
            *((host_bindings,) if host_layer is not None else ()),
            project_bindings,
            design_bindings,
            *((local_bindings,) if local_sections else ()),
            cli_bindings,
            api_bindings,
        )
        effective_bindings(binding_layers, default_nodes([flow_class]), request=flow_request)
        # Ordinary calls keep the early CLI addressing check. A bound request is checked by
        # the resolver against its graph: an alternate producer can be outside the default one.
        if (
            not any(layer.entries or layer.invalid_inputs for layer in binding_layers)
            and len(flow_request.elements) == 1
        ):
            command_line_sections(
                explicit_flow_settings,
                flow_class,
                design,
                flow_class_for=_get_flow_class_if_known,
            )
        file_origins = [
            *([user_sections] if user_sections else []),
            *([host_sections] if host_layer is not None else []),
            *([project_sections] if layer else []),
            design_sections,
            *([local_sections] if local_sections else []),
        ]
        origins = [*file_origins, cli_sections, api_sections]
        all_sections = merge_flow_sections(*origins, flow_class_for=_get_flow_class_if_known)
        # `-s` wins over the design and project files, as documented; see `settings_layers`.
        final_flow_settings = compose_flow_settings(flow_class, origins)
        if settings_instance:
            # Preserve the final-value model API while the captured CLI origin records only
            # supplied contributions for nested-default diagnostics and shared agreement.
            final_flow_settings = compose_flow_settings(
                flow_class,
                file_origins,
                flow_settings,
                api_sections[flow_class.name],
            )
        if self.settings.debug:
            log.info(
                "design: %s" % PrettyPrinter().pformat(_shown_dependencies(design.model_dump()))
            )
        # The files that this launch was given: never the destination of an output.
        # A design that a project selected has two more: its own file and the project file. The
        # private settings files that were loaded come last.
        launch_files: list[Path | None] = [given_file, project_path, design._source_file]
        if layer is not None:
            launch_files.append(Path(project_label))
        # The private files that were loaded are files of the launch too.
        if local_settings is not None:
            launch_files += [local_settings.user_file, local_settings.local_file]
        self._launch_inputs = list(
            dict.fromkeys(
                path.resolve() for path in launch_files if path is not None and path.is_file()
            )
        )
        return _Request(
            flow_class,
            design,
            deepcopy(final_flow_settings),
            deepcopy(all_sections),
            tuple(
                (label, deepcopy(section))
                for label, section in (
                    *(((user_label, user_sections),) if user_sections else ()),
                    *(((host_label, host_sections),) if host_layer is not None else ()),
                    *(((project_label, project_sections),) if layer else ()),
                    (design_label, design_sections),
                    *(((local_label, local_sections),) if local_sections else ()),
                )
            ),
            deepcopy(cli_sections),
            deepcopy(api_sections),
            flow_request,
            binding_layers,
            design,
        )


class FlowRunner(FlowLauncher):
    """An alias of `FlowLauncher`."""


class DefaultRunner(FlowRunner):
    """A launcher that runs a flow with its producers and then reports selected results."""


def add_file_logger(
    logdir: Union[Path, str], timestamp: Union[str, datetime, None] = None
) -> logging.Handler:
    if timestamp is None:
        timestamp = datetime.now()
    if not isinstance(timestamp, str):
        timestamp = timestamp.strftime("%Y-%m-%d-%H%M%S%f")[:-3]
    if not isinstance(logdir, Path):
        logdir = Path(logdir)
    logdir.mkdir(exist_ok=True, parents=True)
    logfile = logdir / f"xeda_{timestamp}.log"
    log.info("Logging to %s", logfile)
    fileHandler = logging.FileHandler(logfile)
    logFormatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
    )
    fileHandler.setFormatter(logFormatter)
    log.root.addHandler(fileHandler)
    return fileHandler


def remove_file_logger(handler: Optional[logging.Handler]) -> None:
    """Remove a handler that `add_file_logger` made from the root logger, and close its file.

    A search that was given one for its run must not leave it to every later log record of the
    process."""
    if handler is not None:
        log.root.removeHandler(handler)
        handler.close()
