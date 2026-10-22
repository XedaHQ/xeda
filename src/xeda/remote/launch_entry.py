"""The remote launch entry: run one packaged request and record its result.

`python -m xeda.remote.launch_entry <launch dir> <hand-over fd>` first takes the locks that the
launch holds (`take_locks`), and then reads `<launch dir>/request.json` (a `LaunchRequest`),
plans the request, checks that this plan is the one the local xeda made, runs it, and writes
`<launch dir>/result.json`. The agent starts this module on the remote host. The launch holds the
locks until it ends. The exit status is 0 when the launch passed, 1 when it did not, 2 when the request is invalid
and 130 when the launch was interrupted. The result is in `result.json` in every case.

`result.json` is the document that `xeda run --json` prints (`Result.to_dict`), or the dry-run
document, so the local xeda can report it as it reports a local run. It has two more keys for a
launch that was not a dry run. `written` holds the files that this launch wrote, for each node
that ran. `unreadable` holds the directories under a run directory that could not be read, so
that the files under them are not in `written`. `named` holds the names of the nodes that the
request names: their declared outputs and artifacts come back.

The launch ends with a result even when it ends in `SystemExit` or another `BaseException`. The
entry writes the failure document first and then raises the exception again.
"""

import json
import logging
import os
import socket
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dataclass import XedaBaseModel
from ..utils import replacing_file
from .errors import RemoteError, RemoteIncompatible

log = logging.getLogger(__name__)

REQUEST_FILE = "request.json"
RESULT_FILE = "result.json"

#: What a launch reads from the lock hand-over socket: the most descriptors (a launch holds the
#: workspace lock, the lock of its environment and the lock of its code copy), and the most bytes
#: of the message, which is the number of descriptors as text.
HAND_OVER_MAX = 16
HAND_OVER_SIZE = 16

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
    a file under one counts as not written, and `written_under` names the directory. Only regular
    files count, and a symbolic link is never followed, the run directory itself included: a
    tool may leave links anywhere in its run directory, and a link there has no files."""

    def __init__(self, directories: Iterable[Path]) -> None:
        self.directories = [Path(os.path.abspath(directory)) for directory in directories]
        self.states: Dict[Path, FileState] = {}
        self.unread: List[Path] = []
        for directory in self.directories:
            self._scan(directory, self.states, self.unread)

    @staticmethod
    def _scan(top: Path, states: Dict[Path, FileState], unread: List[Path]) -> None:
        try:
            if not stat.S_ISDIR(os.lstat(top).st_mode):
                return
        except OSError:
            return
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

    def written_under(self, directory: Path) -> Tuple[List[str], List[str]]:
        """Return the files under `directory` that were written since the snapshot, and the
        directories under it (relative to it, `.` for itself) that could not be read, before or
        after the launch. Both are sorted and use `/` as the separator."""
        directory = Path(os.path.abspath(directory))
        after: Dict[Path, FileState] = {}
        unread_after: List[Path] = []
        self._scan(directory, after, unread_after)
        unread = {
            place
            for place in [*self.unread, *unread_after]
            if place == directory or directory in place.parents
        }
        written = sorted(
            path.relative_to(directory).as_posix()
            for path, state in after.items()
            if self.states.get(path) != state
            and not any(path.is_relative_to(place) for place in unread)
        )
        return written, sorted(place.relative_to(directory).as_posix() for place in unread)


def take_locks(hand_over: int) -> List[int]:
    """Take the lock files that the agent sent through the Unix socket `hand_over`, and return
    their descriptors. Close the socket. The message of the agent is the number of descriptors.

    The agent keeps the locks of the launch in flight in the socket while the login profile and
    the `setup` lines run, so that a program left in the background by them cannot inherit and
    hold a lock. The launch holds the descriptors until it ends. They are not inheritable, so no
    tool gets them. Raise `RemoteError` if the socket does not give them."""
    taken: List[int] = []
    try:
        with socket.socket(fileno=hand_over) as hand_over_socket:
            message, taken, _flags, _address = socket.recv_fds(
                hand_over_socket, HAND_OVER_SIZE, HAND_OVER_MAX
            )
            for descriptor in taken:
                os.set_inheritable(descriptor, False)
            if not message:
                raise RemoteError("the lock hand-over socket was closed with no message")
            try:
                expected = int(message)
            except ValueError:
                raise RemoteError(f"the lock hand-over has the message {message!r}") from None
            if len(taken) != expected:
                raise RemoteError(
                    f"the lock hand-over has {len(taken)} locks, and the agent sent {expected}"
                )
    except (OSError, RemoteError) as e:
        for descriptor in taken:
            os.close(descriptor)
        if isinstance(e, RemoteError):
            raise
        raise RemoteError(f"the launch did not get its locks: {e}") from None
    return taken


def early_failure(message: str) -> Dict[str, Any]:
    """The result document of a launch that ended before it knew its request: no spec, no nodes.

    The code of its error is `remote_failed`."""
    from ..outcomes import Outcome, run_document
    from ..records import ErrorInfo

    document = run_document(
        outcome=Outcome.FAILED,
        error=ErrorInfo(
            code="remote_failed",
            message=message,
            node=None,
            exit_status=None,
            log=None,
        ),
        launch_id=None,
        spec=None,
        roots=(),
        nodes=(),
    )
    document["written"] = {}
    document["unreadable"] = {}
    document["named"] = []
    return document


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
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Return the files written by each node that ran in this launch, by node name, and the
    directories of each node that could not be read (only nodes that have some).

    A node that was reused or rejected again wrote nothing, so it has no entry. A node that a
    failed producer blocked has one: the launch rewrites its results.json."""
    if snapshot is None or plan is None:
        return {}, {}
    directories = {os.path.abspath(path): name for name, path in _node_directories(plan).items()}
    written: Dict[str, List[str]] = {}
    unreadable: Dict[str, List[str]] = {}
    for flow in launched:
        run_path = os.path.abspath(flow.run_path)
        if flow.reused or flow.rejected_again or run_path not in directories:
            continue
        name = directories[run_path]
        written[name], unread = snapshot.written_under(Path(run_path))
        if unread:
            unreadable[name] = unread
    return written, unreadable


def _record_written(
    document: Dict[str, Any],
    snapshot: Optional[RunDirectorySnapshot],
    plan: Any,
    launched: Sequence[Any],
) -> None:
    """Add `written` and `unreadable` to a result document."""
    document["written"], document["unreadable"] = _written(snapshot, plan, launched)


def load_packaged_design(launcher: Any, path: Path, *, rebuild: bool, dry_run: bool) -> Any:
    """Load the design document that the local xeda packaged (`Design.from_file(packaged=True)`).

    The document is loaded as the launcher loads a design file: where the run root keeps what a
    load keeps in it, and with no side effect for a dry run."""
    from ..design import Design, loading_in_run_root, refusing_load_side_effects

    with (
        loading_in_run_root(launcher.load_run_root, rebuild),
        refusing_load_side_effects() if dry_run else nullcontext(),
    ):
        return Design.from_file(path, packaged=True)


def execute(request: LaunchRequest) -> Tuple[Dict[str, Any], Optional[BaseException]]:
    """Run the request and return the result document and the exception to raise again, if any.

    Any exception becomes a failure document. The exception to raise again is a `BaseException`
    that is neither an `Exception` nor a `KeyboardInterrupt` (for example `SystemExit`): the
    caller writes the document first.

    This changes the working directory of the process to `request.inputs_dir`, as a local launch
    runs in the directory of its design."""
    # The launcher and the CLI helpers load the whole of Xeda, so they load here and not when this
    # module is imported (the agent imports `LaunchRequest` and nothing else).
    from ..cli import setup_logger
    from ..flow.flow import using_path_identities
    from ..flow_runner import DefaultRunner
    from ..flow_runner.chains import parse_request
    from ..flow_runner.error_codes import public_error, request_error_info
    from ..flow_runner.launch_result import failed_result, launch_result
    from ..errors import SpecError
    from ..introspect import design_info, plan_info, request_info
    from ..outcomes import Outcome, run_document, spec_document
    from ..records import CANCELED, ErrorInfo
    from ..spec import Spec

    debug = request.log_level <= logging.DEBUG
    launcher: Optional[DefaultRunner] = None
    design: Optional[Path] = None
    plan: Any = None
    snapshot: Optional[RunDirectorySnapshot] = None
    loaded: Any = None
    parsed: Any = None
    try:
        setup_logger(request.log_level, request.detailed_logs)
        parsed = parse_request(request.chain)
        inputs_dir = Path(request.inputs_dir)
        design = inputs_dir / request.design_file
        # The one chdir of Xeda: this is the entry point of a dedicated process, so it changes the
        # state of no other process.
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
        loaded = load_packaged_design(
            launcher, design, rebuild=request.rebuild_all or request.clean, dry_run=request.dry_run
        )
        arguments: Dict[str, Any] = dict(design=loaded)
        with using_path_identities(request.path_identities):
            if request.dry_run:
                plan, freshness = launcher.plan_with_freshness(request.chain, **arguments)
                check_identity({n.name: n.flowrun_hash for n in plan.nodes}, request.expected)
                return {
                    "flow": parsed.requested.name,
                    **design_info(design, launcher.design_name),
                    "target": plan.context.target,
                    "success": True,
                    "dry_run": True,
                    "request": request_info(parsed),
                    "plan": plan_info(plan, freshness),
                }, None
            plan = launcher.plan(request.chain, **arguments)
            check_identity({n.name: n.flowrun_hash for n in plan.nodes}, request.expected)
            snapshot = RunDirectorySnapshot(_node_directories(plan).values())
            result = launch_result(
                launcher,
                Spec(request.chain, loaded),
                lambda: launcher.run(request.chain, **arguments),
            )
        document = result.to_dict()
        _record_written(document, snapshot, plan, launcher.launched)
        document["named"] = [element["node"] for element in request_info(parsed)]
        return document, None
    except BaseException as e:  # pylint: disable=broad-except
        interrupted = isinstance(e, KeyboardInterrupt)
        if interrupted:
            error = ErrorInfo(
                code=CANCELED,
                message="The launch was interrupted.",
                node=None,
                exit_status=None,
                log=None,
            )
        elif isinstance(e, Exception):
            error = request_error_info(
                e, code="remote_failed" if isinstance(e, RemoteError) else None
            )
        else:
            error = ErrorInfo(
                code="internal_error",
                message=f"The launch ended with {type(e).__name__}: {e!r}.",
                node=None,
                exit_status=None,
                log=None,
            )
        log.critical("%s", error.message)
        refused = isinstance(e, Exception) and isinstance(public_error(e), SpecError)
        if launcher is not None and loaded is not None and plan is not None and not refused:
            # Every planned node keeps what the launch did with it: a node that passed stays
            # passed, the node that was running is canceled by an interrupt, and any other
            # node is skipped or blocked.
            document = failed_result(
                launcher, Spec(request.chain, loaded), plan, error, interrupted=interrupted
            ).to_dict()
        else:
            document = run_document(
                outcome=None if refused else Outcome.CANCELED if interrupted else Outcome.FAILED,
                error=error,
                launch_id=None,
                spec=spec_document(
                    flow=request.chain,
                    **design_info(
                        design,
                        (
                            launcher.design_name
                            if launcher is not None
                            else Path(request.design_file).stem
                        ),
                    ),
                    target=None,
                    settings={},
                ),
                roots=[],
                nodes=[],
            )
        if not request.dry_run:
            _record_written(
                document, snapshot, plan, launcher.launched if launcher is not None else ()
            )
            document["named"] = (
                [element["node"] for element in request_info(parsed)] if parsed is not None else []
            )
        return document, None if isinstance(e, Exception | KeyboardInterrupt) else e


def exit_status(document: Mapping[str, Any]) -> int:
    """Return the exit status of a launch with the result `document`: 0 for a launch that
    passed, 130 for one that was interrupted, 2 for a request that started no launch because it
    is invalid, and 1 for any other launch."""
    if document.get("dry_run"):
        return 0 if document.get("success") else 1
    error = document.get("error") or {}
    outcome = document.get("outcome")
    if outcome == "passed":
        return 0
    if error.get("code") == "canceled":
        return 130
    return 2 if outcome is None else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the launch in the directory that `argv` names and return the exit status. The first
    action is to take the locks from the socket that `argv` names."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or not arguments[1].isdecimal():
        print(
            "usage: python -m xeda.remote.launch_entry <launch dir> <hand-over fd>", file=sys.stderr
        )
        return 2
    launch_dir = Path(arguments[0])
    try:
        take_locks(int(arguments[1]))  # nothing closes the descriptors: they end with the process
    except RemoteError as e:
        message = str(e)
        print(message, file=sys.stderr)
        write_result(launch_dir, early_failure(message))
        return 1
    try:
        request = LaunchRequest.model_validate_json(
            (launch_dir / REQUEST_FILE).read_text(encoding="utf-8")
        )
    except Exception as e:  # pylint: disable=broad-except
        # Nothing is known of the request, so the document has no flow and no design.
        message = f"{REQUEST_FILE} is not a launch request: {type(e).__name__}: {e}"
        print(message, file=sys.stderr)
        write_result(launch_dir, early_failure(message))
        return 1
    document, raised = execute(request)
    write_result(launch_dir, document)
    if raised is not None:
        raise raised
    return exit_status(document)


if __name__ == "__main__":
    sys.exit(main())
