"""The chain documentation is executable.

A document that teaches `xeda run a+b+c` and saved `inputs` bindings is only worth what its
examples do when someone types them. So the YAML fixtures in the maintained documents are
extracted and loaded, checked against the published design schema and the flows they configure,
and the documented commands are run against the real FPGA flows: planning needs no tool, and
execution goes through the process fakes of `tests/fake_tools` (never a real programmer; the
conftest guard stands behind that). Nothing here asserts that a document contains a sentence; each
test pins behavior a reader of the document relies on.

A fixture is a fenced `yaml` block (a `code-block:: yaml` in reStructuredText) whose first line is
a comment naming its file, `# routed_demo.yaml`. Fixtures of one name in several documents must
mean the same thing, so the human and agent documents cannot drift apart.
"""

import json
import re
import shlex
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.bindings import split_bindings
from xeda.flow_runner.chains import complete_request, parse_request
from xeda.introspect import all_flow_classes, design_schema, flow_info, settings_info

from . import tool_utils

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).parent.parent
FLOWS_RST = ROOT / "docs/flows.rst"
DESIGN_FILE_RST = ROOT / "docs/design-file.rst"
MACHINE_READABLE_RST = ROOT / "docs/machine-readable.rst"
AGENT = ROOT / "src/xeda/data/agent"
SKILL_MD = AGENT / "SKILL.md"
DESIGN_FILE_MD = AGENT / "references/design-file.md"

CHAIN = "yosys_fpga+nextpnr+fpga_pack"
CHAIN_SECTION = ("Flow chains and input bindings\n", "Open-source FPGA flow targets and tuning\n")
NO_EDGE = r"has no compatible output for a required input of|takes no required input"
FUTURE_MARKER = "# not available yet"

# ---------------------------------------------------------------------------------- the documents


def _blocks(path: Path) -> list[tuple[str, str]]:
    """Every fenced block of a document as (language, dedented text)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".rst":
        found = re.findall(r"^\.\. code-block:: (\w+)\n\n((?:(?:    .*)?\n)+)", text, re.M)
        return [(language, textwrap.dedent(body).rstrip("\n")) for language, body in found]
    return [
        (language, body.rstrip("\n"))
        for language, body in re.findall(r"^```(\w+)\n(.*?)\n```", text, re.M | re.S)
    ]


def _fixtures(path: Path) -> dict[str, str]:
    """The YAML fixtures of a document, by the file name their first line gives."""
    fixtures: dict[str, str] = {}
    for language, body in _blocks(path):
        named = re.match(r"# (\S+\.yaml)\b", body)
        if language == "yaml" and named:
            assert named[1] not in fixtures, f"{path.name} gives {named[1]} twice"
            fixtures[named[1]] = body
    return fixtures


def _section(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    return text[text.index(start) : text.index(end)]


DOCUMENTS = {
    "flows.rst": FLOWS_RST,
    "design-file.rst": DESIGN_FILE_RST,
    "SKILL.md": SKILL_MD,
    "design-file.md": DESIGN_FILE_MD,
}
FIXTURES = [(doc, name) for doc, path in DOCUMENTS.items() for name in _fixtures(path)]


def _fixture(name: str) -> str:
    """The text of a fixture, from the first document that has it."""
    for path in DOCUMENTS.values():
        if name in (fixtures := _fixtures(path)):
            return fixtures[name]
    raise AssertionError(f"no document has a fixture named {name}")


def _stage(tmp_path: Path, *names: str) -> Path:
    """Write the named documented fixtures, and stand-ins for the sources they list, under
    `tmp_path/design`; return the directory."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    for name in names:
        text = _fixture(name)
        (root / name).write_text(text + "\n", encoding="utf-8")
        data = yaml.safe_load(text)
        for source in (data.get("rtl") or {}).get("sources", []):
            file = source["file"] if isinstance(source, dict) else source
            body = {".v": "module top(input clk, output q); assign q = clk; endmodule\n"}.get(
                Path(file).suffix, '{"modules": {}}\n' if file.endswith(".json") else ""
            )
            (root / file).write_text(body)
    return root


def _xeda(*args, ok=None):
    """`xeda <args> --json`: the result and its one parsed document."""
    result = CliRunner().invoke(cli, [str(arg) for arg in (*args, "--json")])
    if ok is not None:
        assert (result.exit_code == 0) is ok, result.output
    return result, json.loads(result.stdout)


@pytest.fixture(autouse=True)
def in_scratch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _node(document: dict, name: str) -> dict:
    nodes = document["plan"]["nodes"] if "plan" in document else document["nodes"]
    return next(node for node in nodes if (node.get("name") or node["node"]) == name)


def _input(document: dict, node: str, name: str) -> dict:
    return next(i for i in _node(document, node)["inputs"] if i["name"] == name)


def _planned(*args) -> dict:
    result, document = _xeda("run", *args, "--dry-run", ok=True)
    return document


# ----------------------------------------------------------------- fixtures load and validate


def test_the_documents_carry_the_fixtures_the_chain_chapter_teaches():
    """A document that loses a fixture (or a renamed one that no test reaches) fails here."""
    assert set(_fixtures(FLOWS_RST)) >= {"routed_demo.yaml", "prebuilt_demo.yaml", "project.yaml"}
    assert "bound_demo.yaml" in _fixtures(DESIGN_FILE_RST)
    assert "bound_demo.yaml" in _fixtures(DESIGN_FILE_MD)
    assert {"prebuilt_demo.yaml", "project.yaml"} <= set(_fixtures(SKILL_MD))


@pytest.mark.parametrize("document, name", FIXTURES)
def test_a_documented_fixture_loads_and_agrees_with_the_published_schema(tmp_path, document, name):
    """Design fixtures load with `Design.from_file`, validate against `design_schema()`, and
    every flow section is valid for its flow once `inputs` is split out as the launcher does;
    project fixtures (no `rtl`) are flow sections alone."""
    root = _stage(tmp_path, name)
    data = yaml.safe_load((root / name).read_text())
    sections = data.get("flows") or {}
    if "rtl" in data:
        design = Design.from_file(root / name)
        jsonschema.validate(data, design_schema())
        assert design.name == data["name"]
        sections = design.flow or {}
    assert sections, "a fixture without a flows section teaches no binding"
    remaining, _bindings = split_bindings(sections, location=name)
    assert all("inputs" not in section for section in remaining.values())
    for flow, section in remaining.items():
        get_flow_class(flow).Settings.from_input(section, design_root=root, runner_cwd=root)


@pytest.mark.parametrize("name", sorted({n for _, n in FIXTURES}))
def test_a_fixture_given_in_several_documents_means_the_same_everywhere(name):
    """The human guide and the agent skill carry the same fixtures, as data."""
    copies = [
        yaml.safe_load(_fixtures(path)[name])
        for path in DOCUMENTS.values()
        if name in _fixtures(path)
    ]
    assert all(copy == copies[0] for copy in copies), name


def test_a_saved_binding_is_resolver_input_and_not_a_flow_setting(tmp_path):
    """`inputs` is documented as wiring: no flow lists it as a setting, a flow section that
    holds it still validates once it is split out, and what runs records no such setting."""
    for flow in ("yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"):
        listed = {f["name"] for f in settings_info(get_flow_class(flow))["fields"]}
        assert "inputs" not in listed, flow
    result = CliRunner().invoke(cli, ["list-settings", "nextpnr", "--json"])
    assert "inputs" not in {f["name"] for f in json.loads(result.stdout)["fields"]}
    root = _stage(tmp_path, "routed_demo.yaml")
    with pytest.raises(Exception, match="inputs"):
        get_flow_class("nextpnr").Settings.from_input(
            {"inputs": {"netlist": "yosys_fpga.netlist"}}, design_root=root, runner_cwd=root
        )


# ------------------------------------------------------------------------ the help example


def test_the_example_of_run_help_is_the_documented_chain_and_plans(tmp_path):
    """`xeda run --help` ends with an example; the guide shows the same command; both run."""
    import click

    help_text = " ".join(click.unstyle(CliRunner().invoke(cli, ["run", "--help"]).output).split())
    example = re.search(r"Example: xeda run (\S+) (\S+\.yaml)", help_text)
    assert example, "run --help needs an example command"
    chain, design_name = example.groups()
    assert chain == CHAIN
    chapter = _section(FLOWS_RST, *CHAIN_SECTION)
    assert f"xeda run {chain} {design_name}" in chapter
    root = _stage(tmp_path, "routed_demo.yaml")
    (root / design_name).write_text((root / "routed_demo.yaml").read_text())
    document = _planned(chain, root / design_name)
    assert [e["flow"] for e in document["request"]] == chain.split("+")
    assert [n["name"] for n in document["plan"]["nodes"]] == chain.split("+")
    assert not (tmp_path / "xeda_run").exists(), "a dry run changes nothing"


def test_every_command_the_chain_chapter_shows_runs_or_is_refused_as_it_says(tmp_path):
    """Each `xeda run` line of the chapter's bash blocks: a command that is supported plans
    (dry run) and one under the `not available yet` marker is refused today, as a usage
    error before any tool, naming the flow with no declared I/O. When `bsc` declares its I/O,
    the refused commands must stop being refused and this test makes the documents catch up."""
    chapter = _section(FLOWS_RST, *CHAIN_SECTION)
    root = _stage(tmp_path, "routed_demo.yaml", "prebuilt_demo.yaml")
    for stem in ("design", "knight"):
        (root / f"{stem}.yaml").write_text((root / "routed_demo.yaml").read_text())
    commands: list[tuple[list[str], bool]] = []
    for language, body in _blocks(FLOWS_RST):
        if language != "bash" or body not in chapter.replace("\n    ", "\n"):
            continue
        future = body.startswith(FUTURE_MARKER)
        for line in body.splitlines():
            if line.startswith("xeda run "):
                commands.append((shlex.split(line, comments=True)[2:], future))
    assert len(commands) >= 5 and any(future for _, future in commands)
    for words, future in commands:
        words = [str(root / w) if w.endswith(".yaml") else w for w in words]
        result = CliRunner().invoke(cli, ["run", *words, "--dry-run", "--json"])
        if future:
            assert result.exit_code == 2, (words, result.output)
            assert re.search(NO_EDGE, result.output), words
        else:
            assert result.exit_code == 0, (words, result.output)
    assert not (tmp_path / "xeda_run").exists()


def test_completion_examples_are_what_completion_returns():
    candidates = [cls for cls in all_flow_classes() if not cls.__name__.startswith("_")]
    examples = re.findall(
        r"^    xeda run (\S*)<TAB>\s+->\s+(.+)$", _section(FLOWS_RST, *CHAIN_SECTION), re.M
    )
    assert len(examples) == 3
    for typed, expected in examples:
        returned = complete_request(typed, candidates)
        assert returned == ([] if expected.startswith("(") else [expected]), typed


# ------------------------------------------------- the structural request and per-node inputs


def _chain_json_example() -> tuple[str, dict]:
    text = MACHINE_READABLE_RST.read_text(encoding="utf-8")
    section = text.split("Planning a chain\n", 1)[1]
    command = re.search(r"    xeda run (.+)\n", section)
    assert command
    example = next(
        body for language, body in _blocks(MACHINE_READABLE_RST) if '"flow": "fpga_pack"' in body
    )
    return command.group(1), json.loads(example)


def test_the_documented_chain_document_is_what_the_dry_run_prints(tmp_path):
    """The abridged chain document of machine-readable.rst is a projection of the real one:
    `request` exactly, each node's wired inputs field for field."""
    command, example = _chain_json_example()
    words = command.split()
    assert words[:2] == [CHAIN, "routed_demo.yaml"] and "--json" in words
    root = _stage(tmp_path, "routed_demo.yaml")
    actual = _planned(CHAIN, root / "routed_demo.yaml")
    projected = {
        "flow": actual["flow"],
        "success": actual["success"],
        "dry_run": actual["dry_run"],
        "request": actual["request"],
        "plan": {
            "requested": actual["plan"]["requested"],
            "nodes": [
                {
                    "name": node["name"],
                    "inputs": [
                        {key: i[key] for key in documented}
                        for i in node["inputs"]
                        for documented in [
                            next(
                                (d for d in doc_node["inputs"] if d["name"] == i["name"]),
                                None,
                            )
                        ]
                        if documented
                    ],
                }
                for node in actual["plan"]["nodes"]
                for doc_node in [
                    next(n for n in example["plan"]["nodes"] if n["name"] == node["name"])
                ]
            ],
        },
    }
    assert projected == example
    # "the chain replaced both bindings the file saved", and says which
    for node, name in (("nextpnr", "netlist"), ("fpga_pack", "config")):
        bound = _input(actual, node, name)
        (replaced,) = bound["overridden"]
        assert "routed_demo.yaml" in replaced and f"flows.{node}.inputs.{name}" in replaced


# ------------------------------------------------- precedence: binding > source > default


def test_an_input_comes_from_a_binding_before_a_source_before_its_default(tmp_path):
    """The documented order, on the documented fixtures: `prebuilt_demo.yaml` reads its
    source and runs no synthesis; a chain, or a saved binding (`bound_demo.yaml`), selects the
    synthesis anyway; with neither a source nor a binding the default producer feeds it."""
    root = _stage(tmp_path, "prebuilt_demo.yaml", "bound_demo.yaml", "routed_demo.yaml")
    source = _planned("nextpnr", root / "prebuilt_demo.yaml")
    assert [n["name"] for n in source["plan"]["nodes"]] == ["nextpnr"]
    netlist = _input(source, "nextpnr", "netlist")
    assert netlist["origin"] == "source" and netlist["binding_origin"] is None
    assert netlist["sources"] == [str(root / "top.json")]

    chain = _planned("yosys_fpga+nextpnr", root / "prebuilt_demo.yaml")
    assert [n["name"] for n in chain["plan"]["nodes"]] == ["yosys_fpga", "nextpnr"]
    netlist = _input(chain, "nextpnr", "netlist")
    assert (netlist["origin"], netlist["producer"], netlist["binding_origin"]) == (
        "producer",
        "yosys_fpga",
        "chain",
    )
    assert netlist["sources"] == []

    saved = _planned("nextpnr", root / "bound_demo.yaml")
    assert [n["name"] for n in saved["plan"]["nodes"]] == ["yosys_fpga", "nextpnr"]
    netlist = _input(saved, "nextpnr", "netlist")
    assert (netlist["producer"], netlist["output"], netlist["binding_origin"]) == (
        "yosys_fpga",
        "netlist",
        "file",
    )
    assert str(root / "bound_demo.yaml") in netlist["binding_location"]

    # neither: the declared default producer, with no binding anywhere
    data = yaml.safe_load((root / "routed_demo.yaml").read_text())
    for section in data["flows"].values():
        section.pop("inputs", None)
    bare = root / "bare.yaml"
    bare.write_text(yaml.safe_dump(data))
    default = _planned("nextpnr", bare)
    netlist = _input(default, "nextpnr", "netlist")
    assert (netlist["producer"], netlist["binding_origin"]) == ("yosys_fpga", None)


def test_a_binding_that_says_what_the_defaults_say_changes_nothing(tmp_path):
    """ "These bindings say what the defaults say, so the plan and every hash are those of the
    file without them": by identity, for `routed_demo.yaml` against itself without `inputs`."""
    root = _stage(tmp_path, "routed_demo.yaml")
    data = yaml.safe_load((root / "routed_demo.yaml").read_text())
    for section in data["flows"].values():
        section.pop("inputs", None)
    (root / "bare.yaml").write_text(yaml.safe_dump(data))

    def graph(document):
        return [
            (
                n["name"],
                n["run_path"].replace("bare", "routed_demo"),
                n["flowrun_hash"],
                n["settings_hash"],
            )
            for n in document["plan"]["nodes"]
        ]

    bound = _planned("fpga_pack", root / "routed_demo.yaml", "--hashed-run-dirs")
    bare = _planned("fpga_pack", root / "bare.yaml", "--hashed-run-dirs")
    assert _input(bound, "fpga_pack", "config")["binding_origin"] == "file"
    assert _input(bare, "fpga_pack", "config")["binding_origin"] is None
    assert [h[2:] for h in graph(bound)] == [h[2:] for h in graph(bare)]
    # ... and a chain spelling the same route is the same graph again
    chained = _planned(CHAIN, root / "bare.yaml", "--hashed-run-dirs")
    assert [h[2:] for h in graph(chained)] == [h[2:] for h in graph(bare)]


# ------------------------------------ a chain over saved bindings; collisions with explicit ones


def test_a_chain_replaces_the_bindings_a_design_and_a_named_project_saved(tmp_path):
    root = _stage(tmp_path, "bound_demo.yaml", "project.yaml")
    design, project = root / "bound_demo.yaml", root / "project.yaml"
    both = ["--xedaproject", project]
    # alone, the design's binding is the one in force (a project's ranks below it)
    saved = _planned("nextpnr", design, *both)
    netlist = _input(saved, "nextpnr", "netlist")
    assert netlist["binding_origin"] == "file" and str(design) in netlist["binding_location"]
    # a chain replaces both, and the plan lists what it replaced: the project's, then the design's
    chain = _planned("yosys_fpga+nextpnr", design, *both)
    netlist = _input(chain, "nextpnr", "netlist")
    assert netlist["binding_origin"] == "chain"
    assert "chain position 1 (yosys_fpga) -> 2 (nextpnr)" in netlist["binding_location"]
    assert [str(project) in o for o in netlist["overridden"]] == [True, False]
    assert [str(design) in o for o in netlist["overridden"]] == [False, True]


DIFFERENT = {"equal": "yosys_fpga.netlist", "different": "fpga_pack"}


@pytest.mark.parametrize("kind", ["equal", "different"])
def test_a_chain_and_a_command_line_binding_of_one_input_collide(tmp_path, kind):
    """Equal or not, before either is checked: an error naming the chain position and where
    the other binding was given, as the documented message does."""
    root = _stage(tmp_path, "prebuilt_demo.yaml")
    explicit = f"flows.nextpnr.inputs.netlist={DIFFERENT[kind]}"
    result, document = _xeda(
        "run", "yosys_fpga+nextpnr", root / "prebuilt_demo.yaml", "--dry-run", "-s", explicit
    )
    assert result.exit_code != 0 and document["success"] is False
    message = document["error"]["message"]
    assert "chain position 1 (yosys_fpga) -> 2 (nextpnr)" in message
    assert "the command line: flows.nextpnr.inputs.netlist" in message
    assert "even when equal" in message
    # the guide quotes this very message
    quoted = next(
        body
        for language, body in _blocks(FLOWS_RST)
        if language == "text" and "also binds that input" in body
    )
    assert " ".join(quoted.split()) in " ".join(message.split())
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize("kind", ["equal", "different"])
@pytest.mark.parametrize("style", ["dotted", "mapping"])
def test_a_chain_and_an_api_binding_of_one_input_collide(tmp_path, kind, style):
    root = _stage(tmp_path, "prebuilt_demo.yaml")
    design = Design.from_file(root / "prebuilt_demo.yaml")
    reference = DIFFERENT[kind]
    overrides = (
        {"inputs.netlist": reference} if style == "dotted" else {"inputs": {"netlist": reference}}
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException) as raised:
        runner.plan(parse_request("yosys_fpga+nextpnr"), design, flow_overrides=overrides)
    message = " ".join(str(raised.value).split())
    assert "chain position 1 (yosys_fpga) -> 2 (nextpnr)" in message
    assert "the API" in message and "inputs.netlist" in message
    assert not (tmp_path / "run").exists()
    # the control: without the chain the same API binding is just a binding
    plan = runner.plan(
        parse_request("nextpnr"), design, flow_overrides={"inputs.netlist": "yosys_fpga.netlist"}
    )
    assert _binding_origin(plan) == "api"


def _binding_origin(plan) -> str:
    from xeda.introspect import plan_info

    document = {"plan": plan_info(plan)}
    return _input(document, "nextpnr", "netlist")["binding_origin"]


# ----------------------------------------- the last node's settings, results and delivery


@pytest.fixture
def toolchain(tmp_path, monkeypatch) -> Path:
    """The fake FPGA toolchain first on PATH, its `openFPGALoader` the fake (never a real
    programmer); no chain below names the programmer."""
    return tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")


def test_the_last_flow_owns_the_results_and_delivery_and_any_flow_is_set_by_name(
    tmp_path, toolchain
):
    root = _stage(tmp_path, "routed_demo.yaml")
    out = tmp_path / "out"
    result, document = _xeda(
        "run",
        CHAIN,
        root / "routed_demo.yaml",
        "-s",
        "flows.nextpnr.seed=2",
        "flows.yosys_fpga.flatten=true",
        "--outputs-to",
        out,
        ok=True,
    )
    assert document["flow"] == "fpga_pack" and document["success"]
    assert [e["flow"] for e in document["request"]] == CHAIN.split("+")
    assert [(n["node"], n["state"]) for n in document["nodes"]] == [
        (flow, "ran") for flow in CHAIN.split("+")
    ]
    # the results and the exit status are the last flow's: its checked bitstream record
    assert set(document["results"]["outputs"]) == {"bitstream"}
    # -s flows.<flow>.key reached that flow and no other
    run = tmp_path / "xeda_run/routed_demo"
    nextpnr = json.loads((run / "nextpnr/settings.json").read_text())["flow_settings"]
    assert nextpnr["seed"] == 2 and "inputs" not in nextpnr
    yosys = json.loads((run / "yosys_fpga/settings.json").read_text())["flow_settings"]
    assert yosys["flatten"] is True and "seed" not in yosys
    pack = json.loads((run / "fpga_pack/settings.json").read_text())["flow_settings"]
    assert "seed" not in pack and "flatten" not in pack and "inputs" not in pack
    # --outputs-to delivered the last flow's output and nothing of the producers'
    delivered = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert delivered == ["outputs/routed_demo.bit"]
    deliveries = {n["node"]: n["deliveries"] for n in document["nodes"]}
    assert deliveries["yosys_fpga"] == deliveries["nextpnr"] == []
    assert [d["setting"] for d in deliveries["fpga_pack"]] == ["--outputs-to"]


def test_the_saved_bindings_of_a_file_are_wiring_in_what_ran(tmp_path, toolchain):
    """Run with the saved bindings alone: the plan used them, the recorded settings do not
    contain them, and the second launch is wholly fresh."""
    root = _stage(tmp_path, "routed_demo.yaml")
    result, first = _xeda("run", "fpga_pack", root / "routed_demo.yaml", ok=True)
    assert [n["state"] for n in first["nodes"]] == ["ran"] * 3
    assert _input(first, "fpga_pack", "config")["binding_origin"] == "file"
    for flow in CHAIN.split("+"):
        recorded = json.loads((tmp_path / f"xeda_run/routed_demo/{flow}/settings.json").read_text())
        assert "inputs" not in recorded["flow_settings"], flow
        assert "inputs" not in recorded["effective_flow_settings"], flow
    result, second = _xeda("run", "fpga_pack", root / "routed_demo.yaml", ok=True)
    assert [n["state"] for n in second["nodes"]] == ["fresh"] * 3


def test_outputs_are_delivered_only_after_the_whole_chain_succeeded(
    tmp_path, toolchain, monkeypatch
):
    """ "--outputs-to delivers the last flow's outputs, only after the whole chain succeeded":
    a failing packer delivers nothing, and the failure document says which node did not run."""
    root = _stage(tmp_path, "routed_demo.yaml")
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "ecppack")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    out = tmp_path / "out"
    result, document = _xeda("run", CHAIN, root / "routed_demo.yaml", "--outputs-to", out)
    assert result.exit_code == 1 and document["success"] is False
    assert [n["state"] for n in document["nodes"]] == ["ran", "ran", "failed"]
    assert not out.exists()


def test_a_flow_that_fails_without_a_tool_error_says_reported_failure(
    tmp_path, toolchain, monkeypatch
):
    """The documented split: the node's `results.json` says `ReportedFailure`, the consumers quote
    it as `FlowDependencyFailure`, and a requested flow's own failure is `FlowFailed` at the top
    level of the document."""
    root = _stage(tmp_path, "routed_demo.yaml")
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "yosys")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "no-output")
    result, document = _xeda(
        "run", "yosys_fpga", root / "routed_demo.yaml", "-s", "fpga.part=LFE5U-85F-6BG381C"
    )
    assert result.exit_code == 1 and document["error"]["type"] == "FlowFailed"
    recorded = json.loads((tmp_path / "xeda_run/routed_demo/yosys_fpga/results.json").read_text())
    assert (
        recorded["error"]["type"] == "ReportedFailure"
        and "reported failure" in recorded["error"]["message"]
    )
    result, document = _xeda("run", CHAIN, root / "routed_demo.yaml")
    assert document["error"]["type"] == "FlowDependencyFailure"
    assert "ReportedFailure" in document["error"]["message"]
    assert [n["state"] for n in document["nodes"]][0] == "failed"


# --------------------------------------------------------- errors and suggestions the guide shows


def test_a_chain_that_does_not_fit_suggests_the_valid_one_and_a_programmer_ends_a_chain(tmp_path):
    root = _stage(tmp_path, "routed_demo.yaml")
    design = root / "routed_demo.yaml"
    result, document = _xeda("run", "nextpnr+openfpgaloader", design, "--dry-run")
    assert result.exit_code == 2 and "request" not in document
    assert "Did you mean `nextpnr+fpga_pack+openfpgaloader`?" in document["error"]["message"]
    # and the suggestion is itself a chain that plans
    planned = _planned("nextpnr+fpga_pack+openfpgaloader", design)
    assert [n["name"] for n in planned["plan"]["nodes"]] == [
        "yosys_fpga",
        "nextpnr",
        "fpga_pack",
        "openfpgaloader",
    ]
    assert _node(planned, "openfpgaloader")["action_reason"] == "it programs a device"
    result, document = _xeda("run", "openfpgaloader+nextpnr", design, "--dry-run")
    assert result.exit_code == 2
    assert "programs a device and can only end a chain" in document["error"]["message"]
    result, document = _xeda("run", "nextpnr+nextpnr", design, "--dry-run")
    assert result.exit_code == 2 and "appears more than once" in document["error"]["message"]


# ------------------------------------------------ local only, and the undeclared flows boundary


def test_chains_and_reached_bindings_are_local_and_refused_before_anything_connects(
    tmp_path, monkeypatch
):
    connected: list = []
    monkeypatch.setattr(remote_module, "Connection", lambda **kwargs: connected.append(kwargs))
    root = _stage(tmp_path, "routed_demo.yaml")
    design = root / "routed_demo.yaml"
    result, document = _xeda("run", CHAIN, design, "--remote", "host")
    assert result.exit_code != 0 and "local `xeda run` requests" in document["error"]["message"]
    # a saved binding the request reaches is just as local ...
    result, document = _xeda("run", "fpga_pack", design, "--remote", "host")
    assert result.exit_code != 0 and "binding" in document["error"]["message"].lower()
    assert not connected
    # ... and every other command takes one flow, and says where chains go
    for command in (("dse", CHAIN, "--design", design), ("scrub", CHAIN, "routed_demo")):
        result = CliRunner().invoke(cli, [str(a) for a in command])
        assert result.exit_code == 2
        assert "A chain is a request for `xeda run`" in " ".join(result.output.split()), command


def test_flows_without_declared_io_run_alone_and_nothing_binds_a_design_input(tmp_path):
    """The boundary the guide states: `bsc` and `vivado_project` are not chainable, and an
    `inputs.design` binding is refused (no flow declares one), in the file, on the command line,
    and saying why. `vivado_synth` declares its outputs, so it precedes the loader, `openroad` by
    the type of each of its two netlists, and `vivado_power` by its routed checkpoint."""
    listed = {
        f["name"]: f for f in json.loads(CliRunner().invoke(cli, ["list-flows", "--json"]).stdout)
    }
    assert [e["flow"] for e in listed["vivado_synth"]["can_precede"]] == [
        "openfpgaloader",
        "openroad",  # by `netlist`
        "openroad",  # by `netlist_timing`
        "vivado_power",
    ]
    for flow in ("bsc", "bsc_sim", "vivado_project"):
        info = listed[flow]
        assert info["can_follow"] == info["can_precede"] == [], flow
        assert info["inputs"] == [] and info["outputs"] == [], flow
        assert flow_info(get_flow_class(flow))["inputs"] == []
    root = _stage(tmp_path, "routed_demo.yaml")
    design = root / "routed_demo.yaml"
    for chain in ("bsc+yosys_fpga", "yosys_fpga+bsc", "vivado_project+openfpgaloader"):
        result, document = _xeda("run", chain, design, "--dry-run")
        assert result.exit_code == 2, chain
        assert re.search(NO_EDGE, document["error"]["message"])
    data = yaml.safe_load(design.read_text())
    data["flows"]["vivado_project"] = {"inputs": {"design": "bsc"}}
    (root / "saved_design_binding.yaml").write_text(yaml.safe_dump(data))
    for args in (
        ("vivado_project", root / "saved_design_binding.yaml"),
        ("vivado_project", design, "-s", "flows.vivado_project.inputs.design=bsc"),
    ):
        result, document = _xeda("run", *args, "--dry-run")
        assert result.exit_code != 0
        message = document["error"]["message"]
        assert "declares no inputs" in message
        assert "only a flow's declared inputs can be bound" in message
    # the guide's own suggestion for an external file is a typed source, never a path binding
    result, document = _xeda("run", "nextpnr", design, "--dry-run", "-s", "inputs.netlist=top.json")
    assert result.exit_code != 0 and "unknown producer 'top'" in document["error"]["message"]
    # nothing was written for any of it
    assert not (tmp_path / "xeda_run").exists()
