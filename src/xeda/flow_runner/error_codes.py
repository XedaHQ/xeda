"""The stable codes of the errors of a launch, and the public errors they belong to.

A node that fails records an `ErrorInfo` in its `results.json`. A call that refuses a request
raises an `xeda.errors.XedaError`. Both carry a `code`: a lower snake case text that a script
can compare. The codes never follow class names. `error_info` gives the record of a node, and
`public_error` gives the public error for an exception of the launcher.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import errors
from ..dataclass import input_names, validation_errors
from ..deliver import DeliveryError
from ..design import DesignFileParseError, DesignValidationError
from ..flow import (
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
)
from ..flow.flow import _model_at, registered_flows
from ..proc_utils import ProcessTimeout
from ..records import (
    CANCELED,
    EXECUTABLE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_METRIC,
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
from ..utils import ConflictingKeys, ExecutableNotFound, NonZeroExitCode, ToolException
from ..xedaproject import ProjectFileError
from .chains import ChainRefusal

__all__ = [
    "CANCELED",
    "EXECUTABLE_NOT_FOUND",
    "INTERNAL_ERROR",
    "INVALID_METRIC",
    "MISSING_OUTPUT",
    "MISSING_REPORT",
    "NONZERO_EXIT",
    "PRODUCER_FAILED",
    "REJECTED",
    "REPORTED_FAILURE",
    "STORAGE_ERROR",
    "TIMEOUT",
    "error_info",
    "public_error",
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
    "removed_setting": "removed_setting",
    "settings_group": "unsupported_setting",
    "unsupported_setting": "unsupported_setting",
}


def error_info(exc: BaseException, *, node: str | None, log: str | None = None) -> ErrorInfo:
    """Return the record of the error `exc` that ended `node` (`None`: the whole launch).

    The code follows the kind of failure, never the name of the class. `log` is the artifact key
    of the log to read, if the caller knows one."""
    exit_status: int | None = None
    if isinstance(exc, ProcessTimeout):
        code = TIMEOUT
    elif isinstance(exc, NonZeroExitCode):
        code = NONZERO_EXIT
        exit_status = exc.exit_code
    elif isinstance(exc, ExecutableNotFound):
        code = EXECUTABLE_NOT_FOUND
    elif isinstance(exc, FlowDependencyFailure):
        code = PRODUCER_FAILED
    elif isinstance(exc, (FlowFatalError, ToolException)):
        # The flow refused to run, or a check of its tool failed: the flow says so itself.
        code = REPORTED_FAILURE
    elif isinstance(exc, DeliveryError):
        code = "delivery_failed"
    else:
        code = INTERNAL_ERROR
    return ErrorInfo(code=code, message=str(exc), node=node, exit_status=exit_status, log=log)


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
    for (location, problem, _, _), code in zip(findings, codes):
        setting = ".".join(location.split(" -> ")) if location else None
        problem = problem or ""
        suggestions = _close_names(model, setting) if code == "unknown_setting" and setting else ()
        built.append(
            errors.SettingsError(
                f"{setting}: {problem}" if setting else problem,
                code=code,
                node=node,
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


def _design_error(exc: DesignValidationError) -> errors.DesignError:
    location = exc.errors[0][0] if exc.errors else None
    return errors.DesignError(
        str(exc),
        code="invalid_design",
        field=".".join(location.split(" -> ")) if location else None,
        problem=exc.errors[0][1] if exc.errors and exc.errors[0][1] else None,
        path=Path(exc.file) if exc.file else None,
    )


def _remote_error(exc: Exception) -> errors.RemoteError | None:
    """Return the public error for an exception of `xeda.remote`, or None for any other."""
    from ..remote.errors import RemoteConnectionError, RemoteError

    if not isinstance(exc, RemoteError):
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
    if isinstance(exc, FlowSettingsException):
        return errors.SettingsError(str(exc), code=exc.code)
    if isinstance(exc, ConflictingKeys):
        return errors.SettingsError(str(exc), code="conflicting_keys", setting=exc.value_key)
    if isinstance(exc, DesignValidationError):
        return _design_error(exc)
    if isinstance(exc, DesignFileParseError):
        missing = not Path(exc.file).exists()
        return errors.DesignError(
            str(exc),
            code="design_file_not_found" if missing else "invalid_design",
            problem=exc.reason,
            path=Path(exc.file),
        )
    if isinstance(exc, ProjectFileError):
        return errors.ProjectError(str(exc), code=exc.code)
    if isinstance(exc, Exception):
        remote = _remote_error(exc)
        if remote is not None:
            return remote
    return errors.XedaError(str(exc), code=INTERNAL_ERROR)


def missing_report_info(node: str, reports: Sequence[Path], run_path: Path) -> ErrorInfo:
    """Return the error of a run whose reports were not there: the files it asked for in vain."""
    names = ", ".join(
        str(report.relative_to(run_path)) if report.is_relative_to(run_path) else str(report)
        for report in reports
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
