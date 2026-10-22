"""A design that a flow cannot read is converted first: the converter runs, and the flow reads the
design it derived.

These tests plan and launch with the test converters of `tests/derived_flows.py`, which need no
tool, and plan the flows that read Verilog on a Bluespec design. `test_converted_designs_real.py`
runs real bsc and yosys.

A design that a converter turns into Verilog has one plan whether the user wrote the bare flow, a
chain (`bsc+yosys`) or a saved binding (`flows.yosys.inputs.design: bsc`): the same nodes, edges
and identities. The converter runs before the flow that reads its design is built, under the lock
of that flow, and the flow is built on the design the converter derived.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from xeda.flow import MetricSpec
from xeda import Design
from xeda.design import LANGUAGE_TYPES, SourceType
from xeda.flow import Flow, FlowDependencyFailure, FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DefaultRunner, launch_producers, plan_freshness
from xeda.flow_runner import default_runner as default_runner_module
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.plan_freshness import NodeFreshness
from xeda.flow_runner.resolver import Plan, planned_design_of
from xeda.flow_runner.trace import Freshness
from xeda.introspect import plan_info

from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .launch_utils import empty_project
from .settings_samples import flow_classes, minimal_settings

BSV = "package Top;\n// a comment\nendpackage\n"


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


# ------------------------------------------------------------------------------- the fixtures


def write_design(
    root: Path,
    *,
    flows: dict[str, Any] | None = None,
    tb: dict[str, Any] | None = None,
    targets: dict[str, Any] | None = None,
    sources: tuple[str, ...] = ("Top.bsv",),
    name: str = "d",
) -> Path:
    """Write a Bluespec design under `root` as a YAML file, and return the file.

    The design file is the form a user writes. `sources` are written with a stand-in text each."""
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        if not (root / source).exists():  # an edit of a test survives a second write
            (root / source).write_text(
                BSV if source.endswith(".bsv") else "module m(); endmodule\n"
            )
    document: dict[str, Any] = {
        "name": name,
        "rtl": {"sources": list(sources), "top": "mkTop", "clock": {"port": "CLK"}},
    }
    if tb is not None:
        document["tb"] = tb
        (root / "tb.v").write_text("module tb(); endmodule\n")
    if flows:
        document["flows"] = flows
    if targets:
        document["targets"] = targets
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False)


def results_of(tmp_path: Path, flow: str, design: str = "d") -> dict[str, Any]:
    """The `results.json` of `flow` in the run root of `runner(tmp_path)`."""
    return json.loads((tmp_path / "run" / design / flow / "results.json").read_text())


def shape(plan: Plan) -> list[tuple[Any, ...]]:
    """What a plan is, without the explanation of its edges: each node, its identity, its run
    directory and its inputs with their producers."""
    return [
        (
            node.name,
            node.flowrun_hash,
            str(node.run_path),
            tuple(
                (
                    item.name,
                    item.origin,
                    item.producer,
                    item.output,
                    item.sources,
                    tuple((ref.node, ref.output) for ref in item.references),
                )
                for item in node.inputs
            ),
        )
        for node in plan.nodes
    ]


# ------------------------------------------------------------------------------ plan equality

ECP5 = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": "10 ns"}}
#: (the bare flow, the chain, the flow whose `design` a saved binding names, settings by flow, and
#: whether the design has a Verilog testbench)
CASES = [
    ("yosys", "bsc+yosys", "yosys", {"yosys": {"clock": {"period": "5 ns"}}}, False),
    ("yosys_fpga", "bsc+yosys_fpga", "yosys_fpga", {"yosys_fpga": ECP5}, False),
    (
        "fpga_pack",
        "bsc+fpga_pack",
        "yosys_fpga",
        {"yosys_fpga": ECP5, "nextpnr": {"fpga": ECP5["fpga"]}, "fpga_pack": ECP5},
        False,
    ),
    ("verilator", "bsc+verilator", "verilator", {}, True),
    (
        "vivado_synth",
        "bsc+vivado_synth",
        "vivado_synth",
        {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}, "clock": {"period": "5 ns"}}},
        False,
    ),
    (
        "openroad",
        "bsc+openroad",
        "yosys",
        {"openroad": {"platform": "nangate45"}, "yosys": {"clock": {"period": "5 ns"}}},
        False,
    ),
]


@pytest.mark.parametrize("target", [None, "fast"])
@pytest.mark.parametrize(
    "bare, chain, binds, flows, with_tb", CASES, ids=[case[0] for case in CASES]
)
def test_bare_chain_and_saved_binding_plan_the_same_nodes_edges_and_identities(
    bare, chain, binds, flows, with_tb, target, tmp_path
) -> None:
    tb = {"sources": ["tb.v"], "top": "tb"} if with_tb else None
    targets = {"fast": {"flows": {"yosys": {"synth_flags": []}}}} if target else None
    path = write_design(tmp_path / "d", flows=flows, tb=tb, targets=targets)
    saved = {
        **flows,
        binds: {**flows.get(binds, {}), "inputs": {"design": "bsc"}},
    }
    bound_path = write_design(tmp_path / "bound", flows=saved, tb=tb, targets=targets)

    def plan(request: Any, file: Path) -> Plan:
        design = Design.from_file(file, target=target)
        return runner(tmp_path).plan(request, design)

    # the same design under two names has the same identities only if the name is the same
    bare_plan = plan(bare, path)
    chain_plan = plan(parse_request(chain), path)
    assert [n.name for n in bare_plan.nodes][0] == "bsc"
    assert shape(bare_plan) == shape(chain_plan)
    bound_plan = plan(bare, bound_path)
    # the saved binding is in the other file, under the same design name and the same run root
    assert [n[:2] + n[3:] for n in shape(bound_plan)] == [n[:2] + n[3:] for n in shape(bare_plan)]
    # only the explanation of the edge differs
    reasons = {
        node.name: [item.reason for item in node.inputs if item.name == "design"]
        for node in bare_plan.nodes
    }
    assert reasons[binds] and all(reasons[binds])
    assert all(
        item.reason is None for node in chain_plan.nodes for item in node.inputs
    ), "a chain says why through its own binding"


def test_the_plan_line_and_the_json_name_the_reason(tmp_path) -> None:
    design = Design.from_file(write_design(tmp_path / "d", flows={"yosys": {}}))
    plan = runner(tmp_path).plan("yosys", design)
    (item,) = plan.node("yosys").inputs
    assert item.describe() == (
        "design <- bsc.design (the design has Bluespec sources, which yosys does not read)"
    )
    info = plan_info(plan)["nodes"]
    assert [i["reason"] for node in info for i in node["inputs"]] == [
        "the design has Bluespec sources, which yosys does not read"
    ]
    chained = runner(tmp_path).plan(parse_request("bsc+yosys"), design)
    assert chained.node("yosys").inputs[0].describe() == "design <- bsc.design (chain)"
    assert plan_info(chained)["nodes"][1]["inputs"][0]["reason"] is None


def test_a_design_the_flow_reads_is_planned_as_it_is(tmp_path) -> None:
    path = write_design(tmp_path / "d", sources=("top.v",))
    plan = runner(tmp_path).plan("yosys", Design.from_file(path))
    assert [n.name for n in plan.nodes] == ["yosys"] and plan.node("yosys").inputs == ()


def test_only_the_languages_a_flow_cannot_read_are_converted(tmp_path) -> None:
    """VHDL and Verilog beside Bluespec: yosys reads them, bsc passes them through."""
    path = write_design(tmp_path / "d", sources=("Top.bsv", "pass.vhd", "ext.v"))
    plan = runner(tmp_path).plan("yosys", Design.from_file(path))
    assert [n.name for n in plan.nodes] == ["bsc", "yosys"]
    view = planned_design_of(
        plan, plan.node("yosys"), Design.from_file(path), runner(tmp_path).run_path_of
    )
    assert [(s.file.name, s.type.name) for s in view.rtl.sources] == [
        ("mkTop.v", "Verilog"),
        ("pass.vhd", "Vhdl"),
        ("ext.v", "Verilog"),
    ]


def test_a_converter_reads_the_design_as_it_is(tmp_path) -> None:
    """One hop: bsc has a design output itself, so its `design` input is never converted."""
    plan = runner(tmp_path).plan("bsc", Design.from_file(write_design(tmp_path / "d")))
    assert [n.name for n in plan.nodes] == ["bsc"] and plan.node("bsc").inputs == ()


@pytest.mark.parametrize(
    "flow_request, flows",
    [("bsc+yosys", {}), ("yosys", {"yosys": {"inputs": {"design": "bsc"}}})],
)
def test_bsc_conversion_requires_bluespec_rtl_during_planning(
    flow_request, flows, tmp_path
) -> None:
    design = Design.from_file(write_design(tmp_path / "d", sources=("top.v",), flows=flows))

    with pytest.raises(FlowSettingsException, match="bsc needs Bluespec RTL"):
        runner(tmp_path).plan(flow_request, design)

    assert not (tmp_path / "run").exists()


def test_a_bluespec_testbench_does_not_satisfy_bsc_rtl_conversion(tmp_path) -> None:
    path = write_design(
        tmp_path / "d",
        sources=("top.v",),
        tb={"sources": ["Tb.bsv"], "top": "mkTb"},
    )
    (path.parent / "Tb.bsv").write_text(BSV)
    design = Design.from_file(path)

    with pytest.raises(FlowSettingsException, match="bsc needs Bluespec RTL"):
        runner(tmp_path).plan("bsc+yosys", design)

    assert not (tmp_path / "run").exists()


def test_two_converters_that_fit_are_an_error_that_names_the_choices(tmp_path) -> None:
    fake, rival, to_vhdl = converter_flows()
    design = Design.from_file(write_design(tmp_path / "d"))
    names = sorted(["bsc", fake.name, rival.name, to_vhdl.name])  # yosys reads VHDL too
    with pytest.raises(FlowSettingsException) as raised:
        runner(tmp_path).plan("yosys", design)
    assert str(raised.value) == (
        "yosys does not read the design's Bluespec sources, and more than one flow converts "
        f"them: {', '.join(names)}. Choose one with a chain (`{names[0]}+yosys`) or a binding "
        f"(`flows.yosys.inputs.design: {names[0]}`)"
    )
    # either choice plans, and a binding names the converter for good
    chosen = Design.from_file(
        write_design(tmp_path / "e", flows={"yosys": {"inputs": {"design": fake.name}}}, name="e")
    )
    assert [n.name for n in runner(tmp_path).plan("yosys", chosen).nodes] == [fake.name, "yosys"]
    assert [n.name for n in runner(tmp_path).plan(parse_request("bsc+yosys"), design).nodes] == [
        "bsc",
        "yosys",
    ]


def test_a_converter_that_writes_what_the_flow_cannot_read_does_not_fit(tmp_path) -> None:
    _fake, _rival, to_vhdl = converter_flows()
    from xeda.flow_runner.chains import converter_edges
    from xeda.flows import Verilator, Yosys

    assert to_vhdl in [edge.producer for edge in converter_edges(Yosys)]  # yosys reads VHDL
    assert to_vhdl not in [edge.producer for edge in converter_edges(Verilator)]
    assert "bsc" in [edge.producer.name for edge in converter_edges(Verilator)]


def test_a_converter_cannot_take_a_converter_for_its_own_design(tmp_path) -> None:
    fake, _rival, _to_vhdl = converter_flows()
    design = Design.from_file(
        write_design(tmp_path / "d", flows={"bsc": {"inputs": {"design": "bsc"}}})
    )
    with pytest.raises(FlowSettingsException, match=r"`bsc\.design` cannot come from bsc itself"):
        runner(tmp_path).plan("bsc", design)


def test_a_flow_that_reads_no_bluespec_and_has_no_converter_is_refused_as_before(tmp_path) -> None:
    design = Design.from_file(write_design(tmp_path / "d"))
    with pytest.raises(FlowSettingsException, match="ghdl_sim cannot read the design's Bluespec"):
        runner(tmp_path).plan("ghdl_sim", design)


def test_a_bluespec_testbench_on_a_verilog_simulator_names_the_simulator_that_reads_it(
    tmp_path,
) -> None:
    path = write_design(tmp_path / "d", tb={"sources": ["Tb.bsv"], "top": "mkTb"})
    (tmp_path / "d" / "Tb.bsv").write_text(BSV)
    with pytest.raises(FlowSettingsException) as raised:
        runner(tmp_path).plan("verilator", Design.from_file(path))
    assert str(raised.value).endswith("; simulate a Bluespec testbench with bsc_sim")
    # a rejected rtl source gets no such advice
    ghdl = Design.from_file(write_design(tmp_path / "e", name="e"))
    with pytest.raises(FlowSettingsException) as raised:
        runner(tmp_path).plan("ghdl_sim", ghdl)
    assert "simulate a" not in str(raised.value)


# ----------------------------------------------------------------- the sweep over every flow


def _bluespec_sweep_design(root: Path, *, bluespec_testbench: bool = False) -> Design:
    """A design with Bluespec, Verilog and VHDL sources and a testbench, so every flow meets a
    language it reads and one it does not."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pass.vhd").write_text("entity pass is end;\n")
    if bluespec_testbench:
        (root / "Tb.bsv").write_text(BSV)
        tb = {"sources": ["Tb.bsv"], "top": "mkTb", "uut": "dut"}
    else:
        tb = {"sources": ["tb.v"], "top": "tb", "uut": "dut"}
    return Design.from_file(write_design(root, sources=("Top.bsv", "pass.v", "pass.vhd"), tb=tb))


def _unreadable(flow_class: type[Flow], design: Design) -> set[SourceType]:
    """The languages of the design's sources that the flow reads neither itself nor through the
    converter: what a refusal of the flow is about."""
    assert flow_class.reads_sources is not None
    sources = [*design.rtl.sources, *design.tb.sources]
    return {
        source.type
        for source in sources
        if source.type in LANGUAGE_TYPES and source.type not in flow_class.reads_sources
    }


@pytest.mark.parametrize("bluespec_testbench", [False, True], ids=["verilog_tb", "bluespec_tb"])
@pytest.mark.parametrize("cls, name", flow_classes(), ids=[name for _cls, name in flow_classes()])
def test_a_bluespec_design_plans_a_conversion_with_its_reason_or_fails_with_a_refusal(
    cls, name, bluespec_testbench, tmp_path
) -> None:
    """For every registered flow: the plan converts the Bluespec sources before any flow that
    cannot read them (and says why), or the request fails with a refusal about a language that
    the flow does not read (a flow that does not read VHDL refuses the VHDL source, and a flow
    that does not read Bluespec refuses a Bluespec testbench). No plan hands a source to a flow
    that does not read it, and every refusal on the sweep design is about a language."""
    design = _bluespec_sweep_design(tmp_path / "d", bluespec_testbench=bluespec_testbench)
    the_runner = runner(tmp_path)
    try:
        plan = the_runner.plan(
            name, design, empty_project(tmp_path), flow_settings=minimal_settings(cls)
        )
    except (FlowSettingsException, FlowSettingsError) as refusal:
        message = str(refusal)
        # the refusal may come from a flow that the request plans first (`vivado_power` plans
        # `vivado_postsynth_sim`), and the message starts with that flow's name
        match = re.match(r"(\w+) cannot read the design's", message)
        assert match, f"{name} refuses without a language: {message}"
        refusing = dict((n, c) for c, n in flow_classes())[match.group(1)]
        sources_of_design = [*design.rtl.sources, *design.tb.sources]
        refused = {s.type for s in sources_of_design if s.type.name in message}
        assert refused & _unreadable(refusing, design), f"{name} refuses wrongly: {message}"
        return
    for node in plan.nodes:
        flow_class = node.flow_class
        if flow_class.reads_sources is None:
            continue
        view = planned_design_of(plan, node, design, the_runner.run_path_of)
        conversion = flow_class.design_conversion(node.settings)
        handed = [
            source
            for part in sorted(flow_class.design_parts)
            for source in getattr(view, part).sources
            if source.type in LANGUAGE_TYPES
            and source.type not in flow_class.reads_sources
            and not (conversion and conversion.passes_through(source))  # a converter passes on
        ]
        assert handed == [], f"{node.name} would be handed {[str(s.file) for s in handed]}"
        converted = any(item.name == "design" and item.origin == "producer" for item in node.inputs)
        if SourceType.Bluespec not in flow_class.reads_sources and (
            SourceType.Bluespec in {s.type for s in design.rtl.sources}
        ):
            assert converted == (flow_class.name not in ("bsc",)), node.name
            reasons = [item.reason for item in node.inputs if item.name == "design"]
            assert all(reasons), f"{node.name}: a conversion says why"


def test_the_sweep_design_reaches_both_outcomes(tmp_path) -> None:
    """The sweep is not vacuous: on the sweep design some flows plan with a conversion, and some
    refuse a language they do not read."""
    design = _bluespec_sweep_design(tmp_path / "d")
    planned, refused = set(), set()
    for cls, name in flow_classes():
        try:
            runner(tmp_path).plan(
                name, design, empty_project(tmp_path), flow_settings=minimal_settings(cls)
            )
            planned.add(name)
        except (FlowSettingsException, FlowSettingsError):
            refused.add(name)
    assert {"yosys", "yosys_fpga", "vivado_synth", "openroad", "dc", "bsc"} <= planned
    assert {"ghdl_sim", "nvc", "ghdl_synth", "verilator", "bsc_sim"} <= refused


# ------------------------------------------------------------------------- the launch order


class Events(list):
    """The events of one launch, in order."""


def consumer_flows(events: Events, written: str = "converted") -> tuple[type[Flow], type[Flow]]:
    """A converter that writes Verilog for a Bluespec source (with the text of the source, less
    the comment lines) and a flow that reads Verilog. Both record what they do in `events`."""
    fake, _rival, _to_vhdl = converter_flows()

    class _Converter(fake):  # type: ignore[valid-type, misc]
        """Converts a Bluespec source into a module of its text, less the comment lines."""

        def run(self) -> None:
            from xeda.flow.derived import DerivedSources, Generated, Passed
            from xeda.utils import replacing_file

            events.append("converter runs")
            entries: list[Generated | Passed] = []
            for index, source in enumerate(self.design.rtl.sources):
                if source.type is SourceType.Bluespec:
                    path = self.run_path / f"{Path(source.file).stem}.v"
                    text = "".join(
                        line
                        for line in Path(source.file).read_text().splitlines(keepends=True)
                        if not line.startswith("//")
                    )
                    with replacing_file(self.run_directory.writable(path)) as f:
                        f.write(f"// {written}\n{text}")
                    entries.append(Generated(path, SourceType.Verilog))
                else:
                    entries.append(Passed(index))
            self.outputs.design = DerivedSources(self.design.rtl.top, tuple(entries))  # type: ignore[union-attr]

    class _Reader(Flow):
        """Reads Verilog, and notes each step of its launch."""

        results_description: dict[str, MetricSpec] = {}
        reads_sources = frozenset({SourceType.Verilog, SourceType.Vhdl})

        @classmethod
        def check_design_supported(cls, design, settings=None):
            events.append(("check", [s.file.name for s in design.rtl.sources]))
            super().check_design_supported(design, settings)

        @classmethod
        def design_scope(cls, settings):
            events.append("design hash")
            return super().design_scope(settings)

        def __init__(self, *args, **kwargs):
            events.append("construct")
            super().__init__(*args, **kwargs)

        def init(self) -> None:
            events.append("init")

        def run(self) -> None:
            events.append(("reader runs", [s.file.name for s in self.design.rtl.sources]))

    return _Converter, _Reader


def launch(tmp_path: Path, classes: tuple[type[Flow], type[Flow]], **options: Any):
    """Launch the reader of `classes` on a Bluespec design, with the converter bound to it."""
    converter, reader = classes
    path = write_design(
        tmp_path / "d",
        sources=options.get("sources", ("Top.bsv", "ext.v")),
        flows={reader.name: {"inputs": {"design": converter.name}}},
    )
    design = Design.from_file(path)
    the_runner = runner(tmp_path)
    flow = the_runner.run_flow(reader, design)
    return converter, reader, design, the_runner, flow


def test_the_converter_runs_before_the_flow_is_built_and_the_flow_reads_what_it_derived(
    tmp_path, monkeypatch
) -> None:
    events = Events()
    real = launch_producers.handed_design

    def handed(producer, output, design):
        derived, entries = real(producer, output, design)
        events.append(("handed", [s.file.name for s in derived.rtl.sources]))
        return derived, entries

    monkeypatch.setattr(launch_producers, "handed_design", handed)
    *_, flow = launch(tmp_path, consumer_flows(events))
    assert flow is not None and flow.succeeded
    derived_names = ["Top.v", "ext.v"]
    ran = events.index("converter runs")
    verified = events.index(("handed", derived_names))
    assert ran < verified
    # once the converter's record is verified: the check of the derived design, its hash, the
    # constructor, `init()`, and the run, in this order. The plan checked the planned design
    # (`mkTop.v`) before any of it.
    after = events[verified + 1 :]
    assert after[:4] == [("check", derived_names), "design hash", "construct", "init"]
    # the record of the run asks for the design scope again, after the run
    assert ("reader runs", derived_names) in after[4:]
    assert [event for event in after[4:] if event != "design hash"] == [
        ("reader runs", derived_names)
    ]
    assert "construct" not in events[:verified] and "init" not in events[:verified]
    assert ("check", ["mkTop.v", "ext.v"]) in events[:ran]


def test_the_flow_is_hashed_and_recorded_on_the_derived_design(tmp_path) -> None:
    events = Events()
    converter, reader, design, the_runner, flow = launch(tmp_path, consumer_flows(events))
    assert flow is not None
    derived = flow.design
    assert [s.file.name for s in derived.rtl.sources] == ["Top.v", "ext.v"]
    scope = reader.design_scope(flow.settings)
    assert flow.design_hash == derived.parts_hash(reader.design_parts, scope)
    assert flow.design_hash != design.parts_hash(reader.design_parts, scope)
    # the dependency run is named first, and the record of the design input holds the files
    assert [p.name for p in the_runner.launched] == [converter.name, reader.name]
    trace = json.loads((flow.run_path / "trace.json").read_text())
    (record,) = [r for r in trace["declared_inputs"] if r["name"] == "design"]
    assert record["origin"] == "producer" and record["producer"] == converter.name
    assert [Path(p).name for p in record["paths"]] == ["Top.v"]
    inputs = [Path(p).name for p in trace["inputs"]]
    assert "Top.v" not in inputs and "ext.v" in inputs and "Top.bsv" not in inputs
    document = json.loads((flow.run_path / "settings.json").read_text())
    listed = [
        Path(item["file"] if isinstance(item, dict) else item).name
        for item in document["design"]["rtl"]["sources"]
    ]
    assert listed == ["Top.v", "ext.v"]


def test_the_design_hash_of_the_reader_does_not_depend_on_where_the_run_root_is(tmp_path) -> None:
    """The hashes are free of location: the same design under two run roots, near the design and
    far from it, gives the reader one design hash and one identity."""
    events = Events()
    converter, reader = consumer_flows(events)
    path = write_design(
        tmp_path / "d",
        sources=("Top.bsv", "ext.v"),
        flows={reader.name: {"inputs": {"design": converter.name}}},
    )
    flows = [
        DefaultRunner(tmp_path / run_root, display_results=False).run_flow(
            reader, Design.from_file(path)
        )
        for run_root in ("run", "deeper/elsewhere/run")
    ]
    near, far = flows
    assert near is not None and far is not None and near.succeeded and far.succeeded
    assert near.run_path != far.run_path
    assert near.design_hash == far.design_hash
    assert near.flow_hash == far.flow_hash


def test_a_second_launch_runs_nothing_and_an_edit_reruns_what_it_changes(tmp_path) -> None:
    events = Events()
    classes = consumer_flows(events)
    launch(tmp_path, classes)
    assert "converter runs" in events and ("reader runs", ["Top.v", "ext.v"]) in events

    def relaunch() -> tuple[bool, bool]:
        events.clear()
        launch(tmp_path, classes)
        return "converter runs" in events, any(
            e[0] == "reader runs" for e in events if e != "x" and isinstance(e, tuple)
        )

    assert relaunch() == (False, False)
    # a comment edit: the converter runs, writes the same text, and the reader stays fresh
    source = tmp_path / "d" / "Top.bsv"
    source.write_text(source.read_text() + "// another comment\n")
    assert relaunch() == (True, False)
    # an edit that changes the Verilog reruns the reader
    source.write_text(source.read_text() + "wire x;\n")
    assert relaunch() == (True, True)
    # a pass-through source: the converter hashes all of `rtl`, and the reader reads the file
    (tmp_path / "d" / "ext.v").write_text("module m2(); endmodule\n")
    assert relaunch() == (True, True)
    assert relaunch() == (False, False)


def test_a_dry_run_agrees_with_the_launch(tmp_path) -> None:
    events = Events()
    classes = consumer_flows(events)
    converter, reader, design, the_runner, flow = launch(tmp_path, classes)
    assert flow is not None

    def states(runner_: DefaultRunner | None = None) -> dict[str, NodeFreshness]:
        _plan, judged = (runner_ or the_runner).plan_with_freshness(reader, design)
        return judged

    assert {name: s.state for name, s in states().items()} == {
        converter.name: "fresh",
        reader.name: "fresh",
    }
    source = tmp_path / "d" / "Top.bsv"
    source.write_text(source.read_text() + "// comment\n")
    judged = Design.from_file(tmp_path / "d" / "d.yaml")
    _plan, judged_states = the_runner.plan_with_freshness(reader, judged)
    assert judged_states[converter.name].state == "stale"
    assert judged_states[reader.name] == NodeFreshness("unknown", f"depends on {converter.name}")
    # after the launch, both are fresh again, as the dry run said they would be
    launch(tmp_path, classes)
    assert {name: s.state for name, s in states().items()} == {
        converter.name: "fresh",
        reader.name: "fresh",
    }
    # --rebuild-all makes the reader stale before it can depend on anything
    rebuilding = DefaultRunner(tmp_path / "run", display_results=False, rebuild_all=True)
    _plan, rebuilt = rebuilding.plan_with_freshness(reader, design)
    assert rebuilt[reader.name].state == "stale" and "--rebuild-all" in rebuilt[reader.name].reason
    # a generated file that changed by hand makes the converter stale, and the reader depends on it
    (the_runner.run_path_of(design.name, converter.name) / "Top.v").write_text(
        "module x(); endmodule\n"
    )
    _plan, tampered = the_runner.plan_with_freshness(reader, design)
    assert tampered[converter.name].state == "stale"
    assert tampered[reader.name].reason == f"depends on {converter.name}"


def test_a_fresh_converter_whose_record_does_not_verify_leaves_the_reader_unknown(
    tmp_path, monkeypatch
) -> None:
    """The trace of the converter holds, so the converter is fresh, but a file of its record in
    `results.json` has another digest. The reader is unknown, for the reason of the hand-over.

    A real edit of the record or of a generated file also changes the trace, which makes the
    converter stale. So the trace check of the converter is patched to hold."""
    events = Events()
    classes = consumer_flows(events)
    converter, reader, design, the_runner, flow = launch(tmp_path, classes)
    assert flow is not None
    converter_run = the_runner.run_path_of(design.name, converter.name)
    results_path = converter_run / "results.json"
    document = json.loads(results_path.read_text())
    (name,) = document["outputs"]["design"]["names"]
    generated = {"generated": str(converter_run / name)}
    (digest,) = document["outputs"]["design"]["digests"]
    document["outputs"]["design"]["digests"] = ["0" * 32]
    assert digest != "0" * 32
    results_path.write_text(json.dumps(document))
    real = plan_freshness.check_trace

    def trace_holds_for_the_converter(run_dir, *args, **kwargs):
        if Path(run_dir) == converter_run:
            return Freshness(True)
        return real(run_dir, *args, **kwargs)

    monkeypatch.setattr(plan_freshness, "check_trace", trace_holds_for_the_converter)
    _plan, judged = the_runner.plan_with_freshness(reader, design)
    assert judged[converter.name].state == "fresh"
    assert judged[reader.name].state == "unknown"
    assert judged[reader.name].reason.startswith(
        f"{converter.name}'s output `design` ({generated['generated']}) changed since its run "
        "recorded it"
    )


def test_a_generated_file_that_changed_before_the_hand_over_fails_the_flow(
    tmp_path, monkeypatch
) -> None:
    events = Events()
    classes = consumer_flows(events)
    real = launch_producers.handed_design

    def tampered(producer, output, design):
        (producer.run_path / "Top.v").write_text("module evil(); endmodule\n")
        return real(producer, output, design)

    monkeypatch.setattr(launch_producers, "handed_design", tampered)
    with pytest.raises(FlowDependencyFailure, match="changed since its run recorded it"):
        launch(tmp_path, classes)
    assert "construct" in events and "init" not in events
    results = results_of(tmp_path, classes[1].name)
    assert results["outcome"] == "failed"
    assert results["error"]["code"] == "producer_failed"


def test_a_converter_that_fails_fails_the_flow_with_the_reason_it_was_needed(
    tmp_path, monkeypatch
) -> None:
    """The flow is built on the planned design and not initialized; its results say why it did
    not run, and name the converter."""
    events = Events()
    converter, reader = classes = consumer_flows(events)

    def fails(self) -> None:
        raise RuntimeError("the converter is out of order")

    monkeypatch.setattr(converter, "run", fails)
    the_runner = runner(tmp_path)
    path = write_design(tmp_path / "d", flows={reader.name: {"inputs": {"design": converter.name}}})
    with pytest.raises(FlowDependencyFailure, match="the converter is out of order") as raised:
        the_runner.run_flow(reader, Design.from_file(path))
    assert raised.value.producer == converter.name
    assert "construct" in events and "init" not in events
    assert [f.name for f in the_runner.launched] == [converter.name, reader.name]
    assert the_runner.launched[-1].blocked_by == converter.name
    results = results_of(tmp_path, reader.name)
    assert results["outcome"] == "failed" and results["error"]["code"] == "producer_failed"
    assert results["identity"]
    assert classes  # both registered for the test only


def test_a_failing_converter_leaves_a_real_flow_built_on_the_planned_design_untouched(
    tmp_path, monkeypatch
) -> None:
    """The flow that waits for the converter is built on the planned design, whose generated
    source does not exist. Nothing reads or hashes it: the flow has no design hash, its run
    directory holds the failure, and the planned file is not created."""
    fake, _rival, _to_vhdl = converter_flows()

    def fails(self) -> None:
        raise RuntimeError("the converter is out of order")

    monkeypatch.setattr(fake, "run", fails)
    path = write_design(tmp_path / "d", flows={"yosys": {"inputs": {"design": fake.name}}})
    the_runner = runner(tmp_path)
    with pytest.raises(FlowDependencyFailure, match="the converter is out of order"):
        the_runner.run_flow("yosys", Design.from_file(path))
    consumer = the_runner.launched[-1]
    assert consumer.name == "yosys" and consumer.blocked_by == fake.name
    planned = [s.file for s in consumer.design.rtl.sources]
    assert [f.name for f in planned] == ["mkTop.v"] and not planned[0].exists()
    results = results_of(tmp_path, "yosys")
    assert results["outcome"] == "failed"
    assert "mkTop.v" not in {f.name for f in consumer.run_path.iterdir()}


def test_a_missing_bsc_names_why_it_was_needed(tmp_path, monkeypatch) -> None:
    """Without bsc, `yosys` on a Bluespec design fails as a producer failure that says why bsc
    was launched."""
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    design = Design.from_file(write_design(tmp_path / "d", flows={"yosys": {}}))
    the_runner = runner(tmp_path)
    with pytest.raises(FlowDependencyFailure) as raised:
        the_runner.run_flow("yosys", design)
    text = str(raised.value)
    assert text.startswith("producer bsc failed (executable_not_found): ")
    assert text.endswith(
        "(bsc converts the design for yosys: the design has Bluespec sources, which yosys does "
        "not read)"
    )


def test_the_converter_of_a_design_is_launched_as_a_producer(tmp_path, monkeypatch) -> None:
    """The converter is launched for a consumer that has no flow yet (`consumer=`), and it is a
    producer in every way that matters: it refreshes no trace, delivers nothing to `--outputs-to`,
    and leaves a token for the hand-over."""
    events = Events()
    classes = consumer_flows(events)
    refreshes: dict[str, bool] = {}
    real = default_runner_module.check_trace

    def spying(run_path, expected, locate, *, refresh):
        refreshes[expected.flow] = refresh
        return real(run_path, expected, locate, refresh=refresh)

    monkeypatch.setattr(default_runner_module, "check_trace", spying)
    launch(tmp_path, classes)  # the first launch judges nothing: no trace yet
    launch(tmp_path, classes)
    converter, reader = classes
    assert refreshes == {converter.name: False, reader.name: True}

    seen: list[tuple[str | None, bool]] = []
    launch_flow = DefaultRunner.launch_flow

    def recording(self, flow_class, design, settings, **kwargs):
        seen.append((kwargs.get("consumer"), kwargs.get("depender") is not None))
        return launch_flow(self, flow_class, design, settings, **kwargs)

    monkeypatch.setattr(DefaultRunner, "launch_flow", recording)
    launch(tmp_path, classes)
    assert seen == [(None, False), (reader.name, False)]
