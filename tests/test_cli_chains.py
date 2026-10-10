"""`xeda run a+b+c`: chains on the command line, the structured request and per-node inputs of
`--json`, dry-run parity, failure documents with the nodes that did not run, and the commands
that refuse a chain."""

import json

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.design import DesignFileParseError
from xeda.cli import cli
from xeda.flow import FlowSettingsException, registered_flows
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.remote import RemoteRunner

from . import io_flows  # noqa: F401 - registers the declared test flows

pytestmark = pytest.mark.python_compat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    before = registered_flows.copy()
    monkeypatch.chdir(tmp_path)
    yield
    registered_flows.clear()
    registered_flows.update(before)


def _design(tmp_path, flows=None, name="design.yaml"):
    (tmp_path / "in.txt").write_text("bound\n")
    sections = {"__input_maker": {"input_file": "$DESIGN_ROOT/in.txt"}, **(flows or {})}
    path = tmp_path / name
    path.write_text(yaml.safe_dump({"name": "demo", "rtl": {"sources": []}, "flows": sections}))
    return path


def _xeda(*args):
    return CliRunner().invoke(cli, [str(arg) for arg in args])


def _json(*args):
    result = _xeda(*args, "--json")
    return result, json.loads(result.stdout)


REQUEST = [
    {"node": "__input_maker", "flow": "__input_maker", "output": None},
    {"node": "__taker", "flow": "__taker", "output": None},
]


# -------------------------------------------------------------------------------------- running


@pytest.mark.parametrize("spelling", ["__input_maker+__taker", "__input_maker.made+__taker"])
def test_a_chain_runs_its_last_flow_and_reports_every_node(tmp_path, spelling):
    design = _design(tmp_path)
    result, document = _json("run", spelling, design)
    assert result.exit_code == 0, result.output
    assert document["success"] and document["flow"] == "__taker"
    assert document["results"]["read"] == "bound\n"
    assert document["request"] == [
        {**REQUEST[0], "output": "made" if "." in spelling else None},
        REQUEST[1],
    ]
    nodes = document["nodes"]
    assert [(n["node"], n["flow"], n["state"]) for n in nodes] == [
        ("__input_maker", "__input_maker", "ran"),
        ("__taker", "__taker", "ran"),
    ]
    (made,) = nodes[1]["inputs"]
    assert (made["name"], made["producer"], made["output"]) == ("made", "__input_maker", "made")
    assert made["binding_origin"] == "chain" and nodes[0]["inputs"] == []
    again, second = _json("run", spelling, design)
    assert [n["state"] for n in second["nodes"]] == ["fresh", "fresh"]


def test_a_single_flow_keeps_its_document_and_gains_the_request(tmp_path):
    result, document = _json("run", "__taker", _design(tmp_path))
    assert result.exit_code == 0, result.output
    assert document["flow"] == "__taker" and document["results"]["read"] == "made\n"
    assert document["request"] == [{"node": "__taker", "flow": "__taker", "output": None}]
    assert [n["flow"] for n in document["nodes"]] == ["__maker", "__taker"]
    assert document["nodes"][1]["inputs"][0]["binding_origin"] is None


def test_dash_s_sets_the_last_flow_and_a_qualified_one_any_node_of_the_chain(tmp_path):
    (tmp_path / "other.txt").write_text("other\n")
    result, document = _json(
        "run",
        "__input_maker+__taker",
        _design(tmp_path),
        "-s",
        "verbose=1",
        f"flows.__input_maker.input_file={tmp_path / 'other.txt'}",
    )
    assert result.exit_code == 0, result.output
    assert document["results"]["read"] == "other\n"
    settings = json.loads((tmp_path / "xeda_run/demo/__taker/settings.json").read_text())
    assert settings["flow_settings"]["verbose"] == 1
    bad, failure = _json(
        "run", "__input_maker+__taker", _design(tmp_path), "-s", "flows.__maker.text=x"
    )
    assert bad.exit_code == 0, "a displaced default producer is addressable, and unused"
    unknown, failure = _json(
        "run", "__input_maker+__taker", _design(tmp_path), "-s", "flows.__fork.label=x"
    )
    assert unknown.exit_code == 1 and "names no flow of this run" in failure["error"]["message"]


def test_help_settings_of_a_chain_are_its_last_flow_s(tmp_path):
    result = _xeda("run", "__input_maker+__taker", "--help-settings", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["flow"] == "__taker"


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
    dry, planned = _json("run", "__input_maker+__taker", design, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert planned["dry_run"] and planned["flow"] == "__taker" and planned["request"] == REQUEST
    assert not (tmp_path / "xeda_run").exists()
    text = _xeda("run", "__input_maker+__taker", design, "--dry-run").output
    assert "made <- __input_maker.made (chain)" in text
    ran, document = _json("run", "__input_maker+__taker", design)
    assert [(n["node"], n["run_path"], n["inputs"]) for n in document["nodes"]] == [
        (n["name"], n["run_path"], n["inputs"]) for n in planned["plan"]["nodes"]
    ]


# ------------------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    "chain, message",
    [
        ("+__taker", "element 1 is empty"),
        ("__input_maker+", "element 2 is empty"),
        ("__input_maker++__taker", "element 2 is empty"),
        ("__input_maker+__takr", "__takr"),
        ("__taker+__taker", "appears more than once"),
        ("__input_maker.nope+__taker", "no output `nope`"),
        ("__taker+__input_maker", "takes no required input"),
    ],
)
def test_a_malformed_chain_is_one_json_error_document_and_plans_nothing(tmp_path, chain, message):
    result, document = _json("run", chain, _design(tmp_path))
    assert result.exit_code == 2, result.output
    assert document["success"] is False and message in document["error"]["message"]
    assert "plan" not in document and "nodes" not in document
    assert not (tmp_path / "xeda_run").exists()
    text = _xeda("run", chain, _design(tmp_path))
    assert text.exit_code == 2 and message in text.output


def test_a_failed_run_reports_the_nodes_that_did_not_run(tmp_path, monkeypatch):
    """`__left` fails (it writes no output): `__join` reports the failed dependency, and the
    planned node never entered, `__right`, is listed as not run."""
    design = _design(tmp_path)
    from .io_flows import _Left

    monkeypatch.setattr(_Left, "run", lambda self: None)
    result, document = _json("run", "__fork.a+__left", design)
    assert result.exit_code == 1 and document["flow"] == "__left"
    assert [(n["node"], n["state"]) for n in document["nodes"]] == [
        ("__fork", "ran"),
        ("__left", "failed"),
    ]
    result, document = _json("run", "__join", design)
    assert result.exit_code == 1, result.output
    assert document["success"] is False and document["flow"] == "__join"
    assert document["request"][-1]["flow"] == "__join"
    states = {n["node"]: n["state"] for n in document["nodes"]}
    assert states == {
        "__fork": "ran",  # another demand on its optional outputs: other settings
        "__left": "failed",
        "__join": "failed",
        "__right": "not run",
    }
    not_run = next(n for n in document["nodes"] if n["node"] == "__right")
    assert not_run["run_path"].endswith("__right") and not_run["inputs"][0]["producer"] == "__fork"
    assert json.loads((tmp_path / "xeda_run/demo/__join/results.json").read_text())["success"] is (
        False
    )
    assert result.stdout.count('"success"') == 1, "one document"


# ------------------------------------------------------------------------------------- refusals


def test_a_chain_is_refused_with_remote_before_anything_is_shipped(tmp_path, monkeypatch):
    connected = []
    monkeypatch.setattr(remote_module, "Connection", lambda **kwargs: connected.append(kwargs))
    design = _design(tmp_path)
    for extra, env in ((("--remote", "host"), {}), ((), {"XEDA_REMOTE": "host"})):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        result, document = _json("run", "__input_maker+__taker", design, *extra)
        assert result.exit_code != 0
        assert "local `xeda run` requests" in document["error"]["message"]
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(
            Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"}),
            "__input_maker+__taker",
            "host",
        )
    assert not connected and not (tmp_path / "mirror").exists()


def test_a_chain_given_to_a_remote_runner_clones_no_git_dependency(tmp_path, monkeypatch):
    """The refusal needs only the flow name, so it comes before the design is loaded: a design
    with a git dependency would otherwise be cloned into the run root first."""
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
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
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(design, "__input_maker+__taker", "host")
    assert not cloned and not (tmp_path / "mirror").exists()


def test_a_chain_given_to_a_remote_runner_is_refused_even_for_a_missing_design(tmp_path):
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(
            tmp_path / "missing.yaml", "__input_maker+__taker", "host"
        )
    assert not (tmp_path / "mirror").exists()


def _dse_runner(tmp_path):
    from xeda.flow_runner.dse import Dse

    from .test_dse_run import _DeclaredOptimizer

    return Dse(_DeclaredOptimizer, run_root=tmp_path / "mirror", variations={}, max_workers=1)


def test_a_chain_given_to_dse_clones_no_git_dependency(tmp_path, monkeypatch):
    """As for a remote runner: the refusal needs only the flow name, so it comes before the
    design is loaded, which would clone a git dependency into the run root first."""
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("__input_maker+__taker", design)
    assert not cloned and not (tmp_path / "mirror").exists()


_MADE = "__input_maker.made"
#: how a binding of the requested node reaches a launcher: `-s`, `-s flows.<node>.`, the API
_BINDING_SPELLINGS = {
    "bare -s": {"flow_settings": ["inputs.made=" + _MADE]},
    "flow-qualified -s": {"flow_settings": ["flows.__taker.inputs.made=" + _MADE]},
    "api": {"flow_overrides": {"inputs": {"made": _MADE}}},
}


@pytest.mark.parametrize("spelling", sorted(_BINDING_SPELLINGS))
def test_a_binding_of_the_requested_node_given_to_dse_clones_no_git_dependency(
    tmp_path, monkeypatch, spelling
):
    """The requested node is always reached, so its explicit binding is refused with no design
    loaded at all: nothing is cloned and no run root is made."""
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("__taker", design, **_BINDING_SPELLINGS[spelling])
    assert not cloned and not (tmp_path / "mirror").exists()


def test_a_project_binding_of_the_requested_node_given_to_dse_clones_no_git_dependency(
    tmp_path, monkeypatch
):
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump({"flows": {"__taker": {"inputs": {"made": _MADE}}}})
    )
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("__taker", design)
    assert not cloned and not (tmp_path / "mirror").exists()


def test_a_saved_binding_of_another_node_is_not_refused_before_the_design_loads(tmp_path):
    """Only the requested node is reached by construction; whether any other node is depends on
    the resolved graph, so a binding saved for one is left to the check that has the plan."""
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump({"flows": {"__input_maker": {"inputs": {"made": _MADE}}}})
    )
    with pytest.raises(DesignFileParseError):
        _dse_runner(tmp_path).run(
            "__taker",
            tmp_path / "missing.yaml",
            flow_settings=["flows.__input_maker.inputs.made=" + _MADE],
        )


#: `run_remote` takes the command line's settings only (no `flow_overrides`): as `-s` items, as
#: the API's mapping of them
_REMOTE_BINDING_SPELLINGS = {
    "bare -s": ["inputs.made=" + _MADE],
    "flow-qualified -s": ["flows.__taker.inputs.made=" + _MADE],
    "mapping": {"inputs": {"made": _MADE}},
    "flow-qualified mapping": {"flows": {"__taker": {"inputs": {"made": _MADE}}}},
}


@pytest.mark.parametrize("spelling", sorted(_REMOTE_BINDING_SPELLINGS))
def test_a_binding_of_the_requested_node_given_to_a_remote_runner_clones_no_git_dependency(
    tmp_path, monkeypatch, spelling
):
    """As for dse: the requested node is always reached, so its explicit binding is refused with
    no design loaded: nothing is cloned and no mirror is made."""
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(
            design, "__taker", "host", flow_settings=_REMOTE_BINDING_SPELLINGS[spelling]
        )
    assert not cloned and not (tmp_path / "mirror").exists()


def test_a_project_binding_of_the_requested_node_given_to_a_remote_runner_clones_nothing(
    tmp_path, monkeypatch
):
    import git.repo

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump({"flows": {"__taker": {"inputs": {"made": _MADE}}}})
    )
    design = _git_dependency_design(tmp_path)
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        RemoteRunner(tmp_path / "mirror").run_remote(design, "__taker", "host")
    assert not cloned and not (tmp_path / "mirror").exists()


def test_a_saved_binding_of_another_node_is_not_refused_by_a_remote_runner_before_the_design(
    tmp_path,
):
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump({"flows": {"__input_maker": {"inputs": {"made": _MADE}}}})
    )
    with pytest.raises(DesignFileParseError):
        RemoteRunner(tmp_path / "mirror").run_remote(
            tmp_path / "missing.yaml",
            "__taker",
            "host",
            flow_settings=["flows.__input_maker.inputs.made=" + _MADE],
        )


@pytest.mark.parametrize(
    "request_text, settings",
    [
        ("__input_maker+__taker", None),
        ("__taker", ["inputs.made=" + _MADE]),
        ("__taker", {"flows": {"__taker": {"inputs": {"made": _MADE}}}}),
    ],
)
def test_dse_and_a_remote_runner_refuse_a_request_with_the_same_error(
    tmp_path, request_text, settings
):
    """One rule, one refusal: the same type and the same message from both entry points."""
    design = tmp_path / "missing.yaml"
    with pytest.raises(FlowSettingsException) as dse_error:
        _dse_runner(tmp_path).run(request_text, design, flow_settings=settings or [])
    with pytest.raises(FlowSettingsException) as remote_error:
        RemoteRunner(tmp_path / "mirror").run_remote(
            design, request_text, "host", flow_settings=settings
        )
    assert type(dse_error.value) is type(remote_error.value)
    assert str(dse_error.value) == str(remote_error.value)


def test_a_chain_given_to_dse_is_refused_even_for_a_missing_design(tmp_path):
    with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
        _dse_runner(tmp_path).run("__input_maker+__taker", tmp_path / "missing.yaml")
    assert not (tmp_path / "mirror").exists()


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
        ("__input_maker+__input_maker", "appears more than once"),
        ("__input_maker+", "is empty"),
        ("no_such_flow", "no_such_flow"),
    ],
)
def test_a_malformed_request_is_refused_before_a_git_dependency_is_cloned(
    tmp_path, monkeypatch, request_text, error
):
    """A request that cannot be parsed is refused on the flow text alone, as the command line
    does, not after the design's git dependency was cloned into the run root."""
    import git.repo

    from xeda.flow_runner.default_runner import FlowLauncher

    cloned = []
    monkeypatch.setattr(
        git.repo.Repo, "clone_from", staticmethod(lambda *args, **kwargs: cloned.append(args))
    )
    with pytest.raises(Exception, match=error):
        FlowLauncher(tmp_path / "run").run(request_text, _git_dependency_design(tmp_path))
    assert not cloned and not (tmp_path / "run").exists()


def test_a_malformed_request_is_refused_even_for_a_missing_design(tmp_path):
    from xeda.flow_runner.default_runner import FlowLauncher

    with pytest.raises(FlowSettingsException, match="appears more than once"):
        FlowLauncher(tmp_path / "run").run("__input_maker+__input_maker", tmp_path / "missing.yaml")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "command",
    [
        ("dse", "__input_maker+__taker", "--design", "design.yaml"),
        ("scrub", "__input_maker+__taker", "demo"),
        ("list-settings", "__input_maker+__taker"),
        ("list-results", "__input_maker+__taker"),
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
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump({"flows": {"__taker": {"inputs": {"made": "__input_maker.made"}}}})
    )
    design = _design(tmp_path)
    result, document = _json("run", "__taker", design, "--xedaproject", project, "--dry-run")
    assert result.exit_code == 0, result.output
    (made,) = document["plan"]["nodes"][-1]["inputs"]
    assert made["producer"] == "__input_maker" and made["binding_origin"] == "file"
    assert str(project) in made["binding_location"]
    # the design's own binding wins over the project's
    over = _design(tmp_path, {"__taker": {"inputs": {"made": "__maker"}}}, "over.yaml")
    result, document = _json("run", "__taker", over, "--xedaproject", project, "--dry-run")
    (made,) = document["plan"]["nodes"][-1]["inputs"]
    assert made["producer"] == "__maker" and str(over) in made["binding_location"]
    # and a chain over both, saying what it replaced
    result, document = _json(
        "run", "__input_maker+__taker", over, "--xedaproject", project, "--dry-run"
    )
    (made,) = document["plan"]["nodes"][-1]["inputs"]
    assert made["binding_origin"] == "chain" and len(made["overridden"]) == 2


# ------------------------------------------------------------------------- listing and follow


def _listed():
    result = CliRunner().invoke(cli, ["list-flows", "--json"])
    assert result.exit_code == 0, result.output
    return {flow["name"]: flow for flow in json.loads(result.stdout)}


def _real(edges):
    """The product's own relations: the registered test flows are not part of its graph."""
    return [edge for edge in edges if not edge["flow"].startswith("__")]


def _edge(flow, output=None, binds=(), target_dependent=False):
    return {
        "flow": flow,
        "output": output,
        "binds": [{"input": i, "output": o} for i, o in binds],
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
    assert _real(nextpnr["can_follow"]) == [_edge("yosys_fpga", None, [("netlist", "netlist")])]
    assert _real(nextpnr["can_precede"]) == [_edge("fpga_pack", None, [("config", "config")], True)]
    assert flows["yosys_fpga"]["can_follow"] == []
    assert _real(flows["fpga_pack"]["can_precede"]) == [
        _edge("openfpgaloader", None, [("bitstream", "bitstream")])
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
    ]
    assert flows["nextpnr"]["action_reason"] is None
    bsc = flows["bsc"]
    assert bsc["inputs"] == [] and bsc["outputs"] == []
    assert bsc["can_follow"] == bsc["can_precede"] == [] and bsc["action_reason"] is None
    # only flows that declare I/O take part: nothing here needs a design value or an artifact
    for flow in flows.values():
        for edge in flow["can_follow"] + flow["can_precede"]:
            other = flows[edge["flow"]]
            assert other["inputs"] or other["outputs"]


def test_list_flows_json_qualifies_an_ambiguous_follower():
    producer = _listed()["__chain_producer"]
    ambiguous = [e for e in producer["can_precede"] if e["flow"] == "__chain_ambiguous_consumer"]
    assert [e["output"] for e in ambiguous] == ["json_a", "json_b"]
    assert all(e["binds"] == [{"input": "netlist", "output": e["output"]}] for e in ambiguous)
    plain = [e for e in producer["can_precede"] if e["flow"] == "__chain_ambiguous_default"]
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
    assert "it programs a device" in rows["openfpgaloader"]
    assert "ends a chain" in rows["openfpgaloader"]
    assert "declared" not in rows["bsc"] and "runs alone" not in rows["bsc"]


def test_action_reason_is_class_metadata_the_dry_run_shows_without_running_flows(
    tmp_path, monkeypatch
):
    from xeda.flow import Flow

    def forbidden(self):
        raise AssertionError("a dry run never asks a flow whether it always runs")

    monkeypatch.setattr(Flow, "always_runs", forbidden)
    design = tmp_path / "design.yaml"
    design.write_text(yaml.safe_dump({"name": "demo", "rtl": {"sources": []}}))
    request = "__chain_simple_producer+__chain_action"
    result, document = _json("run", request, design, "--dry-run")
    assert result.exit_code == 0, result.output
    nodes = {node["name"]: node for node in document["plan"]["nodes"]}
    assert nodes["__chain_action"]["action_reason"] == "performs a test action"
    assert nodes["__chain_simple_producer"]["action_reason"] is None
    text = _xeda("run", request, design, "--dry-run").output
    assert "always runs: performs a test action" in text
    assert text.count("always runs") == 1


def test_a_refused_chain_advertises_only_validated_requests(tmp_path):
    result = _xeda("run", "nextpnr+openfpgaloader", _design(tmp_path))
    assert result.exit_code == 2
    assert "Did you mean `nextpnr+fpga_pack+openfpgaloader`?" in result.stderr
    result, document = _json("run", "nextpnr+openfpgaloader", _design(tmp_path))
    assert result.exit_code == 2 and document["success"] is False
    assert "nextpnr+fpga_pack+openfpgaloader" in document["error"]["message"]
    nothing = _xeda("run", "__chain_simple_consumer+__chain_producer", _design(tmp_path))
    assert nothing.exit_code == 2 and "Did you mean" not in nothing.stderr


def test_an_unknown_input_or_output_name_in_a_binding_gets_close_matches(tmp_path):
    def message(binding):
        design = _design(tmp_path, {"__taker": {"inputs": binding}})
        result, document = _json("run", "__taker", design, "--dry-run")
        assert result.exit_code == 1 and document["success"] is False, result.output
        return document["error"]["message"]

    assert "Did you mean `made`?" in message({"mde": "__maker"})
    assert "Did you mean `made`?" in message({"made": "__maker.mde"})
    # nothing close: say what is declared instead
    assert "It declares: `made`." in message({"zzz": "__maker"})
    assert "It declares: `made`." in message({"made": "__maker.zzz"})
