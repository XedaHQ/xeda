"""Run a Linux host with `sshd` in a Docker container, for the end-to-end tests of remote runs.

    python tools/remote_test_sshd.py start
    python tools/remote_test_sshd.py status
    python tools/remote_test_sshd.py stop

`start` builds a small image if it is not there (Debian slim with `openssh-server`, `curl`,
`bash`, `ca-certificates`, `procps` and `ghdl`) for the architecture of this machine. It starts one
container in the background, with `sshd` on a free port of `127.0.0.1`. It makes a new directory
with a new ed25519 key pair, a `known_hosts` file and an `ssh_config` that names the host
`xeda-test-sshd`. Then it prints the directory and the two variables that the tests read:

    export XEDA_TESTS_REMOTE_HOST=xeda-test-sshd
    export XEDA_TESTS_REMOTE_SSH_CONFIG=<directory>/ssh_config

The container user is `xeda`, with the home directory `/home/xeda`. The `ssh_config` accepts the
new host key (`StrictHostKeyChecking accept-new`) and keeps it in the directory above, so it never
touches `~/.ssh`. Use it only with `ssh -F`. `stop` removes the container and the directory. The
script uses only the standard library.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE = "xeda-test-sshd:1"
CONTAINER = "xeda-test-sshd"
HOST = "xeda-test-sshd"
USER = "xeda"
#: The label of the container that holds the directory with the key pair.
LABEL = "xeda.test-sshd.directory"
DIRECTORY_PREFIX = "xeda-test-sshd-"
#: Seconds that `sshd` may take to accept a connection.
READY_TIMEOUT = 90

DOCKERFILE = f"""\
FROM debian:trixie-slim
RUN apt-get update \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        openssh-server curl bash ca-certificates procps ghdl \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /run/sshd \\
    && ssh-keygen -A \\
    && useradd --create-home --shell /bin/bash {USER} \\
    && usermod --password '*' {USER}
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
"""

ENTRYPOINT = f"""\
set -eu
home=/home/{USER}
install -d -m 700 -o {USER} -g {USER} "$home/.ssh"
printf '%s\\n' "$AUTHORIZED_KEY" > "$home/.ssh/authorized_keys"
chown {USER}:{USER} "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
exec /usr/sbin/sshd -D -e
"""


class ToolError(Exception):
    """A failure that the script reports as one line."""


def run(*command: str, check: bool = True, timeout: float | None = None) -> str:
    """Run a command and return its standard output."""
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(f"`{command[0]}` was not found on PATH") from None
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        raise ToolError(f"`{' '.join(command)}` failed ({done.returncode}):\n{detail}")
    return done.stdout


def container_directory() -> Path | None:
    """The directory of the running or stopped container, or `None` if there is no container."""
    done = subprocess.run(
        ["docker", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', CONTAINER],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def container_state() -> str | None:
    """The state of the container (`running`, `exited`, ...), or `None` if there is none."""
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else None


def build_image() -> None:
    """Build the image if it does not exist."""
    if (
        subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
        ).returncode
        == 0
    ):
        return
    print(f"Building the image {IMAGE} (the first time takes a few minutes)...", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="xeda-test-sshd-build-") as context:
        (Path(context) / "Dockerfile").write_text(DOCKERFILE)
        (Path(context) / "entrypoint.sh").write_text(ENTRYPOINT)
        run("docker", "build", "--quiet", "--tag", IMAGE, context, timeout=1800)


def make_directory() -> tuple[Path, str]:
    """Make the directory with a new key pair. Return it and the public key."""
    directory = Path(tempfile.mkdtemp(prefix=DIRECTORY_PREFIX))
    directory.chmod(0o700)
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", HOST, "-f", str(directory / "id"))
    return directory, (directory / "id.pub").read_text().strip()


def write_config(directory: Path, port: int) -> Path:
    config = directory / "ssh_config"
    config.write_text(
        f"Host {HOST}\n"
        "    HostName 127.0.0.1\n"
        f"    Port {port}\n"
        f"    User {USER}\n"
        f"    IdentityFile {directory}/id\n"
        "    IdentitiesOnly yes\n"
        "    PasswordAuthentication no\n"
        "    StrictHostKeyChecking accept-new\n"
        f"    UserKnownHostsFile {directory}/known_hosts\n"
        "    LogLevel ERROR\n"
    )
    return config


def wait_until_ready(config: Path) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        done = subprocess.run(
            [
                "ssh",
                "-F",
                str(config),
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=3",
                HOST,
                "true",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise ToolError(
                f"sshd did not accept a connection in {READY_TIMEOUT} s:\n{done.stderr.strip()}"
            )
        time.sleep(1)


def published_port() -> int:
    text = run("docker", "port", CONTAINER, "22/tcp").splitlines()[0]
    return int(text.rsplit(":", 1)[1])


def print_variables(directory: Path) -> None:
    print(f"directory: {directory}")
    print(f"export XEDA_TESTS_REMOTE_HOST={HOST}")
    print(f"export XEDA_TESTS_REMOTE_SSH_CONFIG={directory}/ssh_config")


def start() -> None:
    if container_state() is not None:
        raise ToolError(f"the container {CONTAINER} exists; run `stop` first")
    build_image()
    directory, public_key = make_directory()
    try:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            CONTAINER,
            "--label",
            f"{LABEL}={directory}",
            "--publish",
            "127.0.0.1::22",
            "--env",
            f"AUTHORIZED_KEY={public_key}",
            IMAGE,
        )
        config = write_config(directory, published_port())
        wait_until_ready(config)
    except BaseException:
        stop()
        shutil.rmtree(directory, ignore_errors=True)
        raise
    print_variables(directory)


def stop() -> None:
    directory = container_directory()
    subprocess.run(
        ["docker", "rm", "--force", "--volumes", CONTAINER],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if directory is not None and directory.name.startswith(DIRECTORY_PREFIX):
        shutil.rmtree(directory, ignore_errors=True)
    print(f"Stopped {CONTAINER}.")


def status() -> int:
    state = container_state()
    directory = container_directory()
    print(f"{CONTAINER}: {state or 'not found'}")
    if state == "running" and directory is not None:
        print_variables(directory)
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("start", "stop", "status"))
    command = parser.parse_args(argv).command
    try:
        if command == "start":
            start()
        elif command == "stop":
            stop()
        else:
            return status()
    except ToolError as error:
        print(f"remote_test_sshd: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
