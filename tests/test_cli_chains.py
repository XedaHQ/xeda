"""`xeda run a+b+c`: chains on the command line, the nodes and the inputs of their records in
`--json`, dry-run parity, failure documents with the nodes that did not run, and the commands
that refuse a chain."""

import json

import pytest
import yaml
from click.testing import CliRunner

from xeda.design import DesignFileParseError
from xeda.cli import cli
from xeda.flow import FlowSettingsException
from xeda.project import Project

from .scoping import node_settings
from .flow_registry import preserved_flow_registry
from . import io_flows  # noqa: F401 - registers the declared test flows

pytestmark = pytest.mark.python_compat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    with preserved_flow_registry():
        monkeypatch.chdir(tmp_path)
        yield


def _design(tmp_path, flows=None, name="design.yaml"):
    (tmp_path / "in.txt").write_text("bound\n")
    sections = {"t_input_maker": {"input_file": "$DESIGN_ROOT/in.txt"}, **(flows or {})}
    path = tmp_path / name
    path.write_text(yaml.safe_dump({"name": "demo", "rtl": {"sources": []}, "flows": sections}))
    return path


def _xeda(*args):
    return CliRunner().invoke(cli, [str(arg) for arg in args])


def _json(*args):
    result = _xeda(*args, "--json")
    return result, json.loads(result.stdout)


# -------------------------------------------------------------------------------------- running


def _nodes(document):
    return [(n["name"], n["flow"], n["outcome"], n["disposition"]) for n in document["nodes"]]


@pytest.mark.parametrize("spelling", ["t_input_maker+t_taker", "t_input_maker.made+t_taker"])
def test_a_chain_runs_its_last_flow_and_reports_every_node(tmp_path, spelling):
    design = _design(tmp_path)
    result, document = _json("run", spelling, design)
    assert result.exit_code == 0, result.output
    assert document["outcome"] == "passed" and document["roots"] == ["t_taker"]
    assert document["spec"]["flow"] == spelling
    nodes = document["nodes"]
    assert _nodes(document) == [
        ("t_input_maker", "t_input_maker", "passed", "ran"),
        ("t_taker", "t_taker", "passed", "ran"),
    ]
    assert nodes[1]["record"]["metrics"]["read"] == "bound\n"
    (made,) = nodes[1]["record"]["inputs"]
    assert (made["name"], made["origin"], made["producer"], made["output"]) == (
        "made",
        "producer",
        "t_input_maker",
        "made",
    )
    assert nodes[0]["record"]["inputs"] == []
    again, second = _json("run", spelling, design)
    assert [n["disposition"] for n in second["nodes"]] == ["reused", "reused"]


def test_a_single_flow_reports_the_producers_it_needed(tmp_path):
    result, document = _json("run", "t_taker", _design(tmp_path))
    assert result.exit_code == 0, result.output
    assert document["roots"] == ["t_taker"]
    assert document["nodes"][-1]["record"]["metrics"]["read"] == "made\n"
    assert [n["flow"] for n in document["nodes"]] == ["t_maker", "t_taker"]
    assert document["nodes"][1]["record"]["inputs"][0]["producer"] == "t_maker"


def test_dash_s_sets_the_last_flow_and_a_qualified_one_any_node_of_the_chain(tmp_path):
    (tmp_path / "other.txt").write_text("other\n")
    result, document = _json(
        "run",
        "t_input_maker+t_taker",
        _design(tmp_path),
        "-s",
        "t_taker.nthreads=1",
        f"t_input_maker.input_file={tmp_path / 'other.txt'}",
    )
    assert result.exit_code == 0, result.output
    assert document["nodes"][-1]["record"]["metrics"]["read"] == "other\n"
    settings = json.loads((tmp_path / "xeda_run/demo/t_taker/settings.json").read_text())
    assert settings["flow_settings"]["nthreads"] == 1
    bad, failure = _json("run", "t_input_maker+t_taker", _design(tmp_path), "-s", "t_maker.text=x")
    assert bad.exit_code == 0, "a displaced default producer is addressable, and unused"
    unknown, failure = _json(
        "run", "t_input_maker+t_taker", _design(tmp_path), "-s", "t_fork.label=x"
    )
    assert unknown.exit_code == 2 and "names no node of this run" in failure["error"]["message"]
    assert failure["outcome"] is None and failure["nodes"] == []


def test_run_help_explains_chains_with_yaml_examples():
    import click

    text = click.unstyle(_xeda("run", "--help").output)
    assert "FLOW[.OUTPUT][+FLOW[.OUTPUT]...]" in text
    for phrase in ("binding each preceding flow", "--dry-run", "design.yaml"):
        assert phrase in " ".join(text.split()), phrase
    # the chain text above the option list shows YAML only; `--xedaproject` names `.toml` among
    # the project files it discovers
    assert ".toml" not in text.split("Options:")[0]


# -------------------------------------------------------------------------------------- dry run


def test_a_dry_run_of_a_chain_shows_the_graph_that_then_runs(tmp_path):
    design = _design(tmp_path)
    dry, planned = _json("run", "t_input_maker+t_taker", design, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert planned["spec"]["flow"] == "t_input_maker+t_taker" and planned["roots"] == ["t_taker"]
    assert not (tmp_path / "xeda_run").exists()
    text = _xeda("run", "t_input_maker+t_taker", design, "--dry-run").output
    assert "made <- t_input_maker.made (bound by the chain: position 1 (" in text
    ran, document = _json("run", "t_input_maker+t_taker", design)
    assert [(n["name"], n["run_dir"]) for n in document["nodes"]] == [
        (n["name"], n["run_dir"]) for n in planned["nodes"]
    ]
    assert [[i["name"] for i in n["record"]["inputs"]] for n in document["nodes"]] == [
        [i["name"] for i in n["inputs"]] for n in planned["nodes"]
    ]


# ------------------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    "chain, code, message",
    [
        ("+t_taker", "invalid_chain", "element 1 is empty"),
        ("t_input_maker+", "invalid_chain", "element 2 is empty"),
        ("t_input_maker++t_taker", "invalid_chain", "element 2 is empty"),
        ("t_input_maker+t_takr", "flow_not_found", "t_takr"),
        ("t_taker+t_taker", "repeated_flow", "appears more than once"),
        ("t_input_maker.nope+t_taker", "unknown_output", "no output `nope`"),
        ("t_taker+t_input_maker", "no_edge", "takes no required input"),
    ],
)
def test_a_malformed_chain_is_one_json_error_document_and_plans_nothing(
    tmp_path, chain, code, message
):
    result, document = _json("run", chain, _design(tmp_path))
    assert result.exit_code == 2, result.output
    assert document["outcome"] is None and message in document["error"]["message"]
    assert document["error"]["code"] == code and document["nodes"] == []
    assert document["spec"] is None, "the command line gave no request to report"
    assert not (tmp_path / "xeda_run").exists()
    text = _xeda("run", chain, _design(tmp_path))
    assert text.exit_code == 2 and message in text.output


def test_a_failed_run_reports_the_nodes_that_did_not_run(tmp_path, monkeypatch):
    """`t_left` fails (it writes no output): `t_join` is blocked and writes nothing, and the
    planned node never entered, `t_right`, is listed as not run."""
    design = _design(tmp_path)
    from .io_flows import TLeft

    monkeypatch.setattr(TLeft, "run", lambda self: None)
    result, document = _json("run", "t_fork.a+t_left", design)
    assert result.exit_code == 1 and document["roots"] == ["t_left"]
    assert [(n["name"], n["outcome"]) for n in document["nodes"]] == [
        ("t_fork", "passed"),
        ("t_left", "failed"),
    ]
    result, document = _json("run", "t_join", design)
    assert result.exit_code == 1, result.output
    assert document["outcome"] == "failed" and document["roots"] == ["t_join"]
    assert document["error"]["node"] == "t_left"
    states = {n["name"]: n["outcome"] for n in document["nodes"]}
    assert states == {
        "t_fork": "passed",  # another demand on its optional outputs: other settings
        "t_left": "failed",
        "t_join": "blocked",
        "t_right": "skipped",
    }
    nodes = {n["name"]: n for n in document["nodes"]}
    assert nodes["t_join"]["blocked_by"] == "t_left" and nodes["t_join"]["record"] is None
    assert nodes["t_right"]["run_dir"].endswith("t_right") and nodes["t_right"]["record"] is None
    assert not (tmp_path / "xeda_run/demo/t_join/results.json").exists()
    assert result.stdout.count('"launch_id"') == 1, "one document"


# ------------------------------------------------------------------------------------- refusals


def _dse_runner(tmp_path):
    from xeda.flow_runner.dse import Dse

    from .test_dse_run import _DeclaredOptimizer

    return Dse(_DeclaredOptimizer, run_root=tmp_path / "mirror", variations={}, max_workers=1)


def test_a_chain_given_to_dse_fetches_no_git_dependency(tmp_path, monkeypatch):
    """The refusal needs only the flow name, so it comes before the
    design is loaded, which could fetch a git dependency into the run root first."""
    fetched = _fetches(monkeypatch)
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("t_input_maker+t_taker", design)
    assert not fetched and not (tmp_path / "mirror").exists()


_MADE = "t_input_maker.made"
#: how a binding of the requested node reaches a launcher: `-s <node>.`, a table of nodes, the API
_BINDING_SPELLINGS = {
    "dotted -s": {"flow_settings": ["t_taker.inputs.made=" + _MADE]},
    "table of nodes": {"flow_settings": {"t_taker": {"inputs": {"made": _MADE}}}},
    "api": {"flow_overrides": {"inputs": {"made": _MADE}}},
}


@pytest.mark.parametrize("spelling", sorted(_BINDING_SPELLINGS))
def test_a_binding_of_the_requested_node_given_to_dse_fetches_no_git_dependency(
    tmp_path, monkeypatch, spelling
):
    """The requested node is always reached, so its explicit binding is refused with no design
    loaded at all: nothing is fetched and no run root is made."""
    fetched = _fetches(monkeypatch)
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("t_taker", design, **_BINDING_SPELLINGS[spelling])
    assert not fetched and not (tmp_path / "mirror").exists()


def test_a_project_binding_of_the_requested_node_given_to_dse_fetches_no_git_dependency(
    tmp_path, monkeypatch
):
    fetched = _fetches(monkeypatch)
    design = _git_dependency_design(tmp_path)
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump(
            {
                "designs": [{"file": design.name}],
                "flows": {"t_taker": {"inputs": {"made": _MADE}}},
            }
        )
    )
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run(
            "t_taker", Project.load(tmp_path / "xedaproject.yaml").design("d")
        )
    assert not fetched and not (tmp_path / "mirror").exists()


def test_a_saved_binding_of_another_node_is_not_refused_before_the_design_loads(tmp_path):
    """Only the requested node is reached by construction; whether any other node is depends on
    the resolved graph, so a binding saved for one is left to the check that has the plan."""
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump({"flows": {"t_input_maker": {"inputs": {"made": _MADE}}}})
    )
    with pytest.raises(DesignFileParseError):
        _dse_runner(tmp_path).run(
            "t_taker",
            tmp_path / "missing.yaml",
            flow_settings=node_settings("t_taker", ["flows.t_input_maker.inputs.made=" + _MADE]),
        )


def test_a_chain_given_to_dse_is_refused_even_for_a_missing_design(tmp_path):
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("t_input_maker+t_taker", tmp_path / "missing.yaml")
    assert not (tmp_path / "mirror").exists()


def _fetches(monkeypatch):
    """The calls that resolve or fetch a Git dependency: any call is a mistake."""
    import xeda.flow_runner.dependencies as dependencies

    calls: list[object] = []
    for name in ("resolve_reference", "fetch"):
        monkeypatch.setattr(dependencies, name, lambda *args, **kwargs: calls.append(args))
    return calls


def _git_dependency_design(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = tmp_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "dependencies": ["git+https://example.com/u/lib.git#lib.yaml"],
                "rtl": {"sources": ["top.v"], "top": "top"},
            }
        )
    )
    return design


@pytest.mark.parametrize(
    "request_text, error",
    [
        ("t_input_maker+t_input_maker", "appears more than once"),
        ("t_input_maker+", "is empty"),
        ("no_such_flow", "no_such_flow"),
    ],
)
def test_a_malformed_request_is_refused_before_a_git_dependency_is_fetched(
    tmp_path, monkeypatch, request_text, error
):
    """A request that cannot be parsed is refused on the flow text alone, as the command line
    does, not after the design's git dependency was fetched into the run root."""
    from xeda.flow_runner.default_runner import FlowLauncher

    fetched = _fetches(monkeypatch)
    with pytest.raises(Exception, match=error):
        FlowLauncher(tmp_path / "run").run(request_text, _git_dependency_design(tmp_path))
    assert not fetched and not (tmp_path / "run").exists()


def test_a_malformed_request_is_refused_even_for_a_missing_design(tmp_path):
    from xeda.flow_runner.default_runner import FlowLauncher

    with pytest.raises(FlowSettingsException, match="appears more than once"):
        FlowLauncher(tmp_path / "run").run("t_input_maker+t_input_maker", tmp_path / "missing.yaml")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "command",
    [
        ("dse", "t_input_maker+t_taker", "design.yaml"),
        ("scrub", "t_input_maker+t_taker", "demo"),
        ("list-settings", "t_input_maker+t_taker"),
        ("list-results", "t_input_maker+t_taker"),
    ],
)
def test_other_commands_take_one_flow_and_say_chains_are_for_run(tmp_path, command):
    _design(tmp_path)
    result = _xeda(*command)
    assert result.exit_code == 2, result.output
    assert "chain" in result.output and "xeda run" in result.output
    assert not (tmp_path / "xeda_run").exists()


# --------------------------------------------------------------------------- YAML project files


def test_an_explicit_yaml_project_binds_below_the_design_and_names_its_file(tmp_path):
    design = _design(tmp_path)
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "designs": [{"file": design.name}],
                "flows": {"t_taker": {"inputs": {"made": "t_input_maker.made"}}},
            }
        )
    )
    result, document = _json(
        "run", "t_taker", "--design-name", "demo", "--xedaproject", project, "--dry-run"
    )
    assert result.exit_code == 0, result.output
    (made,) = document["nodes"][-1]["inputs"]
    assert made["producer"] == "t_input_maker" and "saved binding" in made["reason"]
    assert str(project) in made["reason"]
    # the design's own binding wins over the project's
    over = _design(tmp_path, {"t_taker": {"inputs": {"made": "t_maker"}}}, "over.yaml")
    other = tmp_path / "other_project.yaml"
    other.write_text(
        yaml.safe_dump(
            {
                "designs": [{"file": over.name}],
                "flows": {"t_taker": {"inputs": {"made": "t_input_maker.made"}}},
            }
        )
    )
    selected = ("--design-name", "demo", "--xedaproject", other, "--dry-run")
    result, document = _json("run", "t_taker", *selected)
    (made,) = document["nodes"][-1]["inputs"]
    assert made["producer"] == "t_maker" and str(over) in made["reason"]
    # and a chain over both, saying what it replaced
    result, document = _json("run", "t_input_maker+t_taker", *selected)
    (made,) = document["nodes"][-1]["inputs"]
    assert "bound by the chain:" in made["reason"] and made["reason"].count("overriding") == 1
    assert str(other) in made["reason"] and str(over) in made["reason"]


# ------------------------------------------------------------------------- listing and follow


def _listed():
    result = CliRunner().invoke(cli, ["list-flows", "--json"])
    assert result.exit_code == 0, result.output
    return {flow["name"]: flow for flow in json.loads(result.stdout)}


def _real(edges):
    """The product's own relations: the registered test flows are not part of its graph."""
    return [edge for edge in edges if not edge["flow"].startswith("t_")]


def _edge(flow, binds=(), target_dependent=False, through=(), output=None):
    """One relation of `can_precede` or `can_follow`. Every relation has the same keys: `through`
    lists the flows that the link passes through (none for a direct link), and each item of
    `binds` is `(flow, input, output)`, with the flow whose input it binds."""
    return {
        "flow": flow,
        "output": output,
        "through": list(through),
        "binds": [{"flow": f, "input": i, "output": o} for f, i, o in binds],
        "target_dependent": target_dependent,
    }


def test_list_flows_json_adds_requiredness_and_follow_relations():
    flows = _listed()
    nextpnr = flows["nextpnr"]
    inputs = {i["name"]: i for i in nextpnr["inputs"]}
    assert (inputs["netlist"]["required"], inputs["netlist"]["optional"]) == (True, False)
    # optional many: not required, and an empty list is allowed
    assert inputs["constraints"]["cardinality"] == "many"
    assert (inputs["constraints"]["required"], inputs["constraints"]["optional"]) == (False, True)
    # the existing keys are all still there
    assert {"name", "types", "cardinality", "producer", "output", "description"} <= set(
        inputs["netlist"]
    )
    # the family-union configuration is a target-dependent hint
    assert _real(nextpnr["can_follow"]) == [
        _edge("yosys_fpga", [("nextpnr", "netlist", "netlist")]),
        # the converter's design goes into the synthesis, which the link passes through
        _edge("bsc", [("yosys_fpga", "design", "design")], through=["yosys_fpga"]),
    ]
    assert _real(nextpnr["can_precede"]) == [
        _edge("fpga_pack", [("fpga_pack", "config", "config")], True),
        # the loader reads the packed bitstream: the link passes through the packer
        _edge("openfpgaloader", [("fpga_pack", "config", "config")], True, through=["fpga_pack"]),
    ]
    # only the converter comes before the synthesis: it makes the design that the synthesis reads
    assert _real(flows["yosys_fpga"]["can_follow"]) == [
        _edge("bsc", [("yosys_fpga", "design", "design")])
    ]
    assert _real(flows["fpga_pack"]["can_precede"]) == [
        _edge("openfpgaloader", [("openfpgaloader", "bitstream", "bitstream")])
    ]
    # a link that skips a flow is listed after the direct ones, with the flows it passes through
    assert _real(flows["yosys_fpga"]["can_precede"]) == [
        _edge("nextpnr", [("nextpnr", "netlist", "netlist")]),
        _edge("vivado_impl", [("vivado_impl", "netlist", "netlist_edif")]),
        _edge("fpga_pack", [("nextpnr", "netlist", "netlist")], through=["nextpnr"]),
        _edge(
            "openfpgaloader",
            [("nextpnr", "netlist", "netlist")],
            through=["nextpnr", "fpga_pack"],
        ),
    ]
    assert _real(flows["fpga_pack"]["can_follow"]) == [
        _edge("nextpnr", [("fpga_pack", "config", "config")], True),
        _edge("bsc", [("yosys_fpga", "design", "design")], through=["yosys_fpga", "nextpnr"]),
        _edge("yosys_fpga", [("nextpnr", "netlist", "netlist")], through=["nextpnr"]),
    ]


def test_list_flows_json_shows_an_action_and_a_flow_without_declared_io_by_their_boundary():
    flows = _listed()
    loader = flows["openfpgaloader"]
    assert loader["action_reason"] == "it programs a device"
    assert loader["can_precede"] == []
    assert [e["flow"] for e in _real(loader["can_follow"])] == [
        "fpga_pack",
        "vivado_alt_synth",
        "vivado_impl",
        "vivado_synth",
        "bsc",
        "nextpnr",
        "yosys_fpga",
    ]
    assert flows["nextpnr"]["action_reason"] is None
    # `bsc` makes the design that the flows reading Verilog take; nothing makes the design of it
    bsc = flows["bsc"]
    assert [(i["name"], i["value"]) for i in bsc["inputs"]] == [("design", "design")]
    assert [(o["name"], o["value"]) for o in bsc["outputs"]] == [("design", "design")]
    assert bsc["can_follow"] == [] and bsc["can_precede"] and bsc["action_reason"] is None
    # `bsc_sim` reads Bluespec itself and makes nothing: no chain
    bsc_sim = flows["bsc_sim"]
    assert bsc_sim["can_follow"] == bsc_sim["can_precede"] == []
    # only flows that declare inputs or outputs take part
    for flow in flows.values():
        for edge in flow["can_follow"] + flow["can_precede"]:
            other = flows[edge["flow"]]
            assert other["inputs"] or other["outputs"]


def test_list_flows_json_qualifies_an_ambiguous_follower():
    producer = _listed()["t_chain_producer"]
    ambiguous = [e for e in producer["can_precede"] if e["flow"] == "t_chain_ambiguous_consumer"]
    assert [e["output"] for e in ambiguous] == ["json_a", "json_b"]
    assert all(
        e["binds"]
        == [{"flow": "t_chain_ambiguous_consumer", "input": "netlist", "output": e["output"]}]
        for e in ambiguous
    )
    assert all(e["through"] == [] for e in ambiguous)
    plain = [e for e in producer["can_precede"] if e["flow"] == "t_chain_ambiguous_default"]
    assert [e["output"] for e in plain] == [None]


def test_list_flows_table_names_takes_makes_followers_and_the_boundary(monkeypatch):
    from xeda.console import console

    monkeypatch.setattr(console, "width", 400)
    result = CliRunner().invoke(cli, ["list-flows"])
    assert result.exit_code == 0, result.output
    rows: dict[str, str] = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith(("├", "┡", "└")):
            current = None  # a rule between rows
        elif line.startswith("│"):
            first = line.split("│")[1].strip()
            if current is None and first:
                current = first.split()[0]
                rows[current] = ""
            if current is not None:
                rows[current] += " ".join(line.replace("│", " ").split()) + "\n"
    for heading in ("Takes", "Makes", "Can be followed by"):
        assert heading in result.stdout
    assert "netlist (JsonNetlist)" in rows["nextpnr"]
    assert "[constraints...] (Lpf/Pcf/Pdc/Xdc)" in rows["nextpnr"]
    assert "config (EcpConfig/IceAsc/Fasm)" in rows["nextpnr"]
    assert "fpga_pack (depends on the target)" in rows["nextpnr"]
    # a follower that a link reaches through other flows names them, so it never reads as direct
    assert "openfpgaloader (through fpga_pack) (depends on the target)" in rows["nextpnr"]
    assert "fpga_pack (through nextpnr)" in rows["yosys_fpga"]
    assert "openfpgaloader (through nextpnr, fpga_pack)" in rows["yosys_fpga"]
    assert "nextpnr (through" not in rows["yosys_fpga"]
    assert "it programs a device" in rows["openfpgaloader"]
    assert "ends a chain" in rows["openfpgaloader"]
    assert "declared" not in rows["bsc"] and "runs alone" not in rows["bsc"]


def test_action_reason_is_class_metadata_the_dry_run_shows_apart_from_what_a_run_would_do(
    tmp_path,
):
    """`action_reason` is why a flow can only end a chain, a fact of its class; what a run would do
    with the flow is the freshness of the node, a separate answer (the flow here does not
    override `always_runs`, so it is judged like any other)."""
    design = tmp_path / "design.yaml"
    design.write_text(yaml.safe_dump({"name": "demo", "rtl": {"sources": []}}))
    request = "t_chain_simple_producer+t_chain_action"
    result, document = _json("run", request, design, "--dry-run")
    assert result.exit_code == 0, result.output
    nodes = {node["name"]: node for node in document["nodes"]}
    assert nodes["t_chain_action"]["action"] is True
    assert nodes["t_chain_simple_producer"]["action"] is False
    assert nodes["t_chain_action"]["state"] == "stale"
    assert nodes["t_chain_action"]["reason"] == "no completed previous run"
    text = _xeda("run", request, design, "--dry-run").output
    assert "always runs: performs a test action" in text
    assert text.count("always runs") == 1


def test_a_refused_chain_advertises_only_validated_requests(tmp_path):
    result = _xeda("run", "yosys_fpga.netlist_edif+fpga_pack", _design(tmp_path))
    assert result.exit_code == 2
    assert "Did you mean `yosys_fpga+fpga_pack` or `yosys_fpga.netlist+fpga_pack`?" in (
        result.stderr
    )
    result, document = _json("run", "yosys_fpga.netlist_edif+fpga_pack", _design(tmp_path))
    assert result.exit_code == 2 and document["outcome"] is None
    assert "yosys_fpga.netlist+fpga_pack" in document["error"]["message"]
    nothing = _xeda("run", "t_chain_simple_consumer+t_chain_producer", _design(tmp_path))
    assert nothing.exit_code == 2 and "Did you mean" not in nothing.stderr


def test_the_request_lists_the_flows_a_link_passes_through(tmp_path):
    result, document = _json("run", "t_route_a+t_route_c", _design(tmp_path), "--dry-run")
    assert result.exit_code == 0, result.output
    assert document["spec"]["flow"] == "t_route_a+t_route_c"
    assert [node["name"] for node in document["nodes"]] == [
        "t_route_a",
        "t_route_b",
        "t_route_c",
    ]
    (bound,) = document["nodes"][1]["inputs"]
    assert (bound["producer"], bound["output"]) == ("t_route_a", "x")
    assert "bound by the chain:" in bound["reason"]
    (taken,) = document["nodes"][2]["inputs"]
    assert not taken["reason"].startswith("bound"), "the other edges stay default edges"


def test_a_pass_through_link_collides_with_a_binding_of_its_input_on_the_command_line(tmp_path):
    result, document = _json(
        "run",
        "t_route_a+t_route_c",
        _design(tmp_path),
        "-s",
        "t_route_b.inputs.x=t_route_a.x",
        "--dry-run",
    )
    assert result.exit_code == 2, result.output
    message = document["error"]["message"]
    assert (
        "The chain position 1 (t_route_a) -> 2 (t_route_c), through t_route_b, binds input 'x' "
        "of t_route_b; "
    ) in message
    assert "t_route_b.inputs.x also binds that input." in message


def test_a_binding_that_cuts_the_graph_is_refused(tmp_path):
    result, document = _json(
        "run",
        "t_route_a+t_route_c",
        _design(tmp_path),
        "-s",
        "t_route_c.inputs.y=t_maker.made",
        "--dry-run",
    )
    assert result.exit_code == 2, result.output
    message = document["error"]["message"]
    assert (
        "The chain link `t_route_a+t_route_c` passes through `t_route_b`, which the binding of "
        "`t_route_c.y` at "
    ) in message
    assert "t_route_c.inputs.y takes" in message and message.endswith("out of the plan.")
    # the same binding without the chain plans, and leaves `t_route_b` out
    plain, planned = _json(
        "run",
        "t_route_c",
        _design(tmp_path),
        "-s",
        "t_route_c.inputs.y=t_maker.made",
        "--dry-run",
    )
    assert plain.exit_code == 0, plain.output
    assert [node["name"] for node in planned["nodes"]] == ["t_maker", "t_route_c"]


def test_an_unknown_input_or_output_name_in_a_binding_gets_close_matches(tmp_path):
    def message(binding):
        design = _design(tmp_path, {"t_taker": {"inputs": binding}})
        result, document = _json("run", "t_taker", design, "--dry-run")
        assert result.exit_code == 2 and document["outcome"] is None, result.output
        return document["error"]["message"]

    assert "Did you mean `made`?" in message({"mde": "t_maker"})
    assert "Did you mean `made`?" in message({"made": "t_maker.mde"})
    # nothing close: say what is declared instead
    assert "It declares: `made`." in message({"zzz": "t_maker"})
    assert "It declares: `made`." in message({"made": "t_maker.zzz"})


def _fan_design(tmp_path, sources=(), flows=None):
    """A design for the fan: `t_fan_head` feeds `t_fan_base`, which feeds `t_fan_left` and
    `t_fan_right`, and both feed `t_fan_tail`."""
    path = tmp_path / "fan.yaml"
    path.write_text(
        yaml.safe_dump({"name": "fan", "rtl": {"sources": list(sources)}, "flows": flows or {}})
    )
    return path


def test_a_link_that_binds_one_flow_lists_every_flow_it_passes_through(tmp_path):
    result, document = _json("run", "t_fan_head+t_fan_tail", _fan_design(tmp_path), "--dry-run")
    assert result.exit_code == 0, result.output
    assert document["spec"]["flow"] == "t_fan_head+t_fan_tail"
    assert [node["name"] for node in document["nodes"]] == [
        "t_fan_head",
        "t_fan_base",
        "t_fan_left",
        "t_fan_right",
        "t_fan_tail",
    ]


def test_a_source_that_takes_an_unbound_flow_out_of_the_plan_is_refused(tmp_path):
    (tmp_path / "left.txt").write_text("")
    design = _fan_design(tmp_path, [{"file": "left.txt", "type": "Data"}])
    result, document = _json("run", "t_fan_head+t_fan_tail", design, "--dry-run")
    assert result.exit_code == 2, result.output
    assert (
        "The chain link `t_fan_head+t_fan_tail` passes through `t_fan_left`, which the source "
        "of `t_fan_tail.left` takes out of the plan."
    ) in document["error"]["message"]
    # the request for the tail alone plans without `t_fan_left`
    plain, planned = _json("run", "t_fan_tail", design, "--dry-run")
    assert plain.exit_code == 0, plain.output
    assert "t_fan_left" not in [node["name"] for node in planned["nodes"]]


def test_a_binding_that_takes_an_unbound_flow_out_of_the_plan_is_refused(tmp_path):
    result, document = _json(
        "run",
        "t_fan_head+t_fan_tail",
        _fan_design(tmp_path, flows={"t_maker": {}}),
        "-s",
        "t_fan_tail.inputs.left=t_maker.made",
        "--dry-run",
    )
    assert result.exit_code == 2, result.output
    message = document["error"]["message"]
    assert (
        "The chain link `t_fan_head+t_fan_tail` passes through `t_fan_left`, which the binding "
        "of `t_fan_tail.left` at "
    ) in message
    assert "t_fan_tail.inputs.left takes" in message and message.endswith("out of the plan.")


def test_a_failed_chain_exits_1_with_the_cause(tmp_path, monkeypatch):
    """The producer fails: the document and the exit status say `failed` with the error of the
    producer, and the consumer is `blocked` by it."""
    from .io_flows import TLeft

    monkeypatch.setattr(TLeft, "run", lambda self: None)
    result, document = _json("run", "t_join", _design(tmp_path))
    assert result.exit_code == 1
    assert document["outcome"] == "failed" and document["roots"] == ["t_join"]
    assert document["error"]["node"] == "t_left"
    assert document["error"]["code"] == "missing_output"
    nodes = {n["name"]: n for n in document["nodes"]}
    assert nodes["t_left"]["record"]["error"] == document["error"]
    assert (nodes["t_join"]["outcome"], nodes["t_join"]["blocked_by"]) == ("blocked", "t_left")
