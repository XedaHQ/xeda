"""The `uart_link` example (`examples/bluespec/uart_link`): LeanLink, a reliable packet transport.

`test_bsc_examples.py` already runs the example like every Bluespec example: bsc without a
warning, the Verilog through iverilog, Verilator and Yosys, the self-checking bench in Bluesim,
Icarus Verilog and Verilator, and the bench with the planted bug (`XEDA_INJECT_BUG`). This file
adds what only this example has:

* the frames of the bench are the output of the generator, which uses the Python codec;
* the bench compares exact bytes: a changed byte in the table fails it;
* the cocotb tests run on the Verilog of the link with Verilator (a subset of the full suite,
  which `tb/run_cosim.py` runs);
* the link refuses pages below 16 bytes and a window above 128;
* the hardware script starts without a board.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim, Verilator

from .tool_utils import (
    HANG_GUARD,
    require_bluesim,
    require_bsc,
    require_cocotb,
    require_verilator,
    run_report,
)

pytestmark = pytest.mark.usefixtures("tool_output_on_terminal")

EXAMPLE = Path(__file__).parent.parent / "examples" / "bluespec" / "uart_link"
DESIGN = EXAMPLE / "uart_link.yaml"
COSIM = EXAMPLE / "tb" / "cosim.yaml"
FRAMES = EXAMPLE / "tb" / "LeanLinkFrames.bsv"

#: The cocotb tests that take seconds, not fractions of a second. `tb/run_cosim.py` runs them.
SLOW = {
    "bridge_budget_credit_recovers",
    "bridge_budget_with_a_slow_reader",
    "credit_edges_across_the_cursor_wrap",
    "cursor_wraps_past_65535",
    "every_request_size",
    "response_write_time_by_size",
    "second_open_straddling_the_reset",
    "stale_frame_straddling_the_session_reset",
    "tx_idle_all_ready_before_tx_starts",
    "tx_idle_with_ready_responses_fast_requests",
}
#: The tests for the profile with one page (W = 1): the cases where one page is different.
W1_TESTS = [
    "basic",
    "open_reply_is_exact",
    "duplicate_request_gets_status_and_no_execution",
    "lost_response_is_replayed_by_rewind",
    "zero_length_request",
    "maximum_request_payload_truncates_the_response",
    "stale_frame_straddling_the_session_reset_minimal",
    "random_faults_both_directions",
]
#: Modules of `tb/` that are no test of the link on the test endpoint.
NOT_ENDPOINT_TESTS = {"lean_tb.py", "lean_core_port.py"}


def _cocotb_tests() -> dict[str, list[str]]:
    """The names of the cocotb tests of each `tb/lean_*.py` module of the endpoint tests."""
    found = {}
    for path in sorted((EXAMPLE / "tb").glob("lean_*.py")):
        if path.name in NOT_ENDPOINT_TESTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found[path.name] = [
            node.name
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and any("cocotb.test" in ast.unparse(decorator) for decorator in node.decorator_list)
        ]
    return found


def test_the_frames_of_the_bench_are_the_output_of_the_generator(tmp_path):
    """The checked-in table is what the script makes from the codec, byte for byte."""
    out = tmp_path / "LeanLinkFrames.bsv"
    # -I: no site packages or user directory on the path; -B: no bytecode in the checkout
    subprocess.run(
        [sys.executable, "-I", "-B", str(EXAMPLE / "tb" / "gen_frames.py"), str(out)],
        check=True,
        timeout=HANG_GUARD,
    )
    assert out.read_bytes() == FRAMES.read_bytes()


def test_the_bench_covers_the_frames_the_example_promises():
    """The table holds the steps that the bench file lists: the power-up delimiter, UNBOUND, OPEN,
    a rejected CRC and a rejected COBS structure with no execution, a duplicate, and the largest
    frame (255 bytes)."""
    text = FRAMES.read_text(encoding="utf-8")
    kinds = re.findall(r"kind: (\w+)", text)
    assert kinds[0] == "Expect" and kinds.count("Quiet") == 2
    lengths = [int(n) for n in re.findall(r"kind: Expect, off: \d+, len: (\d+)", text)]
    assert 1 in lengths and 12 in lengths and 255 in lengths


def test_the_bench_compares_exact_bytes(tmp_path, capfd):
    """One byte of an expected response changed in the table makes the bench fail with a message
    that names the step and the byte."""
    require_bluesim()
    text = FRAMES.read_text(encoding="utf-8")
    # the first step expects the power-up delimiter, the second the UNBOUND status frame:
    # flip the second byte of that frame
    steps = re.findall(r"kind: (\w+), off: (\d+), len: (\d+)", text)
    step = [i for i, (kind, _, _) in enumerate(steps) if kind == "Expect"][1]
    match = list(re.finditer(r"8'h([0-9a-f]{2})", text))[int(steps[step][1]) + 1]
    flipped = f"{int(match.group(1), 16) ^ 1:02x}"
    mutated = tmp_path / "LeanLinkFrames.bsv"
    mutated.write_text(text[: match.start(1)] + flipped + text[match.end(1) :], encoding="utf-8")
    design = Design.from_file(DESIGN)
    design.tb.sources = [mutated, EXAMPLE / "tb" / "TbLeanLink.bsv"]
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        BscSim, design, {"simulator": "bluesim"}
    )
    out = capfd.readouterr().out
    assert flow is not None and not flow.succeeded
    assert f"FAIL: step {step}: byte 1 is" in out and "PASS" not in out


@pytest.mark.parametrize(
    "define, value, message",
    [("LL_LOGP", 3, "pages need at least 16 bytes"), ("LL_LOGW", 8, None)],
    ids=["pages-below-16-bytes", "window-above-128"],
)
def test_the_link_refuses_sizes_it_cannot_build(define, value, message, tmp_path, capfd):
    """`mkLeanLink` stops at elaboration: pages below 16 bytes have no room for the header, and
    a window above 128 does not fit the sequence fields (bsc rejects its types)."""
    require_bsc()
    design = Design.from_file(DESIGN)
    design.rtl.defines[define] = value
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(Bsc, design)
    captured = capfd.readouterr()
    assert flow is not None and not flow.succeeded, run_report(flow, captured)
    if message:
        assert message in captured.out + captured.err


def test_every_cocotb_test_is_in_the_quick_set_or_the_slow_set():
    """The CI runs every test of the endpoint modules but the slow ones. A new test is in the
    quick set unless this file lists it as slow; a name that is gone is an error."""
    names = [name for tests in _cocotb_tests().values() for name in tests]
    assert len(names) == len(set(names)), "a test name is used twice: the filter selects by name"
    assert SLOW <= set(names)
    assert set(W1_TESTS) <= set(names)


def _run_cosim(tmp_path, capfd, monkeypatch, target, names):
    require_bsc()
    require_verilator()
    require_cocotb()
    monkeypatch.setenv("LEAN_SEEDS", "4")  # the stress test: 4 seeds
    design = Design.from_file(COSIM, target=target)
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(
        Verilator, design, {"cocotb": {"testcase": names}}
    )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["cocotb.tests"] == len(names)
    assert flow.results["cocotb.failures"] == 0 and flow.results["cocotb.errors"] == 0
    assert flow.results["cocotb.skipped"] == 0


def test_cocotb_tests_on_the_link_with_eight_pages(tmp_path, capfd, monkeypatch):
    """The directed tests of the link against the Python codec: frame rejection, sessions,
    duplicates, lost responses, credit, sizes, performance and random faults (W = 8)."""
    names = [n for tests in _cocotb_tests().values() for n in tests if n not in SLOW]
    assert len(names) > 50
    _run_cosim(tmp_path, capfd, monkeypatch, None, names)


def test_cocotb_tests_on_the_link_with_one_page(tmp_path, capfd, monkeypatch):
    """The profile with one page of 64 bytes (W = 1) runs the cases where one page is special."""
    _run_cosim(tmp_path, capfd, monkeypatch, "w1", W1_TESTS)


def test_the_hardware_script_starts_without_a_board():
    """`--help` needs neither pyserial nor pyftdi, and a backend that needs a port says so."""
    script = EXAMPLE / "host" / "hw_suite.py"
    run = [sys.executable, "-I", "-B", str(script)]
    helped = subprocess.run(
        [*run, "--help"], capture_output=True, text=True, check=True, timeout=HANG_GUARD
    )
    assert "arty" in helped.stdout and "--ftdi-serial" in helped.stdout
    refused = subprocess.run(
        [*run, "stlv7325", "soak"], capture_output=True, text=True, timeout=HANG_GUARD
    )
    assert refused.returncode != 0 and "needs --port" in refused.stderr


def test_the_hardware_script_fails_on_a_recorded_violation():
    """A result with a violation, a mismatch, a failed programmer or an undetected reset is a
    failed run (a nonzero exit status)."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(EXAMPLE / 'host')!r})\n"
        "import hw_suite\n"
        "good = {'rows': [{'violations': 0, 'reset_detected': True, 'new_session_first_count': 0, 'programmer_rc': 0}]}\n"
        "bad = {'rows': [{'violations': 2}], 'reconfig': {'busy0': {'reset_detected': False}}}\n"
        "run = {'reset_detected': True, 'new_session_first_count': 0}\n"
        "again = {'a': dict(run, new_session_first_count=3), 'b': dict(run), 'c': dict(run)}\n"
        "del again['c']['new_session_first_count']\n"
        "print(json.dumps([hw_suite.failures(good), hw_suite.failures(bad),\n"
        "                  hw_suite.failures({'a': run}), hw_suite.failures(again)]))\n"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=HANG_GUARD,
    )
    good, bad, first_ok, first_bad = json.loads(run.stdout)
    assert good == [] and first_ok == []
    assert bad == [
        "rows[0].violations = 2",
        "reconfig.busy0.new_session_first_count = None",
        "reconfig.busy0.reset_detected = False",
    ]
    # a new session must start at count 0: a higher count, or no count, fails the run
    assert first_bad == ["a.new_session_first_count = 3", "c.new_session_first_count = None"]


@pytest.mark.parametrize(
    "script, args",
    [
        (EXAMPLE / "tb" / "run_cosim.py", ["--seeds", "0"]),
        (EXAMPLE / "tb" / "run_cosim.py", ["--seeds", "-3"]),
    ],
    ids=["zero-seeds", "negative-seeds"],
)
def test_the_cosim_script_rejects_a_seed_count_below_one(script, args):
    run = subprocess.run(
        [sys.executable, "-I", "-B", str(script), *args],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert run.returncode == 2 and "is not above zero" in run.stderr


def test_the_protocol_simulation_rejects_a_request_count_below_one():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(EXAMPLE / 'host')!r})\n"
        "from leanlink import sim\n"
        "sim.main(['throughput', '--n', '0'])\n"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert run.returncode == 2 and "is not above zero" in run.stderr
