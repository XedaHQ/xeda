"""`xeda run a+b+c`: chains on the command line, the structured request and per-node inputs of
`--json`, dry-run parity, failure documents with the nodes that did not run, and the commands
that refuse a chain."""

import json

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import FlowSettingsException, registered_flows
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.remote import RemoteRunner

from . import io_flows  # noqa: F401 - registers the declared test flows


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
    assert ".toml" not in text


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
        ("__taker+__input_maker", "no compatible output"),
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
