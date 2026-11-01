"""The stable codes of the errors of a launch, and the public errors they belong to.

A node that fails records an `ErrorInfo` in its `results.json`. A call that refuses a request
raises an `xeda.errors.XedaError`. Both carry a `code`: a lower snake case text that a script
can compare. The codes never follow class names. `error_info` gives the record of a node, and
`public_error` gives the public error for an exception of the launcher.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from .. import errors
from ..dataclass import input_names, validation_errors
from ..deliver import DeliveryError
from ..flow import (
    FlowDependencyFailure,
    FlowSettingsError,
    FlowSettingsException,
)
from ..flow import Flow
from ..flow.flow import _model_at, registered_flows
from ..flow.metrics import log_stem
from ..generation import GenerationError, MissingGenerated
from ..proc_utils import ProcessTimeout
from ..run_dir import RunDirectoryError
from ..records import (
    CANCELED,
    EXECUTABLE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_METRIC,
    MISSING_GENERATED,
    MISSING_OUTPUT,
    MISSING_REPORT,
    NONZERO_EXIT,
    PRODUCER_FAILED,
    REJECTED,
    REPORTED_FAILURE,
    STORAGE_ERROR,
    TIMEOUT,
    ErrorInfo,
)
from ..utils import (
    Canceled,
    ConflictingKeys,
    EscapedName,
    ExecutableNotFound,
    NonZeroExitCode,
    XedaException,
)
from .chains import ChainRefusal

#: The code of a launch-level error: the outputs of a launch were not delivered.
DELIVERY_FAILED = "delivery_failed"

#: The code of a launch-level error: the generator of the design failed, before any node ran.
GENERATOR_FAILED = "generator_failed"

#: The code of a launch-level error: a clean-up after the run failed, and every root had passed.
CLEANUP_FAILED = "cleanup_failed"

#: The code of the error of an exploration (`xeda dse`) that found no run that passed.
NO_SUCCESSFUL_RUN = "no_successful_run"

#: The code of a node error: a worker process of a sweep could not import the module of a flow.
FLOW_NOT_IMPORTABLE = "flow_not_importable"

__all__ = [
    "CANCELED",
    "CLEANUP_FAILED",
    "DELIVERY_FAILED",
    "EXECUTABLE_NOT_FOUND",
    "FLOW_NOT_IMPORTABLE",
    "GENERATOR_FAILED",
    "INTERNAL_ERROR",
    "INVALID_METRIC",
    "MISSING_GENERATED",
    "MISSING_OUTPUT",
    "MISSING_REPORT",
    "NONZERO_EXIT",
    "NO_SUCCESSFUL_RUN",
    "PRODUCER_FAILED",
    "REJECTED",
    "REPORTED_FAILURE",
    "STORAGE_ERROR",
    "TIMEOUT",
    "error_info",
    "plain_message",
    "public_error",
    "request_error_info",
    "settings_codes",
]

#: The code of a finding of `FlowSettingsError.errors`, by its type: the type of a validation
#: error of pydantic, or the type that the code which refuses the settings gives. Any other type
#: is an invalid value.
_SETTINGS_CODES = {
    "extra_forbidden": "unknown_setting",
    "missing": "required_setting",
    "required_setting": "required_setting",
    "quantity": "invalid_quantity",
    "boolean_text": "boolean_text",
    "list_text": "list_text",
    "deliverable_switch": "deliverable_switch",
    "shared_setting_conflict": "shared_setting_conflict",
    "conflicting_spellings": "conflicting_keys",
    "conflicting_bindings": "conflicting_keys",
    "conflicting_keys": "conflicting_keys",
    "unscoped_setting": "unscoped_setting",
    "unknown_node": "unknown_node",
    "removed_setting": "removed_setting",
    "removed_key": "removed_setting",  # a launcher setting: the same code as a flow setting
    "removed_alias": "removed_alias",
    "settings_group": "unsupported_setting",
    "unsupported_setting": "unsupported_setting",
}


ALL_CODES: Final[Mapping[str, frozenset[str]]] = {
    "SettingsError": frozenset(
        [
            "unknown_setting",
            "required_setting",
            "invalid_quantity",
            "boolean_text",
            "list_text",
            "deliverable_switch",
            "invalid_value",
            "shared_setting_conflict",
            "conflicting_keys",
            "removed_setting",
            "removed_alias",
            "unsupported_setting",
            "unscoped_setting",
            "unknown_node",
        ]
    ),
    "DesignError": frozenset(
        [
            "design_file_not_found",
            "invalid_design",
            "unknown_target",
            "target_required",
            "removed_alias",
            "removed_key",
            "fetch_failed",
            "dependency_cycle",
        ]
    ),
    "ProjectError": frozenset(
        [
            "project_file_not_found",
            "invalid_project",
            "design_name_required",
            "unknown_design",
            "several_project_files",
            "use_design_name",
            "invalid_local_settings",
            "removed_alias",
        ]
    ),
    "FlowNotFoundError": frozenset(["flow_not_found"]),
    "ChainError": frozenset(
        [
            "invalid_chain",
            "repeated_flow",
            "action_not_last",
            "no_edge",
            "unknown_output",
            "ambiguous_output",
            "cardinality_mismatch",
            "type_mismatch",
            "several_roots_unsupported",
        ]
    ),
    "RunRootError": frozenset(["invalid_run_root"]),
    "SpecError": frozenset(
        [
            "equal_specs",
            "colliding_run_dirs",
            "shared_destination",
            "delivery_refused",
            "run_root_mismatch",
            "plan_options",
            "missing_dependency",
            "unsupported_with_remote",
        ]
    ),
    "StalePlanError": frozenset(["stale_plan"]),
    "RecordError": frozenset(
        ["missing_record", "corrupt_record", "unsupported_schema", "not_written", "several_files"]
    ),
    "RunReplacedError": frozenset(["run_replaced"]),
    "RemoteError": frozenset(["remote_unreachable", "remote_setup_failed"]),
    "RunFailedError": frozenset(["run_failed"]),
    "LeaseHeldError": frozenset(["lease_held"]),
    "XedaError": frozenset(["internal_error"]),
    "node": frozenset(
        [
            "nonzero_exit",
            "timeout",
            "executable_not_found",
            "reported_failure",
            "missing_report",
            "missing_output",
            "invalid_metric",
            "rejected",
            "producer_failed",
            "canceled",
            "storage_error",
            "internal_error",
            "missing_generated",
            "flow_not_importable",
        ]
    ),
    "cli": frozenset(["usage_error"]),
    "launch": frozenset(
        [
            "generator_failed",
            "delivery_failed",
            "remote_failed",
            "remote_fetch_failed",
            "remote_detached",
            "canceled",
            "no_successful_run",
            "cleanup_failed",
        ]
    ),
}


#: The text of pydantic for a key that a model does not have.
_EXTRA_INPUTS = "Extra inputs are not permitted"

#: The codes of the findings that refuse an address, and hold the addresses to use in their context.
_ADDRESS_CODES = frozenset({"unscoped_setting", "unknown_node"})


def failure_log(exc: BaseException, flow: Flow | None) -> str | None:
    """Return the artifact key of the log to read for the failure `exc` of `flow`, or None.

    A program that exited with a nonzero status (`NonZeroExitCode`) names its own log, if the run
    wrote one: `log` for the main tool of the flow and `<name>_log` for another program. Any
    other failure names `log`, if the flow wrote the log of its main tool."""
    if flow is None:
        return None
    if isinstance(exc, NonZeroExitCode):
        program = exc.program or (exc.command_args or "").split(" ")[0]
        key = flow.tool_log_keys.get(log_stem(program)) if program else None
        if key is not None:
            return key
    return "log" if "log" in flow.tool_log_keys.values() else None


def error_info(
    exc: BaseException, *, node: str | None, flow: Flow | None = None, log: str | None = None
) -> ErrorInfo:
    """Return the record of the error `exc` that ended `node` (`None`: the whole launch).

    The code follows the kind of failure, never the name of the class. `flow` is the flow that
    raised it, if there is one: the error then names the log to read (`failure_log`). `log` is the
    artifact key of that log, when the caller knows it better."""
    exit_status: int | None = None
    if isinstance(exc, Canceled):
        code = CANCELED
    elif isinstance(exc, ProcessTimeout):
        code = TIMEOUT
    elif isinstance(exc, NonZeroExitCode):
        code = NONZERO_EXIT
        exit_status = exc.exit_code
    elif isinstance(exc, ExecutableNotFound):
        code = EXECUTABLE_NOT_FOUND
    elif isinstance(exc, FlowDependencyFailure):
        code = PRODUCER_FAILED
    elif isinstance(exc, DeliveryError):
        # A hand-over that found an output of a producer missing or changed is a problem with
        # the declared outputs. Any other delivery refusal is the flow's own failure.
        code = MISSING_OUTPUT if exc.output_problem else REPORTED_FAILURE
    elif isinstance(exc, MissingGenerated):
        code = MISSING_GENERATED
    elif isinstance(exc, GenerationError):
        code = GENERATOR_FAILED
    elif isinstance(exc, (OSError, RunDirectoryError)):
        code = STORAGE_ERROR
    elif isinstance(exc, XedaException):
        # The flow refused to run, a check of its tool failed, or the design, the project or the
        # settings were refused: an error that Xeda raises on purpose is the failure of the node.
        # Only an exception that Xeda does not know is a bug of Xeda.
        code = REPORTED_FAILURE
    else:
        code = INTERNAL_ERROR
    return ErrorInfo(
        code=code,
        message=str(exc),
        node=node,
        exit_status=exit_status,
        log=log if log is not None else failure_log(exc, flow),
    )


def _finding_code(kind: str | None, refuses_support: bool) -> str:
    """Return the code of a finding of type `kind`. A refusal of unsupported settings calls a
    finding without a kind of its own an unsupported setting."""
    if kind in _SETTINGS_CODES:
        return _SETTINGS_CODES[kind]
    unspecific = kind in (None, "value_error")
    return "unsupported_setting" if refuses_support and unspecific else "invalid_value"


def settings_codes(exc: FlowSettingsError) -> list[str]:
    """Return the code of each finding of `exc`, in the order of `exc.errors`."""
    return [_finding_code(kind, exc.refuses_support) for *_, kind in exc.errors]


def _owner_name(model: Any) -> str | None:
    """Return the name of the flow whose `Settings` class `model` is, or None."""
    qualified = getattr(model, "__qualname__", None)
    if not isinstance(qualified, str) or "." not in qualified:
        return None
    owner = qualified.rpartition(".")[0]
    for _, flow_class in registered_flows.values():
        if flow_class.__qualname__ == owner and flow_class.__module__ == model.__module__:
            return flow_class.name
    return None


def _close_names(model: Any, address: str) -> tuple[str, ...]:
    """Return the names, at the level of the unknown setting at `address`, that are close to it."""
    parts = address.split(".")
    owner = _model_at(model, parts[:-1]) if isinstance(model, type) else None
    if owner is None:
        return ()
    return tuple(difflib.get_close_matches(parts[-1], list(input_names(owner)), n=3, cutoff=0.75))


def _settings_error(exc: FlowSettingsError | ValidationError) -> errors.SettingsError:
    """Return the `SettingsError` for the findings of `exc`: the first finding, which holds all."""
    refuses_support = False
    if isinstance(exc, ValidationError):
        findings, model = validation_errors(exc.errors()), None
    else:
        findings, model = list(exc.errors), exc.model
        refuses_support = exc.refuses_support
    node = _owner_name(model)
    if findings:
        codes = [_finding_code(kind, refuses_support) for *_, kind in findings]
    else:
        codes = ["invalid_value"]
        findings = [(None, str(exc), None, None)]
    built: list[errors.SettingsError] = []
    for (location, problem, context, _), code in zip(findings, codes):
        setting = ".".join(location.split(" -> ")) if location else None
        problem = problem or ""
        suggestions = _close_names(model, setting) if code == "unknown_setting" and setting else ()
        if code in _ADDRESS_CODES and context:
            # The context of these findings holds the addresses that the message suggests.
            suggestions = tuple(context.split(","))
        # A setting of a flow is named as the user writes it: `-s <flow>.<key>`.
        named = f"{node}.{setting}" if node and setting and code not in _ADDRESS_CODES else setting
        if node and code == "unknown_setting":
            problem = problem.replace(_EXTRA_INPUTS, "the flow has no such setting", 1)
        built.append(
            errors.SettingsError(
                f"{named}: {problem}" if named else problem,
                code=code,
                node=None if code in _ADDRESS_CODES else node,
                setting=setting,
                problem=problem,
                suggestions=suggestions,
            )
        )
    built.sort(key=lambda finding: finding.setting or "")
    first = built[0]
    first.errors = tuple([first, *built[1:]])
    return first


def _chain_refusal(exc: ChainRefusal) -> errors.ChainError:
    return errors.ChainError(str(exc), code=exc.code, chain=exc.chain or "", problem=str(exc))


def _remote_error(exc: Exception) -> errors.RemoteError | None:
    """Return the public error for a failure of the host or its setup, or None.

    A canceled, detached, interrupted or incomplete remote launch is the outcome of a launch,
    not a failure of the host, so it has no `RemoteError`."""
    from ..remote.errors import (
        RemoteCanceled,
        RemoteConnectionError,
        RemoteDetached,
        RemoteError,
        RemoteFetchError,
        RemoteInterrupted,
    )

    if not isinstance(exc, RemoteError):
        return None
    if isinstance(exc, (RemoteCanceled, RemoteDetached, RemoteInterrupted, RemoteFetchError)):
        return None
    code = "remote_unreachable" if isinstance(exc, RemoteConnectionError) else "remote_setup_failed"
    return errors.RemoteError(str(exc), code=code, host=str(getattr(exc, "host", "") or ""))


def public_error(exc: BaseException) -> errors.XedaError:
    """Return the public error for the exception `exc` of the launcher.

    An exception that is a public error already is returned itself. The mapping reads the kind
    of the exception and the code it carries. An exception that no rule knows is an internal
    error: a bug of Xeda, not of the request."""
    if isinstance(exc, errors.XedaError):
        return exc
    if isinstance(exc, (FlowSettingsError, ValidationError)):
        return _settings_error(exc)
    if isinstance(exc, ChainRefusal):
        return _chain_refusal(exc)
    if isinstance(exc, DeliveryError) and exc.before_run:
        return errors.SpecError(str(exc), code="delivery_refused")
    if isinstance(exc, RunDirectoryError):
        # Raised for a request: a run root that holds a link where Xeda would write.
        if exc.path is not None:
            return errors.RunRootError(str(exc), code="invalid_run_root", path=exc.path)
        return errors.SpecError(str(exc), code="invalid_run_root")
    if isinstance(exc, EscapedName):
        return errors.SettingsError(str(exc), code="invalid_value", setting=exc.name)
    if isinstance(exc, FlowSettingsException):
        return errors.SettingsError(str(exc), code=exc.code, node=exc.node)
    if isinstance(exc, ConflictingKeys):
        return errors.SettingsError(str(exc), code="conflicting_keys", setting=exc.value_key)
    if isinstance(exc, GenerationError):
        return errors.XedaError(str(exc), code=GENERATOR_FAILED)
    if isinstance(exc, Exception):
        remote = _remote_error(exc)
        if remote is not None:
            return remote
    return errors.XedaError(str(exc), code=INTERNAL_ERROR)


def plain_message(exc: BaseException) -> str:
    """Return the message of the public error for `exc`, in plain terms: without the name of a
    class in front of it. The name stays in the log lines."""
    public = public_error(exc)
    text = public.message
    for name in dict.fromkeys((type(exc).__name__, type(exc).__qualname__, type(public).__name__)):
        text = text.removeprefix(f"{name}: ")
    return text or type(exc).__name__


def request_error_info(exc: BaseException, *, code: str | None = None) -> ErrorInfo:
    """Return the error of a launch for `exc`, which left it outside any node: the code and the
    node of the public error (`code` replaces the code) and its message in plain terms."""
    public = public_error(exc)
    return ErrorInfo(
        code=code or public.code,
        message=plain_message(exc),
        node=getattr(public, "node", None),
        exit_status=None,
        log=None,
    )


def missing_report_info(node: str, reports: Sequence[Path], run_path: Path) -> ErrorInfo:
    """Return the error of a run whose reports were not there: the files it asked for in vain."""
    names = ", ".join(
        dict.fromkeys(
            str(report.relative_to(run_path)) if report.is_relative_to(run_path) else str(report)
            for report in reports
        )
    )
    return ErrorInfo(
        code=MISSING_REPORT,
        message=(
            f"`{node}` reported failure: a report that it reads was not written by this run "
            f"({names}); see the log and its run directory"
        ),
        node=node,
        exit_status=None,
        log=None,
    )
