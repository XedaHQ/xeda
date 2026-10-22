"""`xeda run --remote` against a real Linux host over real OpenSSH, uv and PyPI.

An opt-in layer: the tests skip unless `XEDA_TESTS_REMOTE_HOST` names a host that `ssh` reaches
without a prompt. `XEDA_TESTS_REMOTE_SSH_CONFIG` is an optional `ssh_config` file (used as
`ssh -F`). `python tools/remote_test_sshd.py start` starts such a host in a Docker container and
prints both variables; `stop` removes it.

The host needs a network (the tests install uv, Python and the dependencies of xeda there) and
`ghdl` for the tests that run a flow. Every test works in a remote root of its own below the home
directory of the remote user (`~/xeda-e2e-<token>`) and removes it at the end. A test that needs
a tool which the host lacks skips and names the tool.

The tests that run flows use the VHDL simulation with GHDL. The tests of the cancel paths run a
testbench that never ends, and so they see a real process on the remote host go away.
"""

import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

import pytest
import yaml
from click.testing import CliRunner

from xeda.cli import cli
from xeda.remote.constants import UV_VERSION
from xeda.remote.errors import RemoteCanceled
from xeda.remote.profile import load_host_profile
from xeda.remote.provision import env_file_text
from xeda.remote.ssh import SshSession

from .remote_fixtures import remote_request
from .test_proc_utils import DEFAULT_SIGINT
from .test_remote_client import TIMEOUT, Program

HOST = os.environ.get("XEDA_TESTS_REMOTE_HOST", "")
CONFIG = os.environ.get("XEDA_TESTS_REMOTE_SSH_CONFIG", "")

pytestmark = [
    pytest.mark.real_tool,
    pytest.mark.skipif(os.name != "posix", reason="POSIX"),
    pytest.mark.skipif(
        not HOST,
        reason="set XEDA_TESTS_REMOTE_HOST to a Linux host to run the remote end-to-end tests",
    ),
]

ADDER = Path(__file__).parent.parent / "examples" / "vhdl" / "pipeline" / "pipelined_adder.yaml"

#: The prefix of the remote roots of these tests, below the home directory of the remote user.
ROOT_PREFIX = "xeda-e2e-"

#: The options of `run_remote` and of the `ssh` of the helpers below.
SSH_OPTIONS = ("-F", CONFIG) if CONFIG else ()


# --- The remote host ----------------------------------------------------------------------------


def remote(command: str, timeout: float = 120) -> "subprocess.CompletedProcess[str]":
    """Run `command` on the host in a connection of its own (`bash -lc`, as a launch starts)."""
    return subprocess.run(
        ["ssh", *SSH_OPTIONS, "-o", "BatchMode=yes", HOST, "bash", "-lc", shlex.quote(command)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def remove_root(token: str) -> None:
    remote(f'rm -rf "$HOME/{ROOT_PREFIX}{token}"')


@pytest.fixture(scope="module")
def host_reachable() -> None:
    done = subprocess.run(
        ["ssh", *SSH_OPTIONS, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, "true"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"cannot connect to XEDA_TESTS_REMOTE_HOST={HOST}: {done.stderr.strip()}")


@pytest.fixture(scope="module")
def shared_token(host_reachable) -> Iterator[str]:
    """The token of the remote root that the tests share: the first test that needs the
    environment makes it, and the others find it."""
    token = secrets.token_hex(4)
    yield token
    remove_root(token)


@pytest.fixture
def own_token(host_reachable) -> Iterator[str]:
    """The token of a remote root that only one test uses, so that the test sees a first
    provisioning."""
    token = secrets.token_hex(4)
    yield token
    remove_root(token)


def use_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Make the profile of the host (the entry the user has, if any) use the remote root of
    `token`. The user-level directories of this machine are below `tmp_path`."""
    entry = load_host_profile(HOST).model_dump(mode="json", exclude_defaults=True)
    entry["root"] = f"~/{ROOT_PREFIX}{token}"
    config = tmp_path / "config"
    (config / "xeda").mkdir(parents=True)
    (config / "xeda" / "remotes.yaml").write_text(yaml.safe_dump({"hosts": {HOST: entry}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def ssh_with_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """For a program that has no `ssh_options` (the command line, in this process or another):
    put an `ssh` first on `PATH` that adds `-F <config>`."""
    if not CONFIG:
        return
    real = shutil.which("ssh")
    assert real, "ssh is not installed"
    directory = tmp_path / "ssh-bin"
    directory.mkdir()
    wrapper = directory / "ssh"
    wrapper.write_text(f'#!/bin/sh\nexec "{real}" -F "{CONFIG}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def require_ghdl() -> None:
    """Skip unless `ghdl` runs on the host as a launch would start it: in the login shell, with
    the `env` table and after the `setup` lines of the host profile."""
    profile = load_host_profile(HOST)
    setup = "".join(f"{line}\n" for line in profile.setup)
    found = remote(f"{env_file_text(profile.env)}{setup}ghdl --version")
    if found.returncode != 0:
        why = (found.stderr.strip().splitlines() or [f"exit status {found.returncode}"])[-1]
        pytest.skip(f"ghdl does not run on {HOST} after the setup lines of its profile: {why}")


def run(tmp_path: Path, design: Path, chain: str = "ghdl_sim", **overrides: Any):
    return remote_request(
        tmp_path, design, chain, destination_text=HOST, ssh_options=SSH_OPTIONS, **overrides
    )


def succeeded(outcome) -> dict[str, Any]:
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    return outcome.document


def log_messages(caplog) -> List[str]:
    return [record.getMessage() for record in caplog.records]


# --- A design that never ends -------------------------------------------------------------------


def forever_design(tmp_path: Path, token: str) -> Path:
    """A VHDL design whose testbench runs until the simulator is stopped. The testbench reports
    `READY-<token>`, and `<token>` is also in the name of its entity, so that a process of its
    simulator is easy to find."""
    root = tmp_path / "forever"
    root.mkdir()
    (root / "dut.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        f"entity dut_{token} is port (clk : in std_logic; q : out std_logic); end entity;\n"
        f"architecture rtl of dut_{token} is begin q <= clk; end architecture;\n"
    )
    (root / "tb.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        f"entity tb_{token} is end entity;\n"
        f"architecture tb of tb_{token} is\n"
        "  signal clk, q : std_logic := '0';\n"
        "begin\n"
        f"  uut : entity work.dut_{token} port map (clk => clk, q => q);\n"
        "  clk <= not clk after 5 ns;\n"
        f'  process begin report "READY-{token}"; wait; end process;\n'
        "end architecture;\n"
    )
    design = root / "forever.yaml"
    design.write_text(
        f"name: forever_{token}\n"
        "language.vhdl.standard: 2008\n"
        f"rtl: {{sources: [dut.vhd], top: dut_{token}, clock.port: clk}}\n"
        f"tb: {{sources: [tb.vhd], top: tb_{token}, uut: uut}}\n"
    )
    return design


def simulator_pids(token: str) -> List[int]:
    """The processes of the simulator of the design on the host."""
    done = remote(f'pgrep -f "[t]b_{token}"')
    return [int(word) for word in done.stdout.split()]


def wait_for_simulator(token: str, fail: Optional[Callable[[], None]] = None) -> List[int]:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if pids := simulator_pids(token):
            return pids
        if fail is not None:
            fail()
        time.sleep(0.5)
    pytest.fail(f"the simulator did not start on {HOST} in {TIMEOUT} s")


def assert_simulator_gone(token: str, pids: List[int]) -> None:
    """The simulator ends. Kill what is left, so that a failure does not leave it running."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not simulator_pids(token):
            return
        time.sleep(0.5)
    remote("kill -9 " + " ".join(str(pid) for pid in pids))
    pytest.fail(f"the simulator {pids} still runs on {HOST}")


def launch_states(token: str) -> dict[str, str]:
    """The state that each launch on the host recorded, by launch id."""
    done = remote(
        f'grep -H "\\"state\\"" "$HOME/{ROOT_PREFIX}{token}"/v1/ws/*/launches/*/state.json'
    )
    states = {}
    for line in done.stdout.splitlines():
        path, _, text = line.partition(":")
        states[path.split("/")[-2]] = json.loads("{" + text.strip().rstrip(",") + "}")["state"]
    return states


def last_launch_state(token: str) -> str:
    """The recorded state of the newest launch (the launch ids start with the time). The agent
    records the end of a launch after it has stopped it, so this waits while the state is
    `running`."""
    deadline = time.monotonic() + 60
    while True:
        states = launch_states(token)
        state = states[max(states)]
        if state != "running" or time.monotonic() > deadline:
            return state
        time.sleep(0.5)


# --- Provisioning ---------------------------------------------------------------------------------


def test_first_run_installs_uv_and_python_and_the_second_builds_nothing(
    tmp_path, monkeypatch, caplog, own_token
):
    """The remote root is new, so uv, Python and the environment come from the real release of uv
    and PyPI. A dry run needs no tool on the host."""
    use_root(tmp_path, monkeypatch, own_token)
    caplog.set_level(logging.INFO, logger="xeda.remote.client")
    first = succeeded(run(tmp_path, ADDER, dry_run=True))
    assert first["remote"] == HOST
    steps = log_messages(caplog)
    assert f"Installing uv {UV_VERSION}" in steps
    assert any(step.startswith("Installing Python 3.") for step in steps)
    assert "Creating the environment" in steps
    assert any(step.startswith("Uploading xeda") for step in steps)
    version = remote(f'"$HOME/{ROOT_PREFIX}{own_token}"/v1/uv/{UV_VERSION}-*/uv --version')
    assert UV_VERSION in version.stdout or UV_VERSION in version.stderr, version

    caplog.clear()
    succeeded(run(tmp_path, ADDER, dry_run=True))
    steps = log_messages(caplog)
    assert not [s for s in steps if s.startswith(("Installing", "Creating", "Uploading"))], steps


# --- A real flow ----------------------------------------------------------------------------------


def test_a_simulation_runs_on_the_host_and_a_second_run_reuses_it(
    tmp_path, monkeypatch, shared_token
):
    require_ghdl()
    use_root(tmp_path, monkeypatch, shared_token)
    first = succeeded(run(tmp_path, ADDER))
    assert [node["state"] for node in first["nodes"]] == ["ran"]
    assert first["results"]["sim.ended_by"] == "finish"
    mirror = Path(first["run_path"])
    assert mirror.is_relative_to(tmp_path / "local" / "xeda_run" / f"@{HOST}")
    for name in ("results.json", "settings.json"):
        assert (mirror / name).is_file(), name
    assert not list(mirror.parent.parent.rglob("trace.json"))
    saved = json.loads((mirror / "results.json").read_text())
    assert saved["outcome"] == "passed"
    assert ROOT_PREFIX not in json.dumps(saved), "the records name no path of the host"

    second = succeeded(run(tmp_path, ADDER))
    assert [node["state"] for node in second["nodes"]] == ["fresh"]
    assert second["results"]["sim.time"] == first["results"]["sim.time"]


# --- Cancel ---------------------------------------------------------------------------------------


def test_ctrl_c_through_openssh_stops_the_simulator_on_the_host(
    tmp_path, monkeypatch, shared_token, ssh_with_config
):
    require_ghdl()
    use_root(tmp_path, monkeypatch, shared_token)
    token = secrets.token_hex(4)
    design = forever_design(tmp_path, token)
    script = (
        f"{DEFAULT_SIGINT}from xeda.cli import cli\n"
        f"cli(['run', 'ghdl_sim', '--design', {str(design)!r}, '--remote', {HOST!r}, "
        f"'--run-root', {str(tmp_path / 'local' / 'xeda_run')!r}, '--json'], "
        "standalone_mode=True)\n"
    )
    program = Program(tmp_path, script)
    pids: List[int] = []
    try:
        try:
            program.wait_for(f"READY-{token}")
            pids = wait_for_simulator(token)
            program.interrupt()  # SIGINT for the process group, as Ctrl-C does at a terminal
            status = program.finish()
            # Check the simulator before anything else: the forced kill below must not hide a
            # simulator that cancel left running.
            assert_simulator_gone(token, pids)
        finally:
            program.finish()
    finally:
        # Forced cleanup, also when a check above fails.
        remote("kill -9 " + " ".join(str(pid) for pid in pids)) if pids else None
    assert status == 1, program.stderr
    document = json.loads(program.out.read_text())
    assert document["success"] is False
    assert document["error"]["type"] == "RemoteCanceled"
    # The cancel message went through the connection: the client did not lose it to the Ctrl-C.
    assert "canceled by Ctrl-C" in document["error"]["message"], document["error"]
    assert document["remote"] == HOST
    assert "Canceling the launch" in program.stderr
    assert last_launch_state(shared_token) == "canceled", launch_states(shared_token)


def test_a_killed_master_connection_cancels_the_launch(tmp_path, monkeypatch, shared_token):
    require_ghdl()
    use_root(tmp_path, monkeypatch, shared_token)
    token = secrets.token_hex(4)
    design = forever_design(tmp_path, token)
    sessions: List[SshSession] = []
    real_enter = SshSession.__enter__

    def enter(self):
        sessions.append(self)
        return real_enter(self)

    monkeypatch.setattr(SshSession, "__enter__", enter)
    pids: List[int] = []
    problems: List[BaseException] = []

    def kill_master() -> None:
        try:
            pids.extend(wait_for_simulator(token))
            done = subprocess.run(
                ["ssh", "-S", sessions[0]._control, "-O", "check", *SSH_OPTIONS, HOST],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            # OpenSSH says `Master running (pid=1234)` on standard error.
            master = int(done.stderr.split("pid=")[1].split(")")[0])
            os.kill(master, signal.SIGKILL)
        except BaseException as error:  # pylint: disable=broad-except
            problems.append(error)

    thread = threading.Thread(target=kill_master, daemon=True)
    thread.start()
    outcome = run(tmp_path, design)
    thread.join()
    assert not problems, problems
    try:
        assert isinstance(outcome.error, RemoteCanceled), outcome.document
        assert "connection" in outcome.document["error"]["message"]
        assert_simulator_gone(token, pids)
    finally:
        if pids:
            remote("kill -9 " + " ".join(str(pid) for pid in pids) + " 2>/dev/null")
    # no orphan: the host records that the launch was canceled, and no simulator of it is left
    assert last_launch_state(shared_token) == "canceled", launch_states(shared_token)
    assert not simulator_pids(token)


# --- The commands ---------------------------------------------------------------------------------


def test_remote_check_and_clean_work_on_the_host(tmp_path, monkeypatch, own_token, ssh_with_config):
    use_root(tmp_path, monkeypatch, own_token)
    layout = f'"$HOME/{ROOT_PREFIX}{own_token}"/v1'
    checked = CliRunner().invoke(cli, ["remote", "check", HOST, "--json"])
    assert checked.exit_code == 0, checked.output
    document = json.loads(checked.stdout)
    assert document["success"] is True and document["host"] == HOST
    assert document["platform"]["system"] == "Linux"
    assert document["python"]["version"].startswith("3.")
    assert isinstance(document["warnings"], list)
    assert remote(f"test -d {layout}/env").returncode == 0, "the check makes the environment"
    (current,) = remote(f"ls {layout}/env").stdout.split()

    # A stale environment and code copy (60 days old, each with the lock file that provisioning
    # makes), and a workspace that was used just now.
    made = remote(
        f"mkdir -p {layout}/env/stale-env {layout}/code/stale-code {layout}/ws/stale-ws"
        f" && touch {layout}/ws/stale-ws/lock {layout}/env/stale-env/.xeda-lock"
        f" {layout}/code/stale-code/.xeda-lock"
        f" && touch -d @$(($(date +%s) - 5184000)) {layout}/env/stale-env {layout}/code/stale-code"
    )
    assert made.returncode == 0, made.stderr

    # The default age is 30 days. The environment of the check is in use, and the workspace is
    # too recent.
    by_age = CliRunner().invoke(cli, ["remote", "clean", HOST, "--yes", "--json"])
    assert by_age.exit_code == 0, by_age.output
    result = json.loads(by_age.stdout)
    assert result["success"] is True
    assert {(e["kind"], Path(e["path"]).name) for e in result["removed"]} == {
        ("env", "stale-env"),
        ("code", "stale-code"),
    }
    kept = {(e["kind"], Path(e["path"]).name): e["reason"] for e in result["kept"]}
    assert kept[("env", current)] == "in use"
    assert kept[("workspace", "stale-ws")] == "recently used"
    assert remote(f"test ! -e {layout}/env/stale-env").returncode == 0

    everything = CliRunner().invoke(cli, ["remote", "clean", HOST, "--all", "--yes", "--json"])
    assert everything.exit_code == 0, everything.output
    result = json.loads(everything.stdout)
    assert [(e["kind"], Path(e["path"]).name) for e in result["removed"]] == [
        ("workspace", "stale-ws")
    ]
    assert remote(f"test -d {layout}/env/{current}").returncode == 0
