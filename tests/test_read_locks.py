"""A consumer holds a shared lock on each dependency's run directory from the moment it completes
to the end of the consumer's launch (13-foundations S5, register L1), so another xeda process
cannot clean or rebuild a producer while its output is read: a launch takes its own run directory
exclusively. Within one process a lock already held is never waited for."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.run_lock import lock_file, run_dir_lock, run_dir_read_lock
from xeda.run_dir import RunDirectoryError

from .io_flows import _Maker, _Taker, _Wrapper

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no run-directory locks")


class _LegacyMaker(Flow):
    """A producer with no declared outputs, read through legacy dependency paths."""

    results_description = {}

    def run(self):
        self.run_directory.writable(self.run_path / "made.txt").write_text("made\n")


class _LegacyReader(Flow):
    """A legacy consumer reads its undeclared producer's file."""

    results_description = {}

    def init(self):
        self.add_dependency(_LegacyMaker, _LegacyMaker.Settings())

    def run(self):
        self.results["read"] = (self.completed_dependencies[0].run_path / "made.txt").read_text()


def _environment():
    root = Path(__file__).resolve().parents[1]
    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(root / "src"), str(root)]))


#: What another process can take of a lock right now, without waiting.
PROBE = textwrap.dedent("""
    import fcntl, sys
    mode = fcntl.LOCK_EX if sys.argv[2] == "exclusive" else fcntl.LOCK_SH
    with open(sys.argv[1], "a") as f:
        try:
            fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            print("blocked")
        else:
            print("free")
    """)


def _probe(run_path: Path, mode: str = "exclusive") -> str:
    """Whether another process could lock `run_path` in `mode` now: "free" or "blocked"."""
    found = subprocess.run(
        [sys.executable, "-c", PROBE, str(lock_file(run_path)), mode],
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip()


def _design(tmp_path: Path, monkeypatch) -> Design:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"})


def test_a_read_lock_keeps_another_process_from_rewriting_but_not_from_reading(tmp_path):
    run_path = tmp_path / "xeda_run" / "d" / "producer"
    with run_dir_read_lock(run_path):
        assert _probe(run_path, "exclusive") == "blocked"
        assert _probe(run_path, "shared") == "free"
    assert _probe(run_path, "exclusive") == "free"


def test_reentry_preserves_the_os_lock_mode(tmp_path):
    run_path = tmp_path / "xeda_run" / "d" / "producer"
    with run_dir_read_lock(run_path):
        with run_dir_read_lock(run_path):
            assert _probe(run_path, "shared") == "free"
        with pytest.raises(RunDirectoryError, match="shared"):
            with run_dir_lock(run_path):
                pytest.fail("a shared lease cannot authorize a writer")
        assert _probe(run_path, "exclusive") == "blocked"
    with run_dir_lock(run_path):
        with run_dir_lock(run_path), run_dir_read_lock(run_path):
            assert _probe(run_path, "shared") == "blocked"
    assert _probe(run_path) == "free", "every hold was released"


def test_a_consumer_holds_its_producer_for_its_whole_run(tmp_path, monkeypatch):
    seen: list[str] = []

    def run(self):
        seen.append(_probe(self.inputs.made.parent))
        self.results["read"] = self.inputs.made.read_text()

    monkeypatch.setattr(_Taker, "run", run)
    taker = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        _Taker, _design(tmp_path, monkeypatch), {}
    )
    assert taker.succeeded and seen == ["blocked"]
    (maker,) = taker.completed_dependencies
    assert _probe(maker.run_path) == "free", "released when the consumer's launch ended"


def test_a_flow_without_declarations_holds_its_dependencies_too(tmp_path, monkeypatch):
    seen: list[str] = []
    original = _Wrapper.run

    def run(self):
        seen.append(_probe(self.completed_dependencies[0].run_path))
        original(self)

    monkeypatch.setattr(_Wrapper, "run", run)
    wrapper = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        _Wrapper, _design(tmp_path, monkeypatch), {}
    )
    assert wrapper.succeeded and seen == ["blocked"]


@pytest.mark.parametrize(
    "consumer,untraced",
    [
        (consumer, untraced)
        for consumer in (_Taker, _Wrapper, _LegacyReader)
        for untraced in (False, True)
    ],
)
def test_a_changed_completed_run_is_refused_before_hand_over(
    tmp_path, monkeypatch, consumer, untraced
):
    """Even identical declared bytes do not vouch for other files read by legacy consumers."""
    from contextlib import contextmanager
    from xeda.flow import FlowDependencyFailure

    runner = DefaultRunner(
        tmp_path / "xeda_run", display_results=False, dump_results_json=not untraced
    )
    design = _design(tmp_path, monkeypatch)
    if untraced:
        monkeypatch.setattr(_Taker, "always_runs", lambda self: "untraced test producer")
        monkeypatch.setattr(_Maker, "always_runs", lambda self: "untraced test producer")
        monkeypatch.setattr(_LegacyMaker, "always_runs", lambda self: "untraced test producer")
    original = runner._producer_read_lease
    target = {_Taker: "__maker", _Wrapper: "__taker", _LegacyReader: "__legacy_maker"}[consumer]
    ran = []
    monkeypatch.setattr(consumer, "run", lambda self: ran.append(self.name))

    @contextmanager
    def gap(producer):
        if producer.name == target:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from pathlib import Path
from xeda.flow_runner.run_lock import run_dir_lock
p = Path(sys.argv[1])
with run_dir_lock(p):
    (p / "another-output.txt").write_text("changed generation")
""",
                    str(producer.run_path),
                ],
                check=True,
                env=_environment(),
                timeout=20,
            )
        with original(producer):
            yield producer

    monkeypatch.setattr(runner, "_producer_read_lease", gap)
    with pytest.raises(FlowDependencyFailure, match="changed.*read lease"):
        runner.launch_flow(consumer, design, {})
    assert consumer.name not in ran
    assert _probe(tmp_path / "xeda_run" / "d" / target) == "free"


def test_dependency_leases_release_when_consumer_raises(tmp_path, monkeypatch):
    def fail(self):
        assert _probe(self.completed_dependencies[0].run_path) == "blocked"
        raise RuntimeError("consumer failed")

    monkeypatch.setattr(_Taker, "run", fail)
    with pytest.raises(RuntimeError, match="consumer failed"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
            _Taker, _design(tmp_path, monkeypatch), {}
        )
    assert _probe(tmp_path / "xeda_run" / "d" / "__maker") == "free"


# Children announce the lock attempt before entering the blocking flock; stdout is only a
# synchronization pipe. Imports and Xeda logs go to stderr.
READER = """
import sys
from pathlib import Path
from xeda import Design
from xeda.flow_runner import DefaultRunner
from tests.io_flows import _Taker
root = Path(sys.argv[1])
def read(self):
    print("reading", flush=True)
    sys.stdin.readline()
    assert self.inputs.made.read_text() == "made\\n"
_Taker.run = read
DefaultRunner(root / "run", display_results=False).launch_flow(
    _Taker, Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}), {})
print("done", flush=True)
"""
WRITER = """
import sys
from pathlib import Path
from xeda import Design
from xeda.console import console
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import scrub_runs
from xeda.flow_runner import run_lock
from tests.io_flows import _Maker
root = Path(sys.argv[1])
action = sys.argv[2]
p = root / "run" / "d" / "__maker"
original = run_lock.fcntl.flock
def flock(fd, mode):
    if mode == run_lock.fcntl.LOCK_EX:
        print("waiting", flush=True)
    return original(fd, mode)
run_lock.fcntl.flock = flock
if action == "rebuild":
    DefaultRunner(root / "run", display_results=False, clean=True).launch_flow(
        _Maker, Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}), {})
else:
    # Keep the actual scrub confirmation/ownership/deletion path, suppressing its display.
    console.input = lambda *a, **kw: "yes"
    console.print = lambda *a, **kw: None
    assert scrub_runs("__maker", p.parent, run_root=root / "run")
print("done", flush=True)
"""


def _message(process, expected, timeout=30):
    import select

    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, f"child did not report {expected}"
    assert process.stdout.readline().strip() == expected


@pytest.mark.parametrize("action", ["rebuild", "scrub"])
def test_a_writer_waits_while_a_consumer_reads(tmp_path, action):
    import os
    import select

    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(__file__).resolve().parents[1]),
            ]
        ),
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", READER, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    writer = None
    try:
        _message(reader, "reading")
        writer = subprocess.Popen(
            [sys.executable, "-c", WRITER, str(tmp_path), action],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        _message(writer, "waiting")
        assert not select.select([writer.stdout], [], [], 0.2)[0], "writer proceeded under a reader"
        producer = tmp_path / "run" / "d" / "__maker"
        assert (producer / "made.txt").read_text() == "made\n"
        assert reader.stdin is not None
        reader.stdin.write("release\n")
        reader.stdin.flush()
        _message(reader, "done")
        _message(writer, "done")
        assert reader.wait(timeout=30) == writer.wait(timeout=30) == 0
        assert producer.exists() is (action == "rebuild")
        assert lock_file(producer).is_file(), "scrub retains the contended lock inode"
    finally:
        for child in (reader, writer):
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=30)
                if child.stdin:
                    child.stdin.close()
                if child.stdout:
                    child.stdout.close()


def test_a_parent_alias_uses_the_same_lock(tmp_path):
    parent = tmp_path / "run" / "d"
    parent.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    with run_dir_read_lock(parent / "producer"):
        assert _probe(alias / "producer") == "blocked"
        with pytest.raises(RunDirectoryError, match="shared"):
            with run_dir_lock(alias / "producer"):
                pytest.fail("an alias bypassed the shared lease")


def test_a_rebuild_under_this_threads_read_lease_is_refused(tmp_path, monkeypatch):
    from .io_flows import _Maker

    design = _design(tmp_path, monkeypatch)
    runner = DefaultRunner(tmp_path / "run", display_results=False, clean=True)
    producer = runner.get_flow_run_path("d", _Maker.name)
    with run_dir_read_lock(producer):
        with pytest.raises(RunDirectoryError, match="shared"):
            runner.launch_flow(_Maker, design, {})
        assert not producer.exists(), "rebuild wrote before acquiring an exclusive lock"


def test_another_thread_cannot_reenter_a_writers_lock(tmp_path):
    import threading

    path = tmp_path / "producer"
    attempting, acquired = threading.Event(), threading.Event()

    def write():
        attempting.set()
        with run_dir_lock(path):
            acquired.set()

    with run_dir_lock(path):
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        assert attempting.wait(10)
        assert not acquired.wait(0.2), "the other thread inherited writer authorization"
    assert acquired.wait(10)
    writer.join(timeout=10)
    assert not writer.is_alive()


def _fork_writer(path, connection):
    connection.send("waiting")
    with run_dir_lock(path):
        connection.send("acquired")
    connection.close()


def test_a_forked_worker_drops_inherited_holds_and_descriptors(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    writer = ctx.Process(target=_fork_writer, args=(tmp_path / "producer", child))
    try:
        with run_dir_read_lock(tmp_path / "producer"):
            writer.start()
            child.close()
            assert parent.poll(10) and parent.recv() == "waiting"
            assert not parent.poll(0.2), "forked worker inherited writer authorization"
        assert parent.poll(10) and parent.recv() == "acquired"
        writer.join(timeout=10)
        assert writer.exitcode == 0, "the inherited descriptor kept the shared hold alive"
    finally:
        if writer.is_alive():
            writer.terminate()
        writer.join(timeout=10)
        parent.close()
        child.close()


def test_two_processes_can_hold_read_leases_together(tmp_path):
    code = """
import sys
from pathlib import Path
from xeda.flow_runner.run_lock import run_dir_read_lock
with run_dir_read_lock(Path(sys.argv[1])):
    print("reading", flush=True)
    sys.stdin.readline()
print("done", flush=True)
"""
    path = tmp_path / "producer"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        with run_dir_read_lock(path):
            _message(child, "reading")
            assert _probe(path) == "blocked"
        assert _probe(path) == "blocked", "the other reader still holds its lease"
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        _message(child, "done")
        assert child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=20)
        if child.stdin:
            child.stdin.close()
        if child.stdout:
            child.stdout.close()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs unreadable output")
def test_uncertain_completion_evidence_refuses_a_consumer_before_it_runs(tmp_path, monkeypatch):
    from xeda.flow import FlowDependencyFailure

    design = _design(tmp_path, monkeypatch)
    ran = []
    original = _LegacyMaker.run

    def leave_unreadable(self):
        original(self)
        locked = self.run_path / "locked"
        locked.mkdir()
        (locked / "output.txt").write_text("cannot vouch for this output")
        locked.chmod(0)

    monkeypatch.setattr(_LegacyMaker, "run", leave_unreadable)
    monkeypatch.setattr(_LegacyReader, "run", lambda self: ran.append(self.name))
    try:
        with pytest.raises(FlowDependencyFailure, match="read lease"):
            DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
                _LegacyReader, design, {}
            )
        assert ran == []
        assert _probe(tmp_path / "run" / "d" / _LegacyMaker.name) == "free"
    finally:
        locked = tmp_path / "run" / "d" / _LegacyMaker.name / "locked"
        if locked.exists():
            locked.chmod(0o755)


SCRUB_VARIANT = """
import sys
from pathlib import Path
from xeda import Design
from xeda.console import console
from xeda.flow_runner import DefaultRunner, default_runner
from tests.io_flows import _Maker
root = Path(sys.argv[1])
console.input = lambda *a, **kw: "yes"
console.print = lambda *a, **kw: None
original = default_runner.scrub_runs
def scrub(*args, **kwargs):
    print("scrubbing", flush=True)
    sys.stdin.readline()
    return original(*args, **kwargs)
original_run = _Maker.run
def checked_run(self):
    original_run(self)
    assert self.outputs.made.read_text() == sys.argv[2]
_Maker.run = checked_run
default_runner.scrub_runs = scrub
flow = DefaultRunner(root / "run", hashed_run_dirs=True, scrub_old_runs=True,
                     display_results=False).launch_flow(
    _Maker, Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}),
    {"text": sys.argv[2]})
assert flow.succeeded
print("done", flush=True)
"""


def test_concurrent_hashed_launches_scrub_without_holding_their_own_locks(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(tmp_path / "run", hashed_run_dirs=True, display_results=False)
    for text in ("one", "two"):
        assert runner.launch_flow(_Maker, design, {"text": text}).succeeded
    children = []
    try:
        for text in ("one", "two"):
            child = subprocess.Popen(
                [sys.executable, "-c", SCRUB_VARIANT, str(tmp_path), text],
                cwd=tmp_path,
                env=_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append(child)
            _message(child, "scrubbing")
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        for child in children:
            try:
                stdout, stderr = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                pytest.fail("concurrent variants deadlocked while scrubbing each other")
            assert child.returncode == 0, stderr
            assert stdout.strip() == "done"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_shared_lock_acquisition_failure_is_a_clear_non_json_cli_error(tmp_path, monkeypatch):
    import errno
    from click.testing import CliRunner
    from xeda.cli import cli
    from xeda.flow_runner import run_lock

    monkeypatch.chdir(tmp_path)
    design = tmp_path / "d.toml"
    design.write_text('name="d"\n[rtl]\nsources=[]\ntop="t"\n')
    original = run_lock.fcntl.flock

    def no_shared_locks(fd, mode):
        if mode == run_lock.fcntl.LOCK_SH:
            raise OSError(errno.ENOLCK, "No locks available")
        return original(fd, mode)

    monkeypatch.setattr(run_lock.fcntl, "flock", no_shared_locks)
    result = CliRunner().invoke(cli, ["run", _Taker.name, str(design)])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "__maker" in result.output
    assert str(tmp_path / "xeda_run" / "d" / "__maker") in result.output
    assert "shared" in result.output and "No locks available" in result.output
    assert "Traceback" not in result.output
