"""The run stage of a launch: what happens around `run()`, `parse_reports()` and `check_results()`.

Free functions with explicit arguments. This module never imports `default_runner`: the
launcher passes what each function needs.
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
from ..run_dir import RunDirectory, RunDirectoryError
from ..tool import NonZeroExitCode
from ..utils import WorkingDirectory
from .outputs import record_outputs

log = logging.getLogger(__name__)


def refuse_inputs_inside(run_path: Path, flow_name: str, files: Iterable[Path]) -> None:
    """xeda empties and rewrites a flow's run directory, so no file of the design, and no
    file a setting reads, may lie in the run directory of the flow that reads it (in another
    run's directory it is that run's output, read by content)."""
    run_dir = Path(os.path.realpath(run_path))
    inside = sorted({f for f in files if Path(os.path.realpath(f)).is_relative_to(run_dir)})
    if inside:
        raise RunDirectoryError(
            f"{inside[0]} lies in {run_path}, {flow_name}'s own run directory, which xeda "
            "empties and rewrites: keep your files in your design, outside the run root"
        )


def free_working_locations(flow: Flow) -> None:
    """Before the run: each working location of `flow` (a setting with the `WORKING` role, a name
    inside the run directory) is where its tools work, never through a symbolic link out of the
    run directory. A tool may leave its working location's own name a link, anywhere: that link
    is removed as itself -- never what it points to -- and the tool makes the location anew. One
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
    """What every results document says of the run, whether it succeeded or not."""
    flow.results["design"] = flow.design.name
    flow.results["design_hash"] = flow.design_hash
    flow.results["flow"] = flow.name
    flow.results["flow_hash"] = flow.flow_hash
    flow.results["settings_hash"] = flow.settings_hash
    flow.results["run_path"] = run_path.absolute()
    flow.results.timestamp = flow.timestamp


def execute(flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
    """Stage 6: `run()`, `parse_reports()`, and the run's results, artifacts included."""
    record_identity(flow, run_path)

    success = True

    with WorkingDirectory(run_path):
        if flow.settings.reports_dir:
            flow.settings.reports_dir.mkdir(exist_ok=True, parents=True)
        try:
            flow.run()
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
            flow.results["error"] = {"type": type(e).__name__, "message": str(e)}
            success = False
        if flow.init_time is not None:
            flow.results.runtime = time.monotonic() - flow.init_time
        try:
            success &= flow.parse_reports()
            success &= flow.check_results()
        except Exception as e:  # pylint: disable=broad-except
            log.critical("parse_reports or check_results threw an exception: %s", e)
            if success:  # if so far so good this is a bug!
                raise e
        flow.add_canonical_result_aliases()
        if success:
            # Consumers use these integrity records for fresh and reused producers alike.
            problems = record_outputs(flow)
            for problem in problems:
                log.error("%s", problem)
            if problems:
                success = False
                flow.results["error"] = {
                    "type": "MissingOutput",
                    "message": "; ".join(problems),
                }
        if not success and not input_settings.is_quiet:
            log.debug("Failure was reported in the parsed results.")
        if not success and not flow.results.get("error"):
            # no exception and no exit status: the reports or checks said so; a failure
            # document always names its error (and a depender quotes it). This is a node's
            # cause, `ReportedFailure`; `FlowFailed` is the verdict the top-level `--json`
            # document of `xeda run` gives whatever the cause, so the names must differ.
            flow.results["error"] = {
                "type": "ReportedFailure",
                "message": (
                    f"`{flow.name}` reported failure: its reports or checks did not pass "
                    "(see the log and its run directory)"
                ),
            }
        flow.results.success = success
    for k, v in flow.artifacts.items():
        if not flow.results.artifacts.get(k):
            flow.results.artifacts[k] = v
