"""The run stage of a launch: what happens around `run()`, `parse_reports()` and `check_results()`.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes what each function needs.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..dataclass import WORKING_ROLE
from ..flow import Flow
from ..flow.flow import WrittenLeaf, map_written_leaves
from ..proc_utils import ProcessTimeout
from ..records import MISSING_OUTPUT, REPORTED_FAILURE, ErrorInfo
from ..run_dir import RunDirectory, RunDirectoryError
from ..tool import NonZeroExitCode
from ..utils import WorkingDirectory
from .error_codes import error_info, missing_report_info
from .outputs import record_outputs

log = logging.getLogger(__name__)


def refuse_inputs_inside(run_path: Path, flow_name: str, files: Iterable[Path]) -> None:
    """Refuse a file of the design or a file that a setting reads in the run directory of the flow.

    Xeda empties and rewrites the run directory of a flow, so no such file may lie there. (In
    the run directory of another run, the file is the output of that run, read by content.)"""
    run_dir = Path(os.path.realpath(run_path))
    inside = sorted({f for f in files if Path(os.path.realpath(f)).is_relative_to(run_dir)})
    if inside:
        raise RunDirectoryError(
            f"{inside[0]} lies in {run_path}, {flow_name}'s own run directory, which Xeda "
            "empties and rewrites: keep your files in your design, outside the run root"
        )


def free_working_locations(flow: Flow) -> None:
    """Free the working locations of `flow` before the run.

    A working location is a setting with the `WORKING` role, a name inside the run directory.
    It is where the tools work, and never through a symbolic link out of the run directory. A
    tool may leave a link anywhere at the name of its working location. Xeda removes that link
    as itself, never what it points to, and the tool makes the location anew. A location
    reached through a link that leads out is refused, naming the link (`RunDirectory.inside`),
    before anything runs there."""
    run_directory = flow.run_directory

    def free(written: WrittenLeaf) -> Any:
        leaf = written.value
        if written.role != WORKING_ROLE or not isinstance(leaf, (str, os.PathLike)):
            return leaf
        if os.path.normpath(os.fspath(leaf)) in ("", "."):
            return leaf  # the run directory itself
        located = run_directory.inside(leaf)
        if located.is_symlink() and not RunDirectory.lies_under(located, run_directory.path):
            log.info(
                "Removing %s, a link left where `%s` works: it leads out of the run directory",
                located,
                written.key,
            )
            run_directory.remove(located)
        return leaf

    map_written_leaves(flow.settings, free)


def record_identity(flow: Flow, run_path: Path) -> None:
    """Record the identity of the run in its results.

    Every results document holds it, whether the run succeeded or not."""
    flow.results["design"] = flow.design.name
    flow.results["design_hash"] = flow.design_hash
    flow.results["flow"] = flow.name
    flow.results["flow_hash"] = flow.flow_hash
    flow.results["settings_hash"] = flow.settings_hash
    flow.results["run_path"] = run_path.absolute()
    flow.results.timestamp = flow.timestamp


def execute(flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
    """Run the flow and collect its results, artifacts included.

    The sequence is `run()`, `parse_reports()` and `check_results()`."""
    record_identity(flow, run_path)

    tool_completed = False

    with WorkingDirectory(run_path):
        if flow.settings.reports_dir:
            flow.settings.reports_dir.mkdir(exist_ok=True, parents=True)
        try:
            flow.run()
            tool_completed = True
        except NonZeroExitCode as e:
            if isinstance(e, ProcessTimeout):
                log.error("%s", e)
            else:
                log.error(
                    "Execution of '%s' returned %d",
                    (
                        " ".join(e.command_args)
                        if isinstance(e.command_args, (list, tuple))
                        else e.command_args
                    ),
                    e.exit_code,
                )
            flow.results["error"] = error_info(e, node=flow.name).as_json_value()
        if flow.init_time is not None:
            flow.results.runtime = time.monotonic() - flow.init_time
        reports_ok: bool | None = None
        checks_ok = False
        try:
            reports_ok = flow.parse_reports()
            checks_ok = flow.check_results()
        except Exception as e:  # pylint: disable=broad-except
            log.critical("parse_reports or check_results threw an exception: %s", e)
            if tool_completed and reports_ok is not False:  # if so far so good this is a bug!
                raise e
        flow.add_canonical_result_aliases()
        # `reject()` is the verdict: a run that called it never passes, whatever the checks
        # returned.
        rejected = flow.rejection is not None
        problems: list[str] = []
        if tool_completed and reports_ok and (checks_ok or rejected):
            # Consumers use these integrity records for a producer that just ran and for a
            # reused one alike. A rejected run has its outputs checked, and hands none over.
            problems = record_outputs(flow, record=checks_ok and not rejected)
            for problem in problems:
                log.error("%s", problem)
        if tool_completed and reports_ok and checks_ok and not rejected and not problems:
            flow.outcome = "passed"
        elif tool_completed and reports_ok and rejected and not problems:
            flow.outcome = "rejected"
        else:
            flow.outcome = "failed"
            if problems and checks_ok and not rejected:
                flow.results["error"] = ErrorInfo(
                    code=MISSING_OUTPUT,
                    message="; ".join(problems),
                    node=flow.name,
                    exit_status=None,
                    log=None,
                ).as_json_value()
        success = flow.outcome == "passed"
        if not success and not input_settings.is_quiet:
            log.debug("Failure was reported in the parsed results.")
        if not success and not flow.results.get("error"):
            # No exception and no exit status: the reports or checks said so. A failure
            # document always names its error, and a consumer quotes it. When the reports
            # failed and a report the flow asked for was not there, that is the cause.
            if tool_completed and reports_ok is False and flow.reports_missing:
                flow.results["error"] = missing_report_info(
                    flow.name, flow.reports_missing, run_path
                ).as_json_value()
            else:
                flow.results["error"] = ErrorInfo(
                    code=REPORTED_FAILURE,
                    message=(
                        f"`{flow.name}` reported failure: its reports or checks did not pass "
                        "(see the log and its run directory)"
                    ),
                    node=flow.name,
                    exit_status=None,
                    log=None,
                ).as_json_value()
        flow.results.success = success
    for k, v in flow.artifacts.items():
        if not flow.results.artifacts.get(k):
            flow.results.artifacts[k] = v
