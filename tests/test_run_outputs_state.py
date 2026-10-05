"""`tool_utils.run_outputs_state`: what "a launch left every output alone" compares."""

import os

from xeda.digest import TIME_MARKER_PREFIX
from xeda.flow_runner.trace import RESERVED_FILES

from . import tool_utils


def _run_dir(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "outputs" / "top.bit").write_bytes(b"bits")
    (run_dir / "results.json").write_text("{}\n")
    return run_dir


def test_the_names_xeda_reserves_are_no_output(tmp_path):
    run_dir = _run_dir(tmp_path)
    before = tool_utils.run_outputs_state(run_dir)
    for name in (*RESERVED_FILES, f"{TIME_MARKER_PREFIX}abc"):
        (run_dir / name).write_text("a refreshed trace\n")
    assert tool_utils.run_outputs_state(run_dir) == before


def test_every_other_change_to_a_file_is_one(tmp_path):
    run_dir = _run_dir(tmp_path)
    bitstream = run_dir / "outputs" / "top.bit"
    before = tool_utils.run_outputs_state(run_dir)
    stat = bitstream.stat()
    os.utime(bitstream, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))  # only its time
    assert tool_utils.run_outputs_state(run_dir) != before
    os.utime(bitstream, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert tool_utils.run_outputs_state(run_dir) == before
    bitstream.write_bytes(b"BITS")  # only its content, same size
    os.utime(bitstream, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert tool_utils.run_outputs_state(run_dir) != before
    bitstream.write_bytes(b"bits")
    os.utime(bitstream, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    (run_dir / "outputs" / "extra.txt").write_text("a new file\n")
    assert tool_utils.run_outputs_state(run_dir) != before
