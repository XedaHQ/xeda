"""The `xeda remote` commands: look at a remote host, clean it, and follow or cancel a launch.

`check HOST` connects like a remote run does, makes the environment if it is missing, and asks the
agent to describe the host. `clean HOST` asks the agent to list the environments, code copies and
workspaces that nobody uses, asks the user, and then asks the agent to remove them. The agent
never removes what a live lock or a running launch holds.

`attach HOST [LAUNCH_ID]` follows a launch that `xeda run --remote HOST --keep-running` started and
fetches its result into the mirror. `cancel HOST [LAUNCH_ID]` stops such a launch.

The module imports only what `click` needs to declare the commands. The code that runs them is
imported when a command runs, so shell completion stays light.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click

if TYPE_CHECKING:
    from .profile import HostProfile
    from .provision import Probe, Provisioned
    from .ssh import SshSession

__all__ = ["add_remote_commands"]

log = logging.getLogger(__name__)

#: Seconds that the agent may take to look for the executables.
_CHECK_TIMEOUT = 300.0

#: Seconds that the agent may take to examine or remove the entries of the remote root.
_CLEAN_TIMEOUT = 3600.0

#: The program that a dockerized flow needs. The check reports it apart from the programs of the
#: flows, which a native run needs.
DOCKER = "docker"


def _fail(error_type: str, message: str, json_flag: bool) -> NoReturn:
    """Report a failure as every command does: a document with `success` false with `--json`, else
    the message alone on stderr. Exit with status 1."""
    if json_flag:
        from ..cli_utils import emit_structured

        emit_structured(
            {"success": False, "error": {"type": error_type, "message": message}}, "json"
        )
    else:
        click.echo(message, err=True)
    sys.exit(1)


def _profile_of(host: str, json_flag: bool) -> HostProfile:
    """Return the profile of `host` from the profile file of this user, or report the failure."""
    from .profile import host_profile, read_host_profiles, remotes_file

    try:
        return host_profile(host, read_host_profiles(remotes_file()))
    except Exception as error:  # pylint: disable=broad-except
        log.debug("The host profiles could not be read", exc_info=True)
        _fail(type(error).__name__, str(error), json_flag)


def _set_up(ctx: click.Context, json_flag: bool) -> None:
    """Give stdout to the document with `--json`, and set the log level: the log shows the steps
    of the provisioning."""
    from ..cli import setup_logger
    from ..cli_utils import machine_readable_mode

    if json_flag:
        machine_readable_mode()
    setup_logger(logging.INFO, False)


def _agent_document(
    session: SshSession,
    provisioned: Provisioned,
    step: str,
    *arguments: str,
    timeout: float,
) -> dict[str, Any]:
    """Run a one-shot command of the agent on the host and return its document."""
    from .client import _during
    from .errors import RemoteConfigError, RemoteError, RemoteProtocolError
    from .frames import skip_to_magic
    from .provision import agent_command

    host = session.destination.text
    try:
        command = agent_command(provisioned, *arguments)
    except ValueError:
        raise RemoteConfigError(
            f"The remote root {provisioned.root!r} or an argument has a character that xeda "
            "cannot put in a remote command line. Set `root` in the host profile to another path."
        ) from None
    with _during(step):
        try:
            done = session.run(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RemoteError(
                f"The agent on {host} did not finish {step} in {timeout:.0f} seconds."
            ) from None
    detail = done.stderr.decode("utf-8", "replace").strip()
    detail = f"\nOutput of the remote host:\n{detail}" if detail else ""
    stream = io.BytesIO(done.stdout)
    try:
        skip_to_magic(stream)
        document = json.loads(stream.read())
    except (RemoteProtocolError, ValueError) as error:
        raise RemoteProtocolError(
            f"The agent on {host} gave no document while {step}: {error}.{detail}"
        ) from None
    if not isinstance(document, dict):
        raise RemoteProtocolError(f"The agent on {host} sent a document that is not an object.")
    problem = document.get("error")
    if problem is not None:
        text = (
            f"{problem.get('type', 'error')}: {problem.get('message', '')}"
            if isinstance(problem, dict)
            else str(problem)
        )
        raise RemoteError(f"The agent on {host} failed while {step}: {text}{detail}")
    if done.returncode != 0:
        raise RemoteError(
            f"The agent on {host} ended with status {done.returncode} while {step}.{detail}"
        )
    return document


def _connect_and_run(host: str, profile: HostProfile, work: Any) -> Any:
    """Check the profile, connect to `host`, make the environment (the steps show in the log) and
    call `work(session, probe, provisioned)`. Return its value."""
    from .client import _is_windows, _provision
    from .environment import code_snapshot, dependency_spec
    from .errors import RemoteError
    from .ssh import SshSession, resolve_destination

    if _is_windows():
        raise RemoteError(
            "`xeda remote` does not work on native Windows. Use WSL, or run xeda on a Linux or "
            "macOS machine."
        )
    destination = resolve_destination(host)
    snapshot = code_snapshot()
    spec = dependency_spec(())
    log.info("Connecting to %s...", host)
    with SshSession(destination) as session:
        facts, provisioned = _provision(session, profile, snapshot, spec)
        return work(session, facts, provisioned)


def _run(json_flag: bool, host: str, work: Any) -> Any:
    """Run `work` on the host, and report a failure as `_fail` does."""
    profile = _profile_of(host, json_flag)
    try:
        return _connect_and_run(host, profile, work)
    except KeyboardInterrupt:
        _fail(
            "RemoteCanceled",
            f"Canceled by Ctrl-C. The command on {host} may be unfinished.",
            json_flag,
        )
    except Exception as error:  # pylint: disable=broad-except
        log.debug("The remote command failed", exc_info=True)
        _fail(type(error).__name__, str(error), json_flag)


def _size(count: int) -> str:
    value = float(count)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------------------------
# xeda remote check
# ---------------------------------------------------------------------------------------------


def describe_host(
    host: str, session: SshSession, facts: Probe, provisioned: Provisioned
) -> dict[str, Any]:
    """Ask the agent to describe the host and return the document of `xeda remote check`."""
    from .. import introspect

    names = introspect.flow_executables()
    report = _agent_document(
        session,
        provisioned,
        "checking the host",
        "check",
        "--json",
        *names,
        DOCKER,
        timeout=_CHECK_TIMEOUT,
    )
    environment = report.get("environment") or {}
    prefix = environment.get("prefix", "")
    found = report.get("executables") or {}
    warnings: list[str] = []
    if report.get("kill_user_processes") == "yes":
        warnings.append(
            "systemd-logind kills the processes of a user when the last session ends "
            "(KillUserProcesses=yes). A launch that keeps running on this host would not survive a "
            "logout. Ask the administrator to enable lingering for the user (`loginctl "
            "enable-linger`)."
        )
    if report.get("bash") is None:
        warnings.append("The host has no bash, so xeda cannot look for the executables.")
    return {
        "success": True,
        "host": host,
        "platform": {
            "system": facts.platform.system,
            "machine": facts.platform.machine,
            "libc": facts.platform.libc,
            "libc_version": facts.platform.libc_version,
        },
        "xeda": report.get("xeda"),
        "python": report.get("python"),
        "environment": {
            "key": provisioned.deps_key,
            "code": provisioned.code_key,
            "prefix": prefix,
            "lock_file": f"{prefix}/xeda-lock.txt",
            "lock": environment.get("lock", []),
        },
        "disk": report.get("disk"),
        "kill_user_processes": report.get("kill_user_processes"),
        "executables": {
            "found": {name: path for name, path in found.items() if path and name in names},
            "missing": [name for name in names if not found.get(name)],
        },
        "docker": {"found": bool(found.get(DOCKER)), "path": found.get(DOCKER) or None},
        "warnings": warnings,
    }


def _print_check(document: dict[str, Any]) -> None:
    platform = document["platform"]
    python = document["python"] or {}
    environment = document["environment"]
    disk = document["disk"] or {}
    packages = [line for line in environment["lock"] if line.strip() and not line.startswith("#")]
    click.echo(
        f"Host {document['host']}: {platform['system']} {platform['machine']}, "
        f"{platform['libc']} {platform['libc_version']}"
    )
    click.echo(f"Xeda {document['xeda']}")
    click.echo(
        f"Python {python.get('version')} ({python.get('implementation')}) at "
        f"{python.get('executable')}"
    )
    click.echo(f"Environment {environment['key']} at {environment['prefix']}")
    click.echo(f"Lock file {environment['lock_file']} ({len(packages)} lines)")
    if disk:
        click.echo(f"Disk {disk['path']}: {_size(disk['free'])} free of {_size(disk['total'])}")
    click.echo(f"KillUserProcesses: {document['kill_user_processes']}")
    executables = document["executables"]
    click.echo(f"Executables on the login PATH ({len(executables['found'])} found):")
    for name, path in executables["found"].items():
        click.echo(f"  found    {name}  {path}")
    for name in executables["missing"]:
        click.echo(f"  missing  {name}")
    docker = document["docker"]
    click.echo(
        "docker (needed only for dockerized flows): "
        + (f"found  {docker['path']}" if docker["found"] else "missing")
    )
    for warning in document["warnings"]:
        click.echo(f"Warning: {warning}", err=True)


@click.argument("host")
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Write one JSON document to stdout."
)
@click.pass_context
def remote_check(ctx: click.Context, host: str, json_flag: bool) -> None:
    """Connect to HOST and report what a remote run finds there.

    The command makes the environment of xeda on the host if it is missing (the same step as a
    remote run). Then it reports the Python, the environment and its lock file, the free disk
    space, the systemd-logind setting `KillUserProcesses` (a launch that outlives a logout needs
    `no`), and which programs of the flows are on the login PATH of the host. A missing program
    is not an error: the exit status is 0 when the connection and the environment work.
    """
    _set_up(ctx, json_flag)
    document = _run(
        json_flag,
        host,
        lambda session, facts, provisioned: describe_host(host, session, facts, provisioned),
    )
    if json_flag:
        from ..cli_utils import emit_structured

        emit_structured(document, "json")
    else:
        _print_check(document)


# ---------------------------------------------------------------------------------------------
# xeda remote clean
# ---------------------------------------------------------------------------------------------


def _clean_arguments(root: str, older_than: float, everything: bool) -> list[str]:
    arguments = ["clean", root, "--older-than", repr(older_than)]
    if everything:
        arguments.append("--all")
    return arguments


def _print_entries(title: str, entries: list[dict[str, str]], reasons: bool, err: bool) -> None:
    click.echo(title, err=err)
    for entry in entries:
        why = f"  ({entry['reason']})" if reasons else ""
        click.echo(f"  {entry['kind']:<10} {entry['path']}{why}", err=err)


def _clean_host(
    host: str,
    older_than: float,
    everything: bool,
    yes: bool,
    json_flag: bool,
    session: SshSession,
    _facts: Probe,
    provisioned: Provisioned,
) -> dict[str, Any]:
    arguments = _clean_arguments(provisioned.root, older_than, everything)
    listing = _agent_document(
        session, provisioned, "listing the entries", *arguments, "--list", timeout=_CLEAN_TIMEOUT
    )
    removable = listing.get("removable") or []
    if not json_flag:
        if removable:
            _print_entries(
                f"Xeda can remove {len(removable)} entries on {host}:", removable, False, True
            )
        if listing.get("kept"):
            _print_entries("Xeda keeps:", listing["kept"], True, True)
    if not removable:
        if not json_flag:
            click.echo(f"Nothing to remove on {host}.")
        return {"success": True, "host": host, "removed": [], "kept": listing.get("kept") or []}
    if not (yes or json_flag):
        try:
            if not click.confirm("Remove them?", default=False, err=True):
                click.echo("Nothing was removed.")
                return {"success": True, "host": host, "removed": [], "kept": listing["kept"]}
        except click.Abort:
            _fail(
                "Aborted",
                "No answer. Nothing was removed. Pass --yes to remove without asking.",
                False,
            )
    # Remove what the user saw and confirmed. The agent checks each path again.
    only = [word for entry in removable for word in ("--only", entry["path"])]
    result = _agent_document(
        session, provisioned, "removing the entries", *arguments, *only, timeout=_CLEAN_TIMEOUT
    )
    removed = result.get("removed") or []
    # The listing kept what the removal did not look at; the removal kept what changed since.
    kept = result.get("kept") or []
    seen = {entry["path"] for entry in kept}
    kept += [entry for entry in listing.get("kept") or [] if entry["path"] not in seen]
    if not json_flag:
        _print_entries(f"Removed {len(removed)} entries on {host}:", removed, False, False)
        if kept:
            _print_entries("Kept:", kept, True, False)
    return {"success": True, "host": host, "removed": removed, "kept": kept}


@click.argument("host")
@click.option(
    "--older-than",
    "older_than",
    type=click.FloatRange(min=0),
    default=30.0,
    show_default=True,
    metavar="DAYS",
    help="Remove only what nobody used for this many days.",
)
@click.option(
    "--all",
    "everything",
    is_flag=True,
    default=False,
    help="Remove everything that nobody uses now, however recently it was used.",
)
@click.option("--yes", "yes", is_flag=True, default=False, help="Do not ask before removing.")
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Write one JSON document to stdout."
)
@click.pass_context
def remote_clean(
    ctx: click.Context, host: str, older_than: float, everything: bool, yes: bool, json_flag: bool
) -> None:
    """Remove what nobody uses on HOST: environments, code copies and workspaces.

    The command lists the entries first and asks before it removes them. With `--yes` or `--json`
    it does not ask. It never removes a workspace whose lock a launch holds or that has a running
    launch, nor an environment or code copy that a process uses. It prints what it removed and
    what it kept, and why.
    """
    _set_up(ctx, json_flag)
    document = _run(
        json_flag,
        host,
        lambda session, facts, provisioned: _clean_host(
            host, older_than, everything, yes, json_flag, session, facts, provisioned
        ),
    )
    if json_flag:
        from ..cli_utils import emit_structured

        emit_structured(document, "json")


# ---------------------------------------------------------------------------------------------
# xeda remote attach and xeda remote cancel
# ---------------------------------------------------------------------------------------------


def _run_root_option(func: Any) -> Any:
    return click.option(
        "--run-root",
        type=click.Path(file_okay=False, dir_okay=True, resolve_path=True, path_type=Path),
        envvar="XEDA_RUN_ROOT",
        default="xeda_run",
        show_default=True,
        show_envvar=True,
        help="The run root of the `xeda run` that started the launch. With the host, it names "
        "the workspace on the host and the mirror here.",
    )(func)


@click.argument("host")
@click.argument("launch_id", required=False)
@_run_root_option
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Write one JSON document to stdout."
)
@click.pass_context
def remote_attach(
    ctx: click.Context, host: str, launch_id: str | None, run_root: Path, json_flag: bool
) -> None:
    """Follow a launch that keeps running on HOST from its start and fetch its result.

    LAUNCH_ID is the id that `xeda run --remote HOST --keep-running` printed. Without it, the
    command takes the launch that started last among those that run in the workspace of this
    machine, run root and host, else the one that started last, and says which. It shows the log
    from the start, waits for the end, and fetches the result into the mirror as `xeda run
    --remote` does. It delivers nothing: run the original command again to deliver (it reuses the
    finished run). Ctrl-C stops following and leaves the launch running.
    """
    _set_up(ctx, json_flag)
    from ..cli_utils import emit_structured
    from .attach import attach_launch

    outcome = attach_launch(
        host, launch_id, run_root=run_root, profile=_profile_of(host, json_flag)
    )
    if outcome.error is not None:
        log.critical("%s", outcome.document["error"]["message"])
    elif not outcome.success:
        log.critical("The launch on '%s' did not succeed.", host)
    if json_flag:
        emit_structured(outcome.document, "json")
    sys.exit(0 if outcome.success else 1)


@click.argument("host")
@click.argument("launch_id", required=False)
@_run_root_option
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Write one JSON document to stdout."
)
@click.pass_context
def remote_cancel(
    ctx: click.Context, host: str, launch_id: str | None, run_root: Path, json_flag: bool
) -> None:
    """Cancel a running launch on HOST.

    LAUNCH_ID is the id that `xeda run --remote HOST --keep-running` printed. Without it, the
    command takes the launch that started last among those that run in the workspace of this
    machine, run root and host. It stops the launch as a Ctrl-C of a normal remote run does: SIGINT, then SIGTERM and SIGKILL
    after the grace periods, to the launch and to the processes it started.
    """
    _set_up(ctx, json_flag)
    from .attach import cancel_launch

    profile = _profile_of(host, json_flag)
    try:
        document = cancel_launch(host, launch_id, run_root=run_root, profile=profile)
    except KeyboardInterrupt:
        _fail(
            "RemoteCanceled",
            f"Canceled by Ctrl-C. The cancel on {host} may be unfinished: run it again.",
            json_flag,
        )
    except Exception as error:  # pylint: disable=broad-except
        log.debug("The remote command failed", exc_info=True)
        _fail(type(error).__name__, str(error), json_flag)
    if json_flag:
        from ..cli_utils import emit_structured

        emit_structured(document, "json")
    elif document["success"]:
        click.echo(f"Canceled the launch {document['launch']} on {host}.")
    else:
        click.echo(
            f"Canceled the launch {document['launch']} on {host}, but {document['survivors']} "
            "process(es) of it did not stop after SIGKILL and may still run there.",
            err=True,
        )
    sys.exit(0 if document["success"] else 1)


# ---------------------------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------------------------


def add_remote_commands(
    group: click.Group, command_class: type[click.Command], context_settings: dict[str, Any]
) -> None:
    """Add the commands of `xeda remote` to `group`. `command_class` is the class of every command
    of Xeda that reads no variable of the environment unless it declares it."""
    group.command(
        "check",
        cls=command_class,
        context_settings=context_settings,
        short_help="Check a remote host: Python, environment, disk space and tools.",
    )(remote_check)
    group.command(
        "clean",
        cls=command_class,
        context_settings=context_settings,
        short_help="Remove the environments and workspaces that nobody uses on a remote host.",
    )(remote_clean)
    group.command(
        "attach",
        cls=command_class,
        context_settings=context_settings,
        short_help="Follow a launch that keeps running on a remote host and fetch its result.",
    )(remote_attach)
    group.command(
        "cancel",
        cls=command_class,
        context_settings=context_settings,
        short_help="Cancel a running launch on a remote host.",
    )(remote_cancel)
