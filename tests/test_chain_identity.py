"""D-9, the one identity rule: a node's identity is its settings plus the ordered origins of
its inputs (a producer's identity and output key, or "source"), for default and explicit edges
alike -- in the plan, the run directory, `results.json` and the trace, whose stale reasons name
the binding that changed."""

import json

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowFatalError, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.bindings import node_identity
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.default_runner import DIR_NAME_HASH_LEN
from xeda.flow_runner.trace import TRACE_FORMAT

from .io_flows import _ChainConsumer, _Join, _Left, _Taker


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def _design(tmp_path, sources=(), flows=None):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    entries = []
    for name, kind in sources:
        (root / name).write_text("data\n")
        entries.append({"file": name, "type": kind.name})
    return Design(name="d", design_root=root, rtl={"sources": entries}, flows=flows or {})


def _runner(tmp_path, **kwargs):
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _plan(tmp_path, flow, flows=None, sources=(), **kwargs):
    return _runner(tmp_path).plan(flow, _design(tmp_path, sources, flows), **kwargs)


def _ids(plan):
    return {node.name: node.flowrun_hash for node in plan.nodes}


def _settings_ids(plan):
    return {node.name: node.settings_hash for node in plan.nodes}


# ---------------------------------------------------------------------------------------------
# The identity matrix
# ---------------------------------------------------------------------------------------------


def test_identity_is_the_settings_hash_and_the_ordered_input_origins(tmp_path):
    plan = _plan(tmp_path, _Join)
    by_name = _ids(plan)
    fork, left, right, join = plan.nodes
    assert fork.origins == ()
    assert left.origins == (("json", ((by_name["__fork"], "a"),)),)
    assert right.origins == (("json", ((by_name["__fork"], "b"),)),)
    assert join.origins == (
        ("left", ((by_name["__left"], "out"),)),
        ("right", ((by_name["__right"], "out"),)),
        ("more", ()),
    )
    for node in plan.nodes:
        assert node.flowrun_hash == node_identity(node.settings_hash, node.origins)
        assert node.flowrun_hash != node.settings_hash


@pytest.mark.parametrize("reference", ["__maker", "__maker.made", "_Maker", "__MAKER"])
def test_a_restated_default_under_any_spelling_is_the_bare_request_s_identity(tmp_path, reference):
    bare = _plan(tmp_path, _Taker)
    restated = _plan(tmp_path, _Taker, {"__taker": {"inputs": {"made": reference}}})
    assert _ids(restated) == _ids(bare)


def test_where_a_binding_is_written_is_no_part_of_the_identity(tmp_path):
    runner = _runner(tmp_path)
    design = _design(tmp_path)
    saved = _plan(tmp_path, _Taker, {"__taker": {"inputs": {"made": "__input_maker.made"}}})
    plans = [
        saved,
        runner.plan(parse_request("__input_maker+__taker"), design),
        runner.plan(_Taker, design, flow_settings=["inputs.made=__input_maker"]),
        runner.plan(_Taker, design, flow_overrides={"inputs.made": "__input_maker.made"}),
    ]
    assert all(_ids(plan) == _ids(saved) for plan in plans)


def test_another_producer_another_output_and_another_order_are_other_identities(tmp_path):
    default = _plan(tmp_path, _Taker)
    other = _plan(tmp_path, _Taker, {"__taker": {"inputs": {"made": "__input_maker"}}})
    assert _ids(default)["__taker"] != _ids(other)["__taker"]
    assert _settings_ids(default)["__taker"] == _settings_ids(other)["__taker"]

    def consumer(left, right, files):
        bindings = {"left": left, "right": right, "files": files}
        return _plan(tmp_path, _ChainConsumer, {"__chain_consumer": {"inputs": bindings}})

    a, b = "__chain_producer.json_a", "__chain_producer.json_b"
    files = ["__chain_producer.files", "__chain_simple_producer.data"]
    straight = consumer(a, b, files)
    crossed = consumer(b, a, files)
    reordered = consumer(a, b, files[::-1])
    for changed in (crossed, reordered):
        # the producers are configured alike: only the consumer's wiring differs
        assert _ids(changed)["__chain_producer"] == _ids(straight)["__chain_producer"]
        assert _settings_ids(changed) == _settings_ids(straight)
        assert _ids(changed)["__chain_consumer"] != _ids(straight)["__chain_consumer"]
    assert _ids(crossed)["__chain_consumer"] != _ids(reordered)["__chain_consumer"]


def test_a_source_supplied_input_is_another_identity_than_a_produced_one(tmp_path):
    produced = _plan(tmp_path, _Taker)
    sourced = _plan(tmp_path, _Taker, sources=[("x.dat", SourceType.Data)])
    assert sourced.node("__taker").origins == (("made", "source"),)
    assert _settings_ids(sourced)["__taker"] == _settings_ids(produced)["__taker"]
    assert _ids(sourced)["__taker"] != _ids(produced)["__taker"]


def test_a_producer_s_settings_move_every_downstream_identity(tmp_path):
    before = _plan(tmp_path, _Join)
    after = _plan(tmp_path, _Join, {"__fork": {"label": "x"}})
    assert all(_ids(before)[name] != _ids(after)[name] for name in _ids(before))
    for name in ("__left", "__right", "__join"):
        assert _settings_ids(before)[name] == _settings_ids(after)[name]


def test_the_run_root_is_no_part_of_the_identity(tmp_path):
    design = _design(tmp_path)
    here = DefaultRunner(tmp_path / "a").plan(_Join, design)
    there = DefaultRunner(tmp_path / "b", hashed_run_dirs=True).plan(_Join, design)
    assert _ids(here) == _ids(there)


# ---------------------------------------------------------------------------------------------
# One identity at every seam: plan, directory, results, trace, validation
# ---------------------------------------------------------------------------------------------


def test_hashed_variants_of_one_flow_coexist_and_run_where_they_were_planned(tmp_path):
    source = tmp_path / "in.txt"
    source.write_text("bound\n")
    runner = _runner(tmp_path, hashed_run_dirs=True)
    design = _design(tmp_path)
    bound = _design(
        tmp_path,
        flows={
            "__taker": {"inputs": {"made": "__input_maker.made"}},
            "__input_maker": {"input_file": str(source)},
        },
    )
    paths = []
    for request in (design, bound):
        plan = runner.plan(_Taker, request)
        flow = runner.run(_Taker, request)
        node = plan.node("__taker")
        assert flow.succeeded and flow.run_path == node.run_path
        assert node.run_path.name == f"__taker_{node.flowrun_hash[:DIR_NAME_HASH_LEN]}"
        results = json.loads((flow.run_path / "results.json").read_text())
        trace = json.loads((flow.run_path / "trace.json").read_text())
        settings = json.loads((flow.run_path / "settings.json").read_text())
        assert results["flow_hash"] == trace["flowrun_hash"] == node.flowrun_hash
        assert settings["flowrun_hash"] == flow.flow_hash == node.flowrun_hash
        assert results["settings_hash"] == trace["settings_hash"] == node.settings_hash
        (producer,) = flow.completed_dependencies
        (record,) = trace["declared_inputs"]
        assert record["references"] == [
            {
                "producer": producer.name,
                "output": "made",
                "producer_hash": plan.node(producer.name).flowrun_hash,
                "producer_path": str(producer.run_path),
            }
        ]
        paths.append(flow.run_path)
    assert paths[0] != paths[1] and all(path.is_dir() for path in paths)
    assert trace["declared_inputs"][0]["binding_origin"] == "file" and trace["format"] == 14
    assert TRACE_FORMAT == 14


def test_a_tampered_identity_fails_before_execution(tmp_path):
    runner = _runner(tmp_path)
    design = _design(tmp_path)
    request = runner._request(_Taker, design, flow_overrides={"inputs.made": "__input_maker"})
    plan = runner._resolve_request(request)
    runner._request_context = request
    node = plan.node("__taker")
    saved = node.origins
    object.__setattr__(node, "origins", (("made", "source"),))
    with pytest.raises(FlowFatalError, match="identity or path"):
        runner.run_flow(
            _Taker, design, request.settings, all_flows_settings=request.sections, plan=plan
        )
    object.__setattr__(node, "origins", saved)
    object.__setattr__(node, "flowrun_hash", node.settings_hash)
    with pytest.raises(FlowFatalError, match="identity or path"):
        runner.run_flow(
            _Taker, design, request.settings, all_flows_settings=request.sections, plan=plan
        )
    assert not (tmp_path / "run").exists()


# ---------------------------------------------------------------------------------------------
# Trace explanations
# ---------------------------------------------------------------------------------------------


def _launched(runner, name):
    return [flow for flow in runner.launched if flow.name == name][-1]


def test_a_changed_producer_is_the_named_reason_and_the_same_wiring_given_otherwise_is_reused(
    tmp_path,
):
    source = tmp_path / "in.txt"
    source.write_text("made\n")  # the very bytes `__maker` writes
    maker = {"__input_maker": {"input_file": str(source)}}
    saved = {**maker, "__taker": {"inputs": {"made": "__input_maker.made"}}}
    runner = _runner(tmp_path)
    first = runner.run(_Taker, _design(tmp_path))
    assert first.succeeded and first.results["read"] == "made\n"
    rebound = runner.run(_Taker, _design(tmp_path, flows=saved))
    assert rebound.results["read"] == "made\n" and not rebound.reused
    assert rebound.stale_reason == "made now from __input_maker.made (was __maker.made)"
    # the same wiring from a chain instead of the file: nothing runs
    chained = runner.run(parse_request("__input_maker+__taker"), _design(tmp_path, flows=maker))
    assert chained.reused and chained.completed_dependencies[0].reused
    # and back to the default producer
    back = runner.run(_Taker, _design(tmp_path))
    assert back.stale_reason == "made now from __maker.made (was __input_maker.made)"
    sourced = runner.run(_Taker, _design(tmp_path, [("x.dat", SourceType.Data)]))
    assert sourced.stale_reason == "made now from the design's sources (was __maker.made)"


def test_a_changed_reference_order_is_the_named_reason(tmp_path):
    def run(more):
        design = _design(tmp_path, flows={"__join": {"inputs": {"more": more}}})
        return runner.run(_Join, design)

    runner = _runner(tmp_path)
    first = run(["__left.out", "__right.out"])
    assert first.succeeded and first.results["more"] == ["a\n", "b\n"]
    assert run(["__left.out", "__right.out"]).reused
    swapped = run(["__right.out", "__left.out"])
    assert swapped.results["more"] == ["b\n", "a\n"]
    assert swapped.stale_reason == (
        "more now from __right.out, __left.out (was __left.out, __right.out)"
    )
    assert all(flow.reused for flow in runner.launched[-4:-1]), "only the consumer ran again"
    emptied = run([])
    assert emptied.stale_reason == "more now from nothing (was __right.out, __left.out)"
    trace = json.loads((emptied.run_path / "trace.json").read_text())
    assert trace["declared_inputs"][2]["references"] == []
    assert trace["declared_inputs"][2]["binding_origin"] == "file"


def test_a_reconfigured_producer_and_a_settings_change_keep_their_own_reasons(tmp_path):
    runner = _runner(tmp_path)
    assert runner.run(_Left, _design(tmp_path)).succeeded
    again = runner.run(_Left, _design(tmp_path, flows={"__fork": {"label": "x"}}))
    assert _launched(runner, "__fork").stale_reason == "settings changed: label"
    assert again.stale_reason == (
        "__fork, which makes json, has other settings or inputs than in the last run"
    )
    own = runner.run(
        _Left, _design(tmp_path, flows={"__fork": {"label": "x"}, "__left": {"verbose": 1}})
    )
    assert own.stale_reason.startswith("settings changed: verbose")
    assert _launched(runner, "__fork").reused
    # when the settings and the wiring both change, the settings are the reason given: a
    # binding is named only when the settings hash is the recorded one
    both = runner.run(
        _Left,
        _design(
            tmp_path,
            flows={"__left": {"verbose": 2, "inputs": {"json": "__fork.b"}}},
        ),
    )
    assert both.stale_reason.startswith("settings changed: verbose")
