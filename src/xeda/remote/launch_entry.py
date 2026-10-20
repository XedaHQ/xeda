"""The remote launch entry: run one packaged request and record its result.

`python -m xeda.remote.launch_entry <launch dir>` reads `<launch dir>/request.json` (a
`LaunchRequest`), plans the request, checks that this plan is the one the local xeda made, runs
it, and writes `<launch dir>/result.json`. The agent starts this module on the remote host. The
exit status is 0 when the launch succeeded and 1 when it did not. The result is in
`result.json` in both cases.

`result.json` is the document that `xeda run --json` prints (or the dry-run document), so the
local xeda can report it as it reports a local run. It has one more key, `written`, for a launch
that was not a dry run: the files that this launch wrote, for each node that ran.
"""

import json
import logging
import os
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dataclass import XedaBaseModel
from ..utils import replacing_file
from .errors import RemoteIncompatible

log = logging.getLogger(__name__)

REQUEST_FILE = "request.json"
RESULT_FILE = "result.json"

#: What identifies the state of a file without comparing a clock of another machine: the device
#: and inode, the size and the two time stamps. A file system gives all five.
FileState = Tuple[int, int, int, int, int]


class LaunchRequest(XedaBaseModel):
    """What the local xeda asks the remote launch to do. All paths are on the remote host."""

    #: The request text of `xeda run` (a flow name or a chain).
    chain: str
    #: The design file, relative to `inputs_dir`.
    design_file: str
    #: The directory that holds the design file and its sources. The launch runs there.
    inputs_dir: str
    #: The run root of the launch.
    run_root: str
    #: The identity of every shipped read path, by its path here (`using_path_identities`).
    path_identities: Dict[str, str]
    hashed_run_dirs: bool
    clean: bool
    rebuild_all: bool
    scrub: bool
    dry_run: bool
    log_level: int
    detailed_logs: bool
    #: The `flowrun_hash` of every node of the local plan, by node name.
    expected: Dict[str, str]


def check_identity(planned: Mapping[str, str], expected: Mapping[str, str]) -> None:
    """Raise `RemoteIncompatible` if the planned nodes are not the expected ones.

    `planned` and `expected` map a node name to its `flowrun_hash`. The remote resolves the same
    request as the local xeda did, or it runs nothing."""
    problems: List[str] = []
    for name in sorted(set(expected) - set(planned)):
        problems.append(f"the local plan has the node `{name}`, and the remote plan has not")
    for name in sorted(set(planned) - set(expected)):
        problems.append(f"the remote plan has the node `{name}`, and the local plan has not")
    for name in sorted(set(planned) & set(expected)):
        if planned[name] != expected[name]:
            problems.append(
                f"the node `{name}` has the identity {planned[name]} here and "
                f"{expected[name]} in the local plan"
            )
    if problems:
        raise RemoteIncompatible(
            "The remote resolved another plan than the local xeda did: " + "; ".join(problems)
        )


class RunDirectorySnapshot:
    """The files under some run directories at one time, to tell later what a launch wrote.

    A file counts as written when its path was not here before, or its state changed
    (`FileState`). A directory that could not be read says nothing about the files under it, so
    a file under one counts as not written. Only regular files count, and a symbolic link is
    never followed: a tool may leave links anywhere in its run directory."""

    def __init__(self, directories: Iterable[Path]) -> None:
        self.directories = [Path(os.path.abspath(directory)) for directory in directories]
        self.states: Dict[Path, FileState] = {}
        self.unread: List[Path] = []
        for directory in self.directories:
            self._scan(directory, self.states, self.unread)

    @staticmethod
    def _scan(top: Path, states: Dict[Path, FileState], unread: List[Path]) -> None:
        pending = [top]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                continue
            except OSError:
                unread.append(directory)
                continue
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(st.st_mode):
                    states[Path(entry.path)] = (
                        st.st_dev,
                        st.st_ino,
                        st.st_size,
                        st.st_mtime_ns,
                        st.st_ctime_ns,
                    )

    def written_under(self, directory: Path) -> List[str]:
        """Return the files under `directory` that were written since the snapshot, relative to
        it, sorted, with `/` as the separator."""
        directory = Path(os.path.abspath(directory))
        after: Dict[Path, FileState] = {}
        self._scan(directory, after, [])
        unread = [
            place for place in self.unread if place == directory or directory in place.parents
        ]
        return sorted(
            path.relative_to(directory).as_posix()
            for path, state in after.items()
            if self.states.get(path) != state
            and not any(path.is_relative_to(place) for place in unread)
        )


def write_result(launch_dir: Path, document: Mapping[str, Any]) -> None:
    """Write `result.json` of the launch complete or not at all."""
    with replacing_file(launch_dir / RESULT_FILE, encoding="utf-8") as f:
        json.dump(document, f, indent=2, default=str)
        f.write("\n")


def _node_directories(plan: Any) -> Dict[str, Path]:
    """Return the run directory of every planned node, by node name."""
    return {node.name: Path(node.run_path) for node in plan.nodes}


def _written(
    snapshot: Optional[RunDirectorySnapshot], plan: Any, launched: Sequence[Any]
) -> Dict[str, List[str]]:
    """Return the files written by each node that ran in this launch, by node name.

    A node that was reused or rejected again wrote nothing, so it has no entry. A node that a
    failed producer blocked has one: the launch rewrites its results.json."""
    if snapshot is None or plan is None:
        return {}
    directories = {os.path.abspath(path): name for name, path in _node_directories(plan).items()}
    written: Dict[str, List[str]] = {}
    for flow in launched:
        run_path = os.path.abspath(flow.run_path)
        if flow.reused or flow.rejected_again or run_path not in directories:
            continue
        written[directories[run_path]] = snapshot.written_under(Path(run_path))
    return written


def execute(request: LaunchRequest) -> Dict[str, Any]:
    """Run the request and return the result document. Any exception becomes a failure document.

    This changes the working directory of the process to `request.inputs_dir`, as a local launch
    runs in the directory of its design."""
    # The launcher and the CLI helpers load the whole of Xeda, so they load here and not when this
    # module is imported (the agent imports `LaunchRequest` and nothing else).
    from ..cli import (
        _failure_document,
        _failure_of,
        _run_document,
        setup_logger,
    )
    from ..flow.flow import using_path_identities
    from ..flow_runner import DefaultRunner
    from ..flow_runner.chains import parse_request
    from ..introspect import design_info, plan_info, request_info

    debug = request.log_level <= logging.DEBUG
    flow = request.chain
    elements: List[Dict[str, Any]] = []
    launcher: Optional[DefaultRunner] = None
    design: Optional[Path] = None
    plan: Any = None
    snapshot: Optional[RunDirectorySnapshot] = None
    try:
        setup_logger(request.log_level, request.detailed_logs)
        parsed = parse_request(request.chain)
        flow = parsed.requested.name
        elements = request_info(parsed)
        inputs_dir = Path(request.inputs_dir)
        design = inputs_dir / request.design_file
        os.chdir(inputs_dir)
        launcher = DefaultRunner(
            request.run_root,
            rebuild_all=request.rebuild_all,
            hashed_run_dirs=request.hashed_run_dirs,
            clean=request.clean,
            scrub_old_runs=request.scrub,
            display_results=False,
            debug=debug,
        )
        # The design is a file that the local xeda wrote from its own loaded design, so it holds
        # no key that the design model refuses.
        arguments: Dict[str, Any] = dict(design=design, design_allow_extra=False)
        with using_path_identities(request.path_identities):
            if request.dry_run:
                plan, freshness = launcher.plan_with_freshness(request.chain, **arguments)
                check_identity({n.name: n.flowrun_hash for n in plan.nodes}, request.expected)
                return {
                    "flow": flow,
                    **design_info(design, launcher.design_name),
                    "target": plan.context.target,
                    "success": True,
                    "dry_run": True,
                    "request": elements,
                    "plan": plan_info(plan, freshness),
                }
            plan = launcher.plan(request.chain, **arguments)
            check_identity({n.name: n.flowrun_hash for n in plan.nodes}, request.expected)
            snapshot = RunDirectorySnapshot(_node_directories(plan).values())
            f = launcher.run(request.chain, **arguments)
        success = bool(f and f.results.success)
        document = _run_document(
            flow,
            design,
            f,
            success,
            launcher.launched,
            launcher.last_plan,
            launcher.target,
            launcher.design_name,
        )
        document["request"] = elements
        document["written"] = _written(snapshot, plan, launcher.launched)
        return document
    except (Exception, KeyboardInterrupt) as e:
        if isinstance(e, KeyboardInterrupt):
            error = ("KeyboardInterrupt", "The launch was interrupted.")
        else:
            error = _failure_of(e, flow, debug)
        log.critical("%s", error[1])
        document = _failure_document(flow, design, launcher, None, *error, elements)
        if not request.dry_run:
            document["written"] = _written(
                snapshot, plan, launcher.launched if launcher is not None else ()
            )
        return document


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the launch in the directory that `argv` names and return the exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m xeda.remote.launch_entry <launch dir>", file=sys.stderr)
        return 2
    launch_dir = Path(arguments[0])
    try:
        request = LaunchRequest.model_validate_json(
            (launch_dir / REQUEST_FILE).read_text(encoding="utf-8")
        )
    except Exception as e:  # pylint: disable=broad-except
        # Nothing is known of the request, so the document has no flow and no design.
        message = f"{REQUEST_FILE} is not a launch request: {type(e).__name__}: {e}"
        print(message, file=sys.stderr)
        write_result(
            launch_dir,
            {
                "flow": None,
                "design": None,
                "design_file": None,
                "target": None,
                "success": False,
                "results": {},
                "error": {"type": type(e).__name__, "message": message},
                "request": [],
                "nodes": [],
                "written": {},
            },
        )
        return 1
    document = execute(request)
    write_result(launch_dir, document)
    if document.get("error", {}).get("type") == "KeyboardInterrupt":
        return 130
    return 0 if document["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
