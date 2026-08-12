"""Tests for the live output streaming used by `xeda.flow_runner.remote`.

These use execnet's local "popen" gateway (which spawns a plain local Python
interpreter) rather than an ssh gateway, so no remote host or network is
needed. That is a faithful stand-in for the real thing here: execnet's worker
bootstrap (`execnet.gateway_base.init_popen_io`) `dup`s the real stdout aside
for its own wire protocol and points fd 1 at /dev/null for *every* gateway
kind, ssh included. So a popen gateway reproduces exactly the file-descriptor
situation `STREAM_OUTPUT_SETUP` has to cope with.
"""

import ast
import inspect
import re
import sys
import textwrap
import time
from typing import List, Tuple

import execnet

from xeda.flow_runner.remote import STREAM_OUTPUT_SETUP, remote_runner

TimedChunks = List[Tuple[float, str]]

# Makes `import xeda` (and any submodule) fail inside the worker, to prove the
# streaming setup is pure-stdlib and does not depend on the remote host's xeda.
BLOCK_XEDA_IMPORTS = r"""
import sys


class _BlockXeda:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xeda" or fullname.startswith("xeda."):
            raise ImportError("xeda imports are blocked by this test")
        return None

    def find_module(self, fullname, path=None):  # pre-3.12 import machinery
        return self.find_spec(fullname, path)


for _name in [n for n in sys.modules if n == "xeda" or n.startswith("xeda.")]:
    del sys.modules[_name]
sys.meta_path.insert(0, _BlockXeda())
channel.send("blocked")
"""


def run_streamed(worker_body: str, block_xeda: bool = False) -> Tuple[TimedChunks, TimedChunks]:
    """Set up remote output streaming on a local execnet worker exactly as
    `RemoteRunner.run_remote` does, run `worker_body` inside that worker, then
    tear the streaming down. Returns the timestamped chunks that arrived on
    each of the stdout/stderr channels.
    """
    gw = execnet.makegateway("popen")
    try:
        if block_xeda:
            assert gw.remote_exec(BLOCK_XEDA_IMPORTS).receive() == "blocked"

        out_chunks: TimedChunks = []
        err_chunks: TimedChunks = []

        def collect(into: TimedChunks):
            # `endmarker=None` delivers a final None when the channel closes;
            # record only real data (`RemoteLogger.cb` skips it the same way)
            def cb(data):
                if data is not None:
                    into.append((time.monotonic(), data))

            return cb

        stream_channel = gw.remote_exec(STREAM_OUTPUT_SETUP)
        outchan, errchan = stream_channel.receive()
        outchan.setcallback(collect(out_chunks), endmarker=None)
        errchan.setcallback(collect(err_chunks), endmarker=None)

        work = gw.remote_exec(worker_body)
        work.waitclose()

        stream_channel.send("stop")
        assert stream_channel.receive() == "stopped"
        stream_channel.waitclose()
        return out_chunks, err_chunks
    finally:
        gw.exit()


def text_of(chunks: TimedChunks) -> str:
    return "".join(text for _, text in chunks)


def test_subprocess_output_is_streamed_back():
    """The case that was broken: a *subprocess* spawned by the remote flow (an
    EDA tool) inherits fds 1/2, so its output must reach us -- this never went
    through the worker's Python-level sys.stdout at all."""
    out_chunks, _ = run_streamed(f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "print('hello-from-tool')"])
channel.send("ran")
""")
    assert "hello-from-tool" in text_of(out_chunks)


def test_streaming_works_without_any_xeda_on_the_remote():
    """Regression test for the breakage this fix replaced: the streaming setup
    must not import anything from xeda, so that it works against a remote host
    whose installed xeda is older than the local one.

    `import xeda` raises inside this worker, so if `STREAM_OUTPUT_SETUP` (or
    the pumping it starts) touched xeda at all, this would fail.
    """
    out_chunks, _ = run_streamed(
        f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "print('tool-output-without-xeda')"])
channel.send("ran")
""",
        block_xeda=True,
    )
    assert "tool-output-without-xeda" in text_of(out_chunks)


def test_block_xeda_helper_actually_blocks_xeda():
    """Guards the test above: if the import block silently stopped working,
    that test would keep passing while proving nothing."""
    gw = execnet.makegateway("popen")
    try:
        assert gw.remote_exec(BLOCK_XEDA_IMPORTS).receive() == "blocked"
        outcome = gw.remote_exec("""
try:
    import xeda
    channel.send("imported")
except ImportError:
    channel.send("blocked")
""").receive()
        assert outcome == "blocked"
    finally:
        gw.exit()


def test_stream_output_setup_never_touches_xeda():
    """Static counterpart to the test above: the streaming setup is shipped to
    the remote host and runs against *its* xeda, so it must stay stdlib-only.

    Checks the parsed code rather than the raw text, so that comments are free
    to explain why the constraint exists without tripping the check.
    """
    tree = ast.parse(STREAM_OUTPUT_SETUP)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not [m for m in imported if m.split(".")[0] == "xeda"], imported

    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not [n for n in referenced if "xeda" in n.lower()], referenced


def test_shipped_remote_code_only_uses_long_stable_xeda_api():
    """`remote_runner`'s source is shipped to the remote host and executed
    against whatever xeda is installed *there*, which may be older than this
    one. Importing a newly-added xeda API in it breaks every remote host that
    hasn't been updated (which is exactly how this feature once broke), so the
    imports it may rely on are deliberately pinned to a tiny stable set.

    If this fails, do not just widen the allowlist: either the remote code must
    stop needing the new API, or remote xeda installs now have a hard minimum
    version that has to be checked and documented.
    """
    allowed = {("xeda.flow_runner", "DefaultRunner")}

    tree = ast.parse(textwrap.dedent(inspect.getsource(remote_runner)))
    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("xeda")
        for alias in node.names
    }
    imported |= {
        (alias.name, "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("xeda")
    }
    assert imported <= allowed, f"remote_runner gained xeda dependencies: {imported - allowed}"


def test_worker_own_writes_are_streamed_back():
    """Output the remote xeda itself prints (via sys.stdout/sys.stderr, which
    are bound to fds 1/2) must be streamed too, on the matching stream."""
    out_chunks, err_chunks = run_streamed("""
import sys
print("printed-to-stdout")
print("printed-to-stderr", file=sys.stderr)
sys.stdout.flush()
sys.stderr.flush()
channel.send("ran")
""")
    assert "printed-to-stdout" in text_of(out_chunks)
    assert "printed-to-stderr" in text_of(err_chunks)


def test_stdout_and_stderr_are_kept_separate():
    out_chunks, err_chunks = run_streamed(f"""
import subprocess
subprocess.run(
    [{sys.executable!r}, "-c",
     "import sys; print('to-out'); print('to-err', file=sys.stderr)"]
)
channel.send("ran")
""")
    out_text, err_text = text_of(out_chunks), text_of(err_chunks)
    assert "to-out" in out_text and "to-out" not in err_text
    assert "to-err" in err_text and "to-err" not in out_text


def test_output_arrives_live_rather_than_all_at_the_end():
    """The whole point of the feature: a long-running tool's output must show
    up while it is still running, not in one burst once it exits."""
    delay = 0.3
    out_chunks, _ = run_streamed(f"""
import subprocess
subprocess.run([
    {sys.executable!r}, "-c",
    "import time\\n"
    "for i in range(3):\\n"
    "    print('line%d' % i, flush=True)\\n"
    "    time.sleep({delay})\\n"
])
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    for i in range(3):
        assert f"line{i}" in out_text

    arrivals = [t for t, text in out_chunks if text.strip()]
    assert len(arrivals) >= 2
    # buffered-until-exit delivery would put every chunk within a few ms of the
    # others; live delivery has to span the sleeps between the lines
    assert arrivals[-1] - arrivals[0] >= delay


def test_lines_are_not_split_or_mangled_by_the_pty():
    """A pty's default line discipline turns '\\n' into '\\r\\n'; the setup
    disables that, so what arrives should be exactly what was printed."""
    out_chunks, _ = run_streamed(f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "print('alpha'); print('beta')"])
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    assert "alpha\nbeta\n" in out_text
    assert "\r" not in out_text


def test_carriage_return_next_to_a_newline_is_dropped():
    """Regression test for the doubled line breaks seen with vivado.

    Vivado sets `highlight_rules`, so on the remote its output goes through
    `run_process`'s piped/highlighting branch, which re-prints every line. In
    xeda versions that append `end="\\r"` to a line that already ends in '\\n',
    each line goes out as 'text\\n\\r' -- which renders with a blank line after
    it. The remote may well be running such a version and we cannot patch it
    from here, so the stream has to be cleaned up on this side of the wire.
    """
    out_chunks, _ = run_streamed(r"""
import sys
for line in ("****** Vivado v2024.2.2 (64-bit)", "  **** SW Build 6060944"):
    sys.stdout.write(line + "\n\r")     # exactly what `end="\r"` produces
sys.stdout.flush()
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    assert out_text == "****** Vivado v2024.2.2 (64-bit)\n  **** SW Build 6060944\n", repr(out_text)


def test_carriage_return_split_from_its_newline_across_reads_is_dropped():
    """The '\\n' can end one os.read() and the '\\r' begin the next; the pump
    remembers only whether the previous chunk ended in a newline, so it still
    drops the '\\r' without ever holding output back."""
    out_chunks, _ = run_streamed(r"""
import sys, time
sys.stdout.write('alpha\n')
sys.stdout.flush()
time.sleep(0.3)              # force the '\r' into a separate read
sys.stdout.write('\rbeta\n')
sys.stdout.flush()
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    assert out_text == "alpha\nbeta\n", repr(out_text)


def test_lone_carriage_return_is_preserved_for_progress_lines():
    """A '\\r' that is NOT adjacent to a newline is how tools redraw a progress
    line in place. Dropping it would turn a progress bar into a wall of lines,
    so it has to survive."""
    out_chunks, _ = run_streamed(r"""
import sys
sys.stdout.write('progress 50%\rprogress 100%\ndone\n')
sys.stdout.flush()
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    assert out_text == "progress 50%\rprogress 100%\ndone\n", repr(out_text)


def test_genuine_blank_lines_in_tool_output_are_kept():
    """Only a redundant CR is removed -- real blank lines the tool printed (the
    vivado banner has one before 'source ...') must still come through."""
    out_chunks, _ = run_streamed(r"""
import sys
sys.stdout.write('    ** Copyright 1986-2022 Xilinx, Inc.\n\r\n\rsource vivado_synth.tcl\n\r')
sys.stdout.flush()
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    assert out_text == "    ** Copyright 1986-2022 Xilinx, Inc.\n\nsource vivado_synth.tcl\n", repr(
        out_text
    )


def test_large_output_is_streamed_without_truncation_or_deadlock():
    """Output bigger than any pipe/pty buffer must keep flowing (the pumps read
    concurrently) and arrive complete."""
    line_count = 2000
    out_chunks, _ = run_streamed(f"""
import subprocess
subprocess.run([
    {sys.executable!r}, "-c",
    "for i in range({line_count}): print('L%04d' % i)"
])
channel.send("ran")
""")
    out_text = text_of(out_chunks)
    received = re.findall(r"L\d{4}", out_text)
    assert received == [f"L{i:04d}" for i in range(line_count)]


def test_trailing_output_is_not_lost_at_teardown():
    """Output written immediately before the run ends must still be delivered:
    teardown drops the writers and waits for the pumps to drain."""
    out_chunks, _ = run_streamed(f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "print('the-very-last-line')"])
channel.send("ran")
""")
    assert "the-very-last-line" in text_of(out_chunks)


def test_worker_stdout_is_restored_after_teardown():
    """Teardown must put the worker's fds back, so the gateway stays usable and
    nothing keeps writing into a torn-down pty."""
    gw = execnet.makegateway("popen")
    try:
        stream_channel = gw.remote_exec(STREAM_OUTPUT_SETUP)
        outchan, errchan = stream_channel.receive()
        outchan.setcallback(lambda d: None, endmarker=None)
        errchan.setcallback(lambda d: None, endmarker=None)

        before = gw.remote_exec("import os; channel.send(os.fstat(1).st_ino)").receive()

        stream_channel.send("stop")
        assert stream_channel.receive() == "stopped"
        stream_channel.waitclose()

        after = gw.remote_exec("import os; channel.send(os.fstat(1).st_ino)").receive()
        assert before != after, "fd 1 should have pointed at the pty during streaming"

        # the gateway must still work normally afterwards
        assert gw.remote_exec("channel.send(21 * 2)").receive() == 42
    finally:
        gw.exit()
