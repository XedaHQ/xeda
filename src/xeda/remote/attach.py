"""Follow, fetch and cancel a launch that keeps running: the code behind `xeda remote attach` and
`xeda remote cancel`.

`attach_launch` connects to the host and asks the agent for a launch of the workspace of this
machine, run root and host: the one with the given id, else the newest that runs, else the newest.
It shows the log of the launch from its start and waits for the end. Then it fetches the result
into the mirror as a normal remote run does, with the local record of the launch
(`launch_record`). It delivers nothing. `cancel_launch` stops the launch with the cancel path of a
normal remote run: SIGINT, SIGTERM and SIGKILL with the grace periods, descendants too.
"""

import logging
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Sequence

from ..flow_runner import DefaultRunner
from .client import (
    RemoteOutcome,
    _discard_mirrors,
    _failed,
    _finish,
    _FETCHING,
    _follow,
    _local_run_root,
    _localized,
    _Local,
    _provision,
    _Run,
    _start_agent,
    _translation,
    workspace_key,
)
from .environment import code_snapshot, dependency_spec
from .errors import RemoteCanceled, RemoteError, RemoteInterrupted, RemoteProtocolError
from .launch_record import LaunchRecord, read_record
from .profile import load_host_profile
from .ssh import SshSession, resolve_destination

log = logging.getLogger(__name__)


def _check_platform() -> None:
    from .client import _is_windows

    if _is_windows():
        raise RemoteError(
            "`xeda remote` does not work on native Windows. Use WSL, or run xeda on a Linux or "
            "macOS machine."
        )


def attach_launch(
    host: str,
    launch_id: Optional[str],
    *,
    run_root: Path,
    ssh_options: Sequence[str] = (),
) -> RemoteOutcome:
    """Follow a launch on `host` and fetch its result. Return the outcome as `run_remote` does."""
    run = _Run(chain="", flow="", design=None, target=None, host=host, debug=False)
    try:
        return _attach(run, host, launch_id, run_root, ssh_options)
    except Exception as error:  # pylint: disable=broad-except
        return _failed(run, error)
    except KeyboardInterrupt:
        # Ctrl-C while the launch runs only stops the follow (`RemoteDetached`). This is before
        # the connection works, or while the result is fetched.
        if run.phase is not None:
            return _failed(
                run,
                RemoteInterrupted(
                    f"Canceled by Ctrl-C while {run.phase}. The launch on {host} had finished, "
                    "and its results are on the host. Run `xeda remote attach` again to fetch "
                    "them."
                ),
            )
        return _failed(run, RemoteCanceled("Canceled by Ctrl-C. The launch was not changed."))


def _attach(
    run: _Run, host: str, launch_id: Optional[str], run_root: Path, ssh_options: Sequence[str]
) -> RemoteOutcome:
    _check_platform()
    profile = load_host_profile(host)
    destination = resolve_destination(host, ssh_options)
    local_root = _local_run_root(run_root)
    mirror_root = local_root / f"@{destination.label()}"
    key = workspace_key(local_root, destination)
    log.info("Connecting to %s...", host)
    with SshSession(destination, ssh_options) as session:
        _facts, provisioned = _provision(session, profile, code_snapshot(), dependency_spec(()))
        agent = _start_agent(session, provisioned, key)
        agent.keep_running = True
        try:
            step = "finding the launch"
            agent.send({"attach": {"workspace": key, "id": launch_id}}, step)
            found = agent.expect("attached", step)
            if not isinstance(found, dict) or not isinstance(found.get("id"), str):
                raise RemoteProtocolError(f"The agent on {host} sent a bad `attached` record.")
            number = found["id"]
            record = read_record(mirror_root, number)
            _fill(run, record)
            run.launch_id = number
            _say_which(host, found, launch_id)
            local = _Local(
                DefaultRunner(run_root, display_results=False),
                record.requested,
                record.design_name,
                [Path(path) for path in record.run_paths],
                None,
            )
            remote_run_root = PurePosixPath(record.remote_run_root)
            try:
                result = _follow(agent, number)
            except BaseException:
                _discard_mirrors(local, mirror_root, local_root)
                raise
            run.phase = _FETCHING
            translate = _translation(
                remote_run_root=remote_run_root,
                mirror_root=mirror_root,
                inputs_dir=PurePosixPath(record.inputs_dir),
                files=record.files,
                generated=record.generated,
                design_root=Path(record.design_root),
            )
            run.localize = lambda: _localized(
                result,
                run,
                mirror_root=mirror_root,
                remote_run_root=remote_run_root,
                translate=translate,
            )
            outcome = _finish(
                agent,
                local,
                run,
                result,
                mirror_root=mirror_root,
                local_root=local_root,
                remote_run_root=remote_run_root,
                translate=translate,
                fetch_all=record.fetch_all,
                outputs_to=None,
            )
            agent.send({"bye": True}, "ending the session")
        except BaseException:
            agent.close(wait=5)
            raise
        agent.close()
    if record.delivers:
        log.info(
            "The request had --outputs-to, and attach delivers nothing. Run the original command "
            "again to deliver: it reuses this finished run."
        )
    return outcome


def _fill(run: _Run, record: LaunchRecord) -> None:
    """Give the run the names of the request that the record holds."""
    run.chain, run.flow, run.design = record.chain, record.flow, record.design
    run.target, run.debug, run.request = record.target, record.debug, record.request
    run.design_name, run.settings = record.design_name, record.settings
    run.local_root = Path(record.local_root)
    run.mirror_root = run.local_root / f"@{run.host}"
    run.nodes = [
        (name, flow, Path(path)) for (name, flow), path in zip(record.nodes, record.run_paths)
    ]


def _say_which(host: str, found: Dict[str, Any], asked: Optional[str]) -> None:
    number, state = found["id"], found.get("state")
    if found.get("running"):
        log.info(
            "Following the launch %s on %s (running since %s).", number, host, found["started"]
        )
    elif asked is None:
        log.info("No launch runs on %s. Showing the newest launch, %s (%s).", host, number, state)
    else:
        log.info("The launch %s on %s is %s.", number, host, state)


def cancel_launch(
    host: str,
    launch_id: Optional[str],
    *,
    run_root: Path,
    ssh_options: Sequence[str] = (),
) -> Dict[str, Any]:
    """Cancel a running launch on `host`. Return the document of `xeda remote cancel`."""
    _check_platform()
    profile = load_host_profile(host)
    destination = resolve_destination(host, ssh_options)
    key = workspace_key(_local_run_root(run_root), destination)
    log.info("Connecting to %s...", host)
    with SshSession(destination, ssh_options) as session:
        _facts, provisioned = _provision(session, profile, code_snapshot(), dependency_spec(()))
        agent = _start_agent(session, provisioned, key)
        try:
            step = "canceling the launch"
            agent.send({"cancel": {"workspace": key, "id": launch_id}}, step)
            agent.expect("canceling", step)
            stopped = agent.expect("stopped", step)
        except BaseException:
            agent.close(wait=5)
            raise
        agent.close()
    if not isinstance(stopped, dict):
        raise RemoteProtocolError(f"The agent on {host} sent a bad `stopped` record.")
    survivors = int(stopped.get("survivors") or 0)
    return {
        "success": survivors == 0,
        "host": host,
        "launch": stopped.get("id"),
        "survivors": survivors,
    }


__all__ = ["attach_launch", "cancel_launch"]
