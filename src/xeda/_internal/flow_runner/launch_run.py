"""The run stage of a launch: what happens around `run()`, `parse_reports()` and `check_results()`.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes what each function needs.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ..artifacts import iter_artifact_paths
from ..dataclass import WORKING_ROLE
from ..flow import Flow
from ..flow.flow import WrittenLeaf, map_written_leaves
from ..proc_utils import ProcessTimeout
from ..records import (
    INTERNAL_ERROR,
    INVALID_METRIC,
    MISSING_OUTPUT,
    REPORTED_FAILURE,
    ErrorInfo,
    artifact_problem,
    collect_metrics,
)
from ..run_dir import RunDirectory, RunDirectoryError
from ..tool import NonZeroExitCode
from .error_codes import error_info, missing_report_info
from .launch_identity import RunDirPolicy
from .outputs import declared_output_files, record_outputs
from .scrub import run_directory_names
from .trace import remove_trace

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


def timestamp_text(moment: datetime) -> str:
    """Return `moment` as the local time text of `Flow.timestamp` (`YYYY-MM-DD-HHMMSS`)."""
    return moment.astimezone().strftime("%Y-%m-%d-%H%M%S")


def record_identity(flow: Flow, run_path: Path) -> None:
    """Record the identity of the run in `Flow.results`, which the launcher and the CLI read.

    The record of the run (`records.record_of`) takes its identity from the flow, not from here."""
    flow.results["design"] = flow.design.name
    flow.results["design_hash"] = flow.design_hash
    flow.results["flow"] = flow.name
    flow.results["flow_hash"] = flow.flow_hash
    flow.results["settings_hash"] = flow.settings_hash
    flow.results["run_path"] = run_path.absolute()
    flow.results.timestamp = flow.timestamp


def drop_bad_artifacts(flow: Flow) -> list[str]:
    """Drop the artifacts of `flow` that a record cannot hold, and return a line for each.

    A flow registers an artifact as one file under a key of the grammar (`artifact_problem`). One
    that registers a list, a table, a directory or a bad key has a bug. The flow loses that
    artifact, so that the record of its run can be written."""
    # An empty value (`None`, `""`, `[]`) is no artifact in either mapping, as in the copy below.
    held = {
        **{k: v for k, v in flow.artifacts.items() if v},
        **{k: v for k, v in flow.results.artifacts.items() if v},
    }
    found = []
    for key, value in held.items():
        problem = artifact_problem(flow.run_path, key, value)
        if problem is not None:
            found.append(f"`{flow.name}` registers the artifact {key!r}: {problem}")
            flow.artifacts.pop(key, None)
            flow.results.artifacts.pop(key, None)
    return found


def execute(flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
    """Run the flow and collect its results, artifacts included.

    The sequence is `run()`, `parse_reports()` and `check_results()`."""
    record_identity(flow, run_path)

    tool_completed = False

    if flow.settings.reports_dir:
        flow.run_file(flow.settings.reports_dir).mkdir(exist_ok=True, parents=True)
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
        flow.results["error"] = error_info(e, node=flow.name, flow=flow).as_json_value()
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
    # A value that is no metric fails the node, and the metrics that are valid stay.
    invalid = collect_metrics(flow.results, type(flow).results_description)[2]
    for bad in invalid:
        log.error("%s", bad)
        flow.results.pop(bad.path, None)
    # An error that the run has already (a failed tool, a rejection) stays the cause.
    if invalid:
        earlier = flow.outcome == "failed" or bool(flow.results.get("error"))
        flow.outcome = "failed"
        if not earlier:
            flow.results["error"] = ErrorInfo(
                code=INVALID_METRIC,
                message="; ".join(str(bad) for bad in invalid),
                node=flow.name,
                exit_status=None,
                log=None,
            ).as_json_value()
    # A bad artifact is a bug of the flow. The node fails, and the record is written without
    # the artifact. An earlier error stays the cause, a rejection included.
    bad_artifacts = drop_bad_artifacts(flow)
    for line in bad_artifacts:
        log.error("%s", line)
    if bad_artifacts:
        earlier = flow.outcome == "failed" or bool(flow.results.get("error"))
        flow.outcome = "failed"
        if not earlier:
            flow.results["error"] = ErrorInfo(
                code=INTERNAL_ERROR,
                message="; ".join(bad_artifacts),
                node=flow.name,
                exit_status=None,
                log=None,
            ).as_json_value()
    success = flow.outcome == "passed"
    if not success and input_settings.verbosity != "quiet":
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


def clean_up_run(
    run_path: Path,
    run_directory: RunDirectory,
    flow_name: str,
    results: Mapping[str, Any],
    settings_json: Path,
    results_json: Path,
    run_root: Path,
    policy: RunDirPolicy,
) -> None:
    """Clean up after a run, as `policy` says.

    Either purge the run directory, or prune it to what the run reports and records
    (`settings.json`, `results.json`, artifacts). `results` holds the artifacts and the declared
    outputs of the run, as `Flow.results` or `launch_report.results_of` give them.

    Pruning removes the trace first. Until every flow declares every output it writes, a
    pruned directory may lack a file that a consumer reads, so the run must not be reused,
    not even if the pruning is interrupted halfway."""
    if policy.post_cleanup:
        remove_trace(run_path)
        if policy.purges:
            log.warning("Deleting run directory %s", run_path)
            # With every name of it, the link that the launch named it by included, so none
            # is left leading nowhere.
            run_directory.delete(*run_directory_names(run_path, flow_name, run_root))
        else:
            log.warning("Cleaning up %s", run_path)
            named_run_path = Path(os.path.abspath(run_path))
            run_path = run_path.resolve()
            kept: set[Path] = set()
            for raw_path in (
                settings_json,
                results_json,
                *iter_artifact_paths(results["artifacts"]),
                *declared_output_files(dict(results)),
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

            ancestors = {parent for path in kept for parent in path.parents if parent != run_path}
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
                        removed.extend(run_directory.remove(path))

            if run_path.is_relative_to(run_root) and run_path not in kept:
                prune(run_path)
            log.warning("Removed the following files: %s", " ".join(str(p) for p in removed))
