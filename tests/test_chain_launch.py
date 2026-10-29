"""Executing exactly the resolved bound inputs: one launch per node in plan order, ordered
hand-over through checked output records, producers held for reading through the consumer's
results and trace, and deliveries only for a graph that succeeded."""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowDependencyFailure
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner import default_runner as runner_module
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.trace import as_recorded

from .flow_registry import preserved_flow_registry
from . import tool_utils
from .io_flows import TJoin, TTaker
from .test_read_locks import _probe

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _design(tmp_path, flows=None):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    return Design(name="d", root=root, rtl={"sources": [], "top": "t"}, flows=flows or {})


def _runner(tmp_path, **kwargs):
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _bound(tmp_path, text="bound\n"):
    """A design whose `t_taker` reads the alternate producer `t_input_maker`."""
    source = tmp_path / "in.txt"
    source.write_text(text)
    return _design(
        tmp_path,
        {
            "t_taker": {"inputs": {"made": "t_input_maker.made"}},
            "t_input_maker": {"input_file": str(source)},
        },
    )


MORE = {"t_join": {"inputs": {"more": ["t_right.out", "t_left.out", "t_right.out"]}}}

# --------------------------------------------------------------------------------- the oracle


def test_the_launch_follows_the_plan_node_for_node_and_reference_for_reference(tmp_path):
    runner = _runner(tmp_path, hashed_run_dirs=True)
    design = _design(tmp_path, MORE)
    plan = runner.plan(TJoin, design)
    frozen = [(n.name, as_recorded(n.settings), n.flowrun_hash, n.inputs) for n in plan.nodes]
    flow = runner.run(TJoin, design)
    assert flow.succeeded
    # one launch per node, producers before consumers, in the plan's order and directories
    assert [(f.name, f.flow_hash, f.run_path) for f in runner.launched] == [
        (n.name, n.flowrun_hash, n.run_path) for n in plan.nodes
    ]
    assert [as_recorded(f.settings) for f in runner.launched[:1]] == [frozen[0][1]]
    # two branches demand the fork's two outputs: unioned before it ran, once
    fork = runner.launched[0]
    assert fork.settings.first and fork.settings.second
    # the ordered many input: each reference's files, in reference order, duplicates kept
    left, right = (plan.node(name).run_path / "out.txt" for name in ("t_left", "t_right"))
    assert flow.inputs.more == [right, left, right]
    assert flow.results["more"] == ["b\n", "a\n", "b\n"]
    record = json.loads((flow.run_path / "trace.json").read_text())["declared_inputs"][2]
    assert [ref["producer"] for ref in record["references"]] == ["t_right", "t_left", "t_right"]
    assert record["paths"] == [str(right), str(left), str(right)]
    assert [ref["producer_hash"] for ref in record["references"]] == [
        plan.node(ref["producer"]).flowrun_hash for ref in record["references"]
    ]
    # the plan the launcher followed is as it was resolved
    assert frozen == [
        (n.name, as_recorded(n.settings), n.flowrun_hash, n.inputs) for n in plan.nodes
    ]
    again = runner.run(TJoin, design)
    assert again.reused and all(f.reused for f in runner.launched[-4:])


def test_an_alternate_producer_is_launched_once_and_then_reused(tmp_path):
    runner = _runner(tmp_path)
    design = _bound(tmp_path)
    first = runner.run(TTaker, design)
    assert first.succeeded and first.results["read"] == "bound\n"
    assert [f.name for f in runner.launched] == ["t_input_maker", "t_taker"]
    assert not (tmp_path / "run" / "d" / "t_maker").exists(), "the default producer never ran"
    again = runner.run(TTaker, design)
    assert again.reused and tool_utils.producers_of(runner, again)[0].reused
    assert again.inputs.made == first.inputs.made


# --------------------------------------------------------------- records, tampering and leases


def _tamper(producer_path: Path, tmp_path: Path, tamper: str) -> None:
    results_json = producer_path / "results.json"
    results = json.loads(results_json.read_text())
    if tamper == "sha":
        results["outputs"]["made"]["digests"] = ["0" * 32]
    elif tamper == "missing":
        results["outputs"]["made"]["names"] = ["absent.txt"]
    elif tamper == "outside":
        outside = tmp_path / "outside.txt"
        outside.write_text("bound\n")
        results["outputs"]["made"]["names"] = ["../outside.txt"]
    else:
        del results["outputs"]["made"]
    results_json.write_text(json.dumps(results))


TAMPERS = pytest.mark.parametrize("tamper", ["sha", "missing", "no-record", "outside"])


@TAMPERS
def test_a_bound_producer_s_record_is_checked_at_hand_over(tmp_path, monkeypatch, tamper):
    """The consumer takes a bound producer's file through its output record, under the lease:
    a record that is missing, names another file or no longer matches the bytes refuses the
    hand-over, and the consumer's directory then holds a failure document and no trace."""
    runner = _runner(tmp_path)
    design = _bound(tmp_path)
    first = runner.run(TTaker, design)
    assert first.succeeded
    original = runner._producer_read_lease

    @contextmanager
    def lease(producer):
        with original(producer):
            # after the completed generation was verified, under the lease
            record = producer.results["outputs"]
            if tamper == "sha":
                (producer.run_path / "made.txt").write_text("other bytes\n")
            elif tamper == "missing":
                (producer.run_path / "made.txt").unlink()
            elif tamper == "outside":
                outside = tmp_path / "outside.txt"
                outside.write_text("changed\n")
                record["made"] = {**record["made"], "path": str(outside)}
            else:
                del record["made"]
            yield producer

    monkeypatch.setattr(runner, "_producer_read_lease", lease)
    (tmp_path / "in.txt").write_text("changed\n")  # the producer runs again
    with pytest.raises(FlowDependencyFailure):
        runner.run(TTaker, _bound(tmp_path, "changed\n"))
    failure = json.loads((first.run_path / "results.json").read_text())
    assert failure["outcome"] == "failed" and failure["error"]["code"] == "producer_failed"
    assert failure["identity"] == first.flow_hash, "the failure keeps the run's identity"
    assert not (first.run_path / "trace.json").exists()
    assert _probe(tmp_path / "run" / "d" / "t_input_maker") == "free"


@TAMPERS
def test_a_bound_producer_s_edited_record_makes_it_run_again(tmp_path, tamper):
    """Between launches, a hand edit of a bound producer's record is a changed output of its
    run: the producer runs again and hands over what it then records."""
    runner = _runner(tmp_path)
    design = _bound(tmp_path)
    first = runner.run(TTaker, design)
    (producer,) = tool_utils.producers_of(runner, first)
    _tamper(producer.run_path, tmp_path, tamper)
    again = runner.run(TTaker, design)
    assert again.succeeded and again.results["read"] == "bound\n"
    (rerun,) = tool_utils.producers_of(runner, again)
    assert not rerun.reused and "results.json" in rerun.stale_reason
    assert again.inputs.made == rerun.run_path / "made.txt"


def test_every_bound_producer_is_held_through_the_consumer_s_results_and_trace(
    tmp_path, monkeypatch
):
    seen: dict[str, list[str]] = {}
    producers = [tmp_path / "run" / "d" / name for name in ("t_left", "t_right")]

    def probe(stage):
        seen[stage] = [_probe(path) for path in producers] + [
            _probe(path, "shared") for path in producers
        ]

    def run(self):
        probe("run")
        self.results["read"] = "read"

    write_trace = runner_module.write_trace

    def writing(run_path, trace):
        if run_path.name == "t_join":
            probe("trace")
        return write_trace(run_path, trace)

    monkeypatch.setattr(TJoin, "run", run)
    monkeypatch.setattr(runner_module, "write_trace", writing)
    flow = _runner(tmp_path).run(TJoin, _design(tmp_path, MORE))
    assert flow.succeeded
    # The producers ran in this launch: it holds them exclusively, so no other launch, writer
    # or reader, enters before the consumer's results and trace are written.
    held = ["blocked", "blocked", "blocked", "blocked"]
    assert seen == {"run": held, "trace": held}
    assert [_probe(path) for path in producers] == ["free", "free"]


def test_a_bound_producer_changed_after_it_completed_is_refused(tmp_path, monkeypatch):
    """The launch holds the run directory of the alternate producer from before it runs until
    the end, so no Xeda process completes another generation there. A file that changes anyway
    (something outside Xeda) between the producer's completion and the hand-over is refused."""
    runner = _runner(tmp_path)
    design = _bound(tmp_path)
    original = runner._producer_read_lease
    ran = []
    monkeypatch.setattr(TTaker, "run", lambda self: ran.append(self.name))

    @contextmanager
    def gap(producer):
        if producer.name == "t_input_maker":
            (producer.run_path / "another-output.txt").write_text("changed generation")
        with original(producer):
            yield producer

    monkeypatch.setattr(runner, "_producer_read_lease", gap)
    with pytest.raises(FlowDependencyFailure, match="changed.*read lease"):
        runner.run(TTaker, design)
    assert not ran
    assert _probe(tmp_path / "run" / "d" / "t_input_maker") == "free"


def test_a_failing_bound_producer_blocks_the_consumer_and_leaves_its_earlier_record(tmp_path):
    """The alternate producer fails (its input file is gone): the consumer never runs and writes
    nothing, so its directory keeps the record and the trace of its earlier success."""
    runner = _runner(tmp_path)
    design = _bound(tmp_path)
    first = runner.run(TTaker, design)
    assert first.succeeded
    (tmp_path / "in.txt").unlink()
    broken = _design(
        tmp_path,
        {
            "t_taker": {"inputs": {"made": "t_input_maker.made"}},
            "t_input_maker": {"input_file": str(tmp_path / "in.txt"), "text": "again"},
        },
    )
    with pytest.raises(FlowDependencyFailure, match="t_input_maker"):
        runner.run(TTaker, broken)
    kept = json.loads((first.run_path / "results.json").read_text())
    assert kept["outcome"] == "passed" and kept["run_id"] == first.run_id
    assert (first.run_path / "trace.json").exists()
    assert [f.name for f in runner.launched[-2:]] == ["t_input_maker", "t_taker"]


# ------------------------------------------------------------------------------------ delivery

ECP5 = "LFE5U-25F-6BG381C"


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


def _fpga_design(tmp_path: Path, flows) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    return Design(name="top", root=root, rtl={"sources": ["top.v"], "top": "top"}, flows=flows)


def test_a_chain_delivers_only_after_the_whole_graph_succeeded(tmp_path, toolchain, monkeypatch):
    """An upstream node's named delivery waits for the requested flow; `--outputs-to` is the
    requested node's alone."""
    config = tmp_path / "delivered" / "routed.config"
    config.parent.mkdir()
    outputs = tmp_path / "out"
    design = _fpga_design(tmp_path, {"nextpnr": {"fpga": {"part": ECP5}, "textcfg": str(config)}})
    chain = parse_request("yosys_fpga+nextpnr+fpga_pack")
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "ecppack")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "partial")
    failing = _runner(tmp_path, outputs_to=outputs)
    try:
        failed = failing.run(chain, design)
    except Exception:  # noqa: BLE001 - either way, the packer did not succeed
        failed = None
    assert failed is None or not failed.succeeded
    nextpnr = [f for f in failing.launched if f.name == "nextpnr"][-1]
    assert nextpnr.succeeded, "the upstream node itself succeeded"
    assert not config.exists() and not outputs.exists()
    monkeypatch.delenv("XEDA_FAKE_FPGA_MODE")
    runner = _runner(tmp_path, outputs_to=outputs)
    flow = runner.run(chain, design)
    assert flow.succeeded and config.is_file()
    delivered = sorted(p.name for p in outputs.rglob("*") if p.is_file())
    # the bitstream, and the log of the packer, which is an artifact of the requested flow
    assert "ecppack.log" in delivered and "top.bit" in delivered
    assert all(name.startswith("top.") or name == "ecppack.log" for name in delivered), delivered
    assert not any(
        d.destination.is_relative_to(outputs) for f in runner.launched[:-1] for d in f.deliveries
    ), "only the requested flow delivers to --outputs-to"


def test_a_delivery_cannot_replace_a_file_the_chain_reads(tmp_path, toolchain):
    """A handed-over input is a file the launch reads: no node's delivery may land on it."""
    design = _fpga_design(tmp_path, {"nextpnr": {"fpga": {"part": ECP5}}})
    runner = _runner(tmp_path)
    flow = runner.run(parse_request("nextpnr+fpga_pack"), design)
    assert flow.succeeded
    assert runner._read_inputs.find(flow.inputs.config) is not None
    bitstream = [f for f in runner.launched if f.name == "nextpnr"][-1].run_path / "config.txt"
    assert bitstream == flow.inputs.config
