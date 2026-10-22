"""The values that a launch records and a caller reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["ErrorInfo"]

# The codes of the errors that a node records (`flow_runner.error_codes` re-exports them). They
# are defined here so that the flow classes can write an error without importing the launcher.
NONZERO_EXIT = "nonzero_exit"
TIMEOUT = "timeout"
EXECUTABLE_NOT_FOUND = "executable_not_found"
REPORTED_FAILURE = "reported_failure"
MISSING_REPORT = "missing_report"
MISSING_OUTPUT = "missing_output"
INVALID_METRIC = "invalid_metric"
REJECTED = "rejected"
PRODUCER_FAILED = "producer_failed"
CANCELED = "canceled"
STORAGE_ERROR = "storage_error"
INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ErrorInfo:
    """Why a node failed, was rejected or was canceled, or why a launch failed.

    `code` is a stable lower snake case text. Its set is open: a reader keeps a code it does not
    know as text. `node` is the node that the error belongs to, `None` for an error of the whole
    launch. `exit_status` is the exit status of the tool, for `nonzero_exit`. `log` is the key of
    the artifact to read for the cause, if there is one."""

    code: str
    message: str
    node: str | None
    exit_status: int | None
    log: str | None

    def as_json_value(self) -> dict[str, Any]:
        """Return the JSON form: the document `results.json` holds under `error`."""
        return {
            "code": self.code,
            "message": self.message,
            "node": self.node,
            "exit_status": self.exit_status,
            "log": self.log,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ErrorInfo:
        """Return the error that `as_json_value` wrote. A value of another shape is a `ValueError`."""
        if not isinstance(value, Mapping):
            raise ValueError(f"an error is a mapping, not {type(value).__name__}")
        for key in ("code", "message"):
            if not isinstance(value.get(key), str):
                raise ValueError(f"the `{key}` of an error is text")
        for key in ("node", "log"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"the `{key}` of an error is text or null")
        status = value.get("exit_status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
            raise ValueError("the `exit_status` of an error is an integer or null")
        return cls(
            code=value["code"],
            message=value["message"],
            node=value.get("node"),
            exit_status=status,
            log=value.get("log"),
        )
