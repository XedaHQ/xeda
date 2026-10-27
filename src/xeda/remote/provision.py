"""Provision a remote host: uv, Python, a dependency environment and the xeda code snapshot.

The work is done by the POSIX shell script `bootstrap.sh` on the remote host. This module sends
the script, runs its commands over an `SshSession`, and reads its answers.

The installer is the first command. Its standard input is a short shell script that this module
generates. It finds the remote root `R`, writes the bootstrap script to `R/v1/bin/boot-<hash>.sh`
and the environment file to `R/v1/bin/env-<hash>.sh`, and prints facts about the host. Every later
command is `sh <bootstrap script> <command> <environment hash> ...`, so the standard input of
these commands is free for data.

The environment file holds the `env` table of the host profile as `export` lines. The installer
and every command of the bootstrap script read it first, so every remote command gets the
variables of the profile (a proxy, a license server, a root).

The remote host prints these lines, and the client ignores every other line of its output, because
a login shell can print text of its own:

    XEDA-PROBE <key>=<value>
    XEDA-STEP <text>
    XEDA-STATUS env=<present|missing> code=<present|missing>
    XEDA-ERROR <code> <text>
"""

import contextvars
import hashlib
import io
import logging
import re
import shlex
import threading
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Callable, Deque, Dict, List, Mapping, Optional

from .constants import UV_VERSION
from .environment import (
    CodeSnapshot,
    DependencySpec,
    RemotePlatform,
    dependency_key,
    python_choice,
    uv_release,
    write_code_tar,
)
from .errors import (
    RemoteConfigError,
    RemoteConnectionError,
    RemoteProtocolError,
    RemoteProvisionError,
)
from .profile import HostProfile, env_name_problem
from .ssh import SshSession, sh_command

log = logging.getLogger(__name__)

#: The codes of `RemoteProvisionError`. The remote script sends the first group. The client
#: raises `unsupported-platform` itself, when no uv release is pinned for the host.
ERROR_CODES = (
    "not-linux",
    "no-downloader",
    "download-failed",
    "checksum-mismatch",
    "python-failed",
    "env-failed",
    "lock-timeout",
    "no-space",
    "bad-request",
    "no-sha256",
    "unsupported-platform",
)

#: The facts that the installer prints.
_PROBE_KEYS = (
    "system",
    "machine",
    "libc",
    "libc_version",
    "home",
    "root",
    "boot",
    "disk_free_kb",
    "kill_user_processes",
)

#: Lines of the remote standard error that go into an error message.
_STDERR_LINES = 15

#: Free space in KB below which a failure of a remote step is reported as a full disk. The
#: bootstrap script uses the same value.
LOW_DISK_KB = 51200

_INSTALLER = r"""# shellcheck disable=SC2034
set -eu
@@ENV@@
say() {
    printf '\nXEDA-%s\n' "$*"
}
fail() {
    say "ERROR $1 $2"
    exit 1
}
XEDA_BOOT_SYSTEM=$(uname -s)
say "PROBE system=$XEDA_BOOT_SYSTEM"
if [ "$XEDA_BOOT_SYSTEM" != Linux ]; then
    fail not-linux "The remote system is '$XEDA_BOOT_SYSTEM'. A remote run needs Linux."
fi
say "PROBE machine=$(uname -m)"
XEDA_BOOT_HOME=${HOME:-}
if [ -z "$XEDA_BOOT_HOME" ] || [ ! -d "$XEDA_BOOT_HOME" ]; then
    fail env-failed "The home directory of the remote user is not set, or it does not exist."
fi
XEDA_BOOT_HOME=${XEDA_BOOT_HOME%/}
case ${2:-} in
    -)
        case ${XDG_CACHE_HOME:-} in
            /*) XEDA_BOOT_ROOT=${XDG_CACHE_HOME%/}/xeda ;;
            *) XEDA_BOOT_ROOT=$XEDA_BOOT_HOME/.cache/xeda ;;
        esac
        ;;
    home:*)
        XEDA_BOOT_REL=${2#home:}
        XEDA_BOOT_ROOT=$XEDA_BOOT_HOME${XEDA_BOOT_REL:+/$XEDA_BOOT_REL}
        ;;
    /*) XEDA_BOOT_ROOT=$2 ;;
    *) fail bad-request "Not a root: '${2:-}'." ;;
esac
XEDA_BOOT_ROOT=${XEDA_BOOT_ROOT%/}
XEDA_BOOT_V=$XEDA_BOOT_ROOT/v1
free_kb() {
    df -Pk "$1" 2>/dev/null </dev/null | {
        read -r XEDA_BOOT_JUNK || :
        read -r XEDA_BOOT_JUNK XEDA_BOOT_JUNK XEDA_BOOT_JUNK XEDA_BOOT_AVAILABLE XEDA_BOOT_JUNK || :
        printf '%s' "${XEDA_BOOT_AVAILABLE:-}"
    }
}
fail_write() {
    XEDA_BOOT_FREE=$(free_kb "$XEDA_BOOT_V")
    case $XEDA_BOOT_FREE in
        '' | *[!0-9]*) ;;
        *)
            if [ "$XEDA_BOOT_FREE" -lt @@LOW_DISK_KB@@ ]; then
                fail no-space "The disk of $XEDA_BOOT_V has only $XEDA_BOOT_FREE KB free. $1"
            fi
            ;;
    esac
    fail env-failed "$1"
}
mkdir -p "$XEDA_BOOT_V/bin" || fail_write "Cannot create $XEDA_BOOT_V/bin."
XEDA_BOOT_BOOT=$XEDA_BOOT_V/bin/@@BOOT_NAME@@
if [ ! -f "$XEDA_BOOT_BOOT" ]; then
    XEDA_BOOT_TMP=$XEDA_BOOT_V/bin/.boot.$$
    cat >"$XEDA_BOOT_TMP" <<'@@BOOT_DELIMITER@@' || { rm -f "$XEDA_BOOT_TMP"; fail_write "Cannot write $XEDA_BOOT_BOOT."; }
@@BOOT_TEXT@@
@@BOOT_DELIMITER@@
    mv "$XEDA_BOOT_TMP" "$XEDA_BOOT_BOOT" || fail_write "Cannot write $XEDA_BOOT_BOOT."
fi
XEDA_BOOT_ENVFILE=$XEDA_BOOT_V/bin/@@ENV_NAME@@
if [ ! -f "$XEDA_BOOT_ENVFILE" ]; then
    XEDA_BOOT_TMP=$XEDA_BOOT_V/bin/.env.$$
    # The file may hold a proxy password: only its owner reads it.
    (umask 077; cat >"$XEDA_BOOT_TMP" <<'@@ENV_DELIMITER@@') || { rm -f "$XEDA_BOOT_TMP"; fail_write "Cannot write $XEDA_BOOT_ENVFILE."; }
@@ENV_TEXT@@
@@ENV_DELIMITER@@
    chmod 600 "$XEDA_BOOT_TMP" || fail_write "Cannot write $XEDA_BOOT_ENVFILE."
    mv "$XEDA_BOOT_TMP" "$XEDA_BOOT_ENVFILE" || fail_write "Cannot write $XEDA_BOOT_ENVFILE."
fi
XEDA_BOOT_LIBC=unknown
XEDA_BOOT_LIBC_VERSION=unknown
XEDA_BOOT_LDD_OUTPUT=$(ldd --version 2>&1 </dev/null) || :
case $XEDA_BOOT_LDD_OUTPUT in
    *[Mm]usl*)
        XEDA_BOOT_LIBC=musl
        while IFS= read -r XEDA_BOOT_LINE; do
            case $XEDA_BOOT_LINE in
                'Version '*)
                    XEDA_BOOT_LIBC_VERSION=${XEDA_BOOT_LINE#Version }
                    break
                    ;;
            esac
        done <<EOF_LDD
$XEDA_BOOT_LDD_OUTPUT
EOF_LDD
        ;;
    *GLIBC* | *'GNU libc'*)
        XEDA_BOOT_LIBC=glibc
        IFS= read -r XEDA_BOOT_LINE <<EOF_LDD
$XEDA_BOOT_LDD_OUTPUT
EOF_LDD
        XEDA_BOOT_LIBC_VERSION=${XEDA_BOOT_LINE##* }
        ;;
    *)
        XEDA_BOOT_GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || XEDA_BOOT_GLIBC_VERSION=
        case $XEDA_BOOT_GLIBC_VERSION in
            'glibc '*)
                XEDA_BOOT_LIBC=glibc
                XEDA_BOOT_LIBC_VERSION=${XEDA_BOOT_GLIBC_VERSION#glibc }
                ;;
        esac
        ;;
esac
say "PROBE libc=$XEDA_BOOT_LIBC"
say "PROBE libc_version=$XEDA_BOOT_LIBC_VERSION"
say "PROBE home=$XEDA_BOOT_HOME"
say "PROBE root=$XEDA_BOOT_ROOT"
say "PROBE boot=$XEDA_BOOT_BOOT"
XEDA_BOOT_FREE=$(free_kb "$XEDA_BOOT_V")
case $XEDA_BOOT_FREE in
    '' | *[!0-9]*) XEDA_BOOT_FREE=-1 ;;
esac
say "PROBE disk_free_kb=$XEDA_BOOT_FREE"
XEDA_BOOT_KILL_USER_PROCESSES=unknown
if command -v loginctl >/dev/null 2>&1; then
    XEDA_BOOT_LOGIN_OUTPUT=$(loginctl show-user --property=KillUserProcesses 2>/dev/null </dev/null) || XEDA_BOOT_LOGIN_OUTPUT=
    case $XEDA_BOOT_LOGIN_OUTPUT in
        KillUserProcesses=*)
            XEDA_BOOT_KILL_USER_PROCESSES=${XEDA_BOOT_LOGIN_OUTPUT#KillUserProcesses=}
            XEDA_BOOT_KILL_USER_PROCESSES=${XEDA_BOOT_KILL_USER_PROCESSES%%[!a-z]*}
            ;;
    esac
fi
say "PROBE kill_user_processes=${XEDA_BOOT_KILL_USER_PROCESSES:-unknown}"
"""


@dataclass(frozen=True)
class Probe:
    """The facts about a remote host, from the installer.

    `root` is the remote root `R`, and `boot` is the path of the bootstrap script on the host.
    `disk_free_kb` is the free space of the file system of the root in KB, or -1 if the host did
    not tell. `kill_user_processes` is the value of the `KillUserProcesses` setting of logind
    (`yes`, `no`), or `unknown`.
    """

    platform: RemotePlatform
    home: str
    root: str
    boot: str
    disk_free_kb: int
    kill_user_processes: str


@dataclass(frozen=True)
class Provisioned:
    """A host that has the environment and the code, and the names to start the agent with."""

    root: str
    boot: str
    env_hash: str
    deps_key: str
    code_key: str


# --- The script and the environment file ------------------------------------------------------


@lru_cache(maxsize=1)
def boot_script() -> str:
    """The text of the bootstrap script."""
    return resources.files(__package__).joinpath("bootstrap.sh").read_text(encoding="utf-8")


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def env_file_text(env: Mapping[str, str]) -> str:
    """The `export` lines for the `env` table of a host profile. Every value is quoted for the
    shell, so it arrives unchanged, whatever characters it has (spaces, `$`, quotes, backquotes,
    line breaks)."""
    lines = []
    for name, value in sorted(env.items()):
        problem = env_name_problem(name)
        if problem:
            raise RemoteConfigError(f"Invalid host profile: {problem}.")
        if "\0" in value:
            raise RemoteConfigError(f"The value of the environment variable {name} has a NUL byte.")
        lines.append(f"export {name}={shlex.quote(value)}\n")
    return "".join(lines)


def env_hash(env: Mapping[str, str]) -> str:
    """The name of the environment file for a profile: 12 hex characters of its text."""
    return _short_hash(env_file_text(env))


def _delimiter(*texts: str) -> str:
    """A here-document delimiter that is not a line of any of the texts."""
    lines = {line for text in texts for line in text.splitlines()}
    base = "XEDA_END_" + _short_hash("".join(texts))
    delimiter = base
    count = 0
    while delimiter in lines:
        count += 1
        delimiter = f"{base}_{count}"
    return delimiter


def _root_argument(root: Optional[str]) -> str:
    """The root of a host profile as one word of a command line: `-` for the default root, an
    absolute path, or `home:<path>` for a path in the home directory (`~/` is not a plain
    character)."""
    if root is None:
        return "-"
    if root.startswith("~/"):
        return "home:" + root[2:]
    return root


def installer_script(boot_text: str, env_text: str) -> str:
    """The shell script that the installer command reads from its standard input."""
    boot_name = f"boot-{_short_hash(boot_text)}.sh"
    env_name = f"env-{_short_hash(env_text)}.sh"
    values = {
        "ENV": env_text.rstrip("\n"),
        "LOW_DISK_KB": str(LOW_DISK_KB),
        "BOOT_NAME": boot_name,
        "BOOT_DELIMITER": _delimiter(boot_text),
        "BOOT_TEXT": boot_text.rstrip("\n"),
        "ENV_NAME": env_name,
        "ENV_DELIMITER": _delimiter(env_text),
        "ENV_TEXT": env_text.rstrip("\n"),
    }
    # One pass, so that a placeholder in a substituted text stays as it is.
    return re.sub(r"@@([A-Z_]+)@@", lambda m: values[m.group(1)], _INSTALLER)


# --- Running a command on the host ------------------------------------------------------------


@dataclass
class _Reply:
    probes: Dict[str, str] = field(default_factory=dict)
    status: Optional[Dict[str, str]] = None
    errors: List[str] = field(default_factory=list)
    stderr: str = ""
    returncode: int = 0


def _execute(
    session: SshSession,
    command: str,
    stdin: bytes,
    report: Optional[Callable[[str], None]],
) -> _Reply:
    """Run `command` on the host with `stdin` as its input. Pass each step to `report` as it
    starts. Only the lines of the protocol count; every other line goes to the DEBUG log."""
    process = session.popen(command)
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    tail: Deque[str] = deque(maxlen=_STDERR_LINES)

    def feed() -> None:
        try:
            process.stdin.write(stdin)  # type: ignore[union-attr]
            process.stdin.close()  # type: ignore[union-attr]
        except OSError:  # the client ended before it read everything
            pass

    def drain() -> None:
        for raw in process.stderr:  # type: ignore[union-attr]
            line = raw.decode("utf-8", "replace").rstrip()
            log.debug("remote stderr: %s", line)
            if line:
                tail.append(line)

    # A thread starts with an empty context: the records it logs carry the launch of the caller.
    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(feed,), daemon=True),
        threading.Thread(target=contextvars.copy_context().run, args=(drain,)),
    ]
    for thread in threads:
        thread.start()
    reply = _Reply()
    try:
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("XEDA-"):
                if line:
                    log.debug("remote: %s", line)
                continue
            kind, _, rest = line[len("XEDA-") :].partition(" ")
            if kind == "STEP":
                log.debug("remote step: %s", rest)
                if report is not None:
                    report(rest)
            elif kind == "PROBE":
                key, _, value = rest.partition("=")
                reply.probes[key] = value
            elif kind == "STATUS":
                reply.status = dict(item.partition("=")[::2] for item in rest.split())
            elif kind == "ERROR":
                reply.errors.append(rest)
            else:
                log.debug("remote: unknown protocol line %r", line)
        reply.returncode = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=10)
    reply.stderr = "\n".join(tail)
    return reply


def _raise_failure(session: SshSession, reply: _Reply, what: str) -> None:
    """Raise the error that a reply reports, if it reports one."""
    detail = f"\nOutput of the remote host:\n{reply.stderr}" if reply.stderr else ""
    if reply.errors:
        code, _, text = reply.errors[0].partition(" ")
        if code not in ERROR_CODES:
            raise RemoteProtocolError(
                f"The remote host sent the unknown error code '{code}' while {what}: {text}"
            )
        raise RemoteProvisionError(code, text + detail)
    if reply.returncode != 0:
        if reply.returncode == 255 and not session.check():
            raise RemoteConnectionError(
                f"the SSH connection to {session.destination.text} closed while {what}"
            )
        raise RemoteProtocolError(
            f"The remote host stopped with exit status {reply.returncode} while {what}, and sent"
            f" no error message.{detail}"
        )


def _call(
    session: SshSession,
    command: str,
    stdin: bytes,
    what: str,
    report: Optional[Callable[[str], None]] = None,
) -> _Reply:
    reply = _execute(session, command, stdin, report)
    _raise_failure(session, reply, what)
    return reply


# --- The steps --------------------------------------------------------------------------------


def probe(session: SshSession, profile: HostProfile) -> Probe:
    """Install the bootstrap script on the host and learn what the host is.

    This makes the remote root, the directory `R/v1/bin` and two files in it. A host that is not a
    Linux host gets nothing: this raises `RemoteProvisionError` with the code `not-linux`.
    """
    boot_text = boot_script()
    installer = installer_script(boot_text, env_file_text(profile.env))
    command = sh_command("install", _root_argument(profile.root))
    reply = _call(
        session,
        command,
        installer.encode("utf-8"),
        f"reading the facts of {session.destination.text}",
    )
    missing = [key for key in _PROBE_KEYS if key not in reply.probes]
    if missing:
        raise RemoteProtocolError(
            f"The installer on {session.destination.text} did not report: {', '.join(missing)}."
        )
    found = reply.probes
    try:
        free = int(found["disk_free_kb"])
    except ValueError:
        free = -1
    try:
        sh_command("agent", script_path=found["boot"])
    except ValueError:
        raise RemoteConfigError(
            f"The remote root {found['root']!r} has a character that xeda cannot put in a remote"
            " command line (a double quote, a backslash, '$', a backquote or '!'). Set `root` in"
            " the host profile to another path."
        ) from None
    return Probe(
        platform=RemotePlatform(
            system=found["system"],
            machine=found["machine"],
            libc=found["libc"],
            libc_version=found["libc_version"],
        ),
        home=found["home"],
        root=found["root"],
        boot=found["boot"],
        disk_free_kb=free,
        kill_user_processes=found["kill_user_processes"],
    )


def _status(
    session: SshSession, boot: str, env: str, deps_key: str, code_key: str
) -> Dict[str, str]:
    reply = _call(
        session,
        sh_command("status", env, deps_key, code_key, script_path=boot),
        b"",
        "checking the environment",
    )
    status = reply.status
    if (
        status is None
        or status.get("env") not in ("present", "missing")
        or status.get("code") not in ("present", "missing")
    ):
        raise RemoteProtocolError(
            f"The remote host did not report the state of the environment: {status!r}."
        )
    return status


def ensure_environment(
    session: SshSession,
    probe: Probe,
    profile: HostProfile,
    snapshot: CodeSnapshot,
    spec: DependencySpec,
    report: Callable[[str], None],
) -> Provisioned:
    """Make sure that the host has the dependency environment and the code snapshot.

    Each of the two is made only if it is missing. `report` gets the text of each step that the
    host starts ("Installing uv 0.12.23", "Creating the environment"). A repeated call reports
    nothing and changes nothing on the host.
    """
    python = python_choice(profile.python)
    uv_url, uv_sha256 = uv_release(probe.platform)
    deps_key = dependency_key(spec, python, probe.platform)
    env = env_hash(profile.env)
    state = _status(session, probe.boot, env, deps_key, snapshot.key)
    if state["env"] != "present":
        lines = [f"R {requirement}\n" for requirement in spec.requirements]
        lines += [f"C {constraint}\n" for constraint in spec.constraints]
        _call(
            session,
            sh_command(
                "provision",
                env,
                deps_key,
                python.path or python.request,
                UV_VERSION,
                uv_url,
                uv_sha256,
                script_path=probe.boot,
            ),
            "".join(lines).encode("utf-8"),
            "making the environment",
            report,
        )
    if state["code"] != "present":
        archive = io.BytesIO()
        write_code_tar(snapshot, archive)
        _call(
            session,
            sh_command("put-code", env, snapshot.key, script_path=probe.boot),
            archive.getvalue(),
            "uploading xeda",
            report,
        )
    return Provisioned(
        root=probe.root,
        boot=probe.boot,
        env_hash=env,
        deps_key=deps_key,
        code_key=snapshot.key,
    )


def agent_command(provisioned: Provisioned, *args: str) -> str:
    """The command line that starts the agent on the host, with the code snapshot first on the
    path of its Python. `args` are plain words (see `sh_command`)."""
    return sh_command(
        "agent",
        provisioned.env_hash,
        provisioned.deps_key,
        provisioned.code_key,
        *args,
        script_path=provisioned.boot,
    )
