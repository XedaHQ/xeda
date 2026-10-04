"""Documentation coverage of the agent-facing surface.

Flows, their settings and their result keys are what a coding agent reads to decide how to drive
xeda, so an undocumented field is a real gap rather than a cosmetic one. These tests pin the
coverage that has been reached so it cannot silently regress.
"""

import enum
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Literal, Set, Tuple, get_args, get_origin

import pytest

from xeda.dataclass import annotation_args
from xeda.design import SOURCE_SUFFIXES
from xeda.flow import Flow, describe_results
from xeda.flow.sim import SimFlow
from xeda.flow_runner import FlowNotFoundError, get_flow_class
from xeda.introspect import all_flow_classes, flow_info, results_info, settings_info

#: (flow, setting) pairs that are allowed to have no description. Empty on purpose: every
#: setting is documented today. A new setting must be documented rather than added here.
SETTINGS_WITHOUT_DESCRIPTION: Set[Tuple[str, str]] = set()

#: Flows allowed to have no `results_description`. Empty on purpose -- a flow that reports
#: nothing beyond the common keys declares `results_description = {}` explicitly.
FLOWS_WITHOUT_RESULTS_DOC: Set[str] = set()

FLOW_CLASSES = all_flow_classes()
FLOW_IDS = [cls.name for cls in FLOW_CLASSES]


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_flow_has_its_own_description(flow_class):
    """`xeda list-flows` used to show inherited base-class docstrings as flow descriptions."""
    own_doc = flow_class.__dict__.get("__doc__")
    assert own_doc and own_doc.strip(), (
        f"{flow_class.__name__} has no docstring of its own, so `xeda list-flows` has nothing "
        "truthful to show for it."
    )


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_every_setting_is_documented(flow_class):
    undocumented = sorted(
        field["name"]
        for field in settings_info(flow_class)["fields"]
        if not field["description"]
        and (flow_class.name, field["name"]) not in SETTINGS_WITHOUT_DESCRIPTION
    )
    assert not undocumented, (
        f"{flow_class.name} has settings with no description: {undocumented}. Add a "
        "`description=` to each Field; agents and `xeda list-settings` have nothing else to go on."
    )


def _declared_choices(annotation: Any) -> list[Any] | None:
    """The choices a `Literal` or `Enum` annotation allows, optional or not; else `None`."""
    args = annotation_args(annotation)
    if len(args) != 1:
        return None
    (arg,) = args
    if get_origin(arg) is Literal:
        return list(get_args(arg))
    if isinstance(arg, type) and issubclass(arg, enum.Enum):
        return [member.value for member in arg]
    return None


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_every_choice_setting_advertises_its_choices(flow_class):
    """`enum` in `list-settings` is derived from the JSON Schema, whose shape pydantic chooses:
    a one-value `Literal["rtl"]` became `{"const": "rtl"}` in pydantic 2 and silently lost its
    choice. Check it against the Python annotations instead, so a shape change cannot hide one."""
    fields = {field["name"]: field for field in settings_info(flow_class)["fields"]}
    mismatched = {
        name: {"advertised": fields[name]["enum"], "declared": declared}
        for name, model_field in flow_class.Settings.model_fields.items()
        if name in fields
        and (declared := _declared_choices(model_field.annotation)) is not None
        and fields[name]["enum"] != declared
    }
    assert not mismatched, f"{flow_class.name}: {mismatched}"


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_every_flow_documents_its_results(flow_class):
    if flow_class.name in FLOWS_WITHOUT_RESULTS_DOC:
        pytest.skip("explicitly allow-listed")
    info = results_info(flow_class)
    assert info["documented"], (
        f"{flow_class.name} does not declare `results_description`. Declare the keys it reports, "
        "or `results_description = {}` if it reports none beyond the common keys."
    )
    missing = [k["name"] for k in info["keys"] if not k["documented"]]
    assert not missing, f"{flow_class.name} reports undocumented result keys: {missing}"


def test_describe_results_rejects_unknown_keys():
    """The helper must not silently invent a description for a key it does not know."""
    with pytest.raises(KeyError, match="No shared description"):
        describe_results("not_a_known_result_key")


def test_describe_results_allows_flow_specific_keys():
    described = describe_results("wns", my_own_key="Something this flow alone reports.")
    assert described["wns"]
    assert described["my_own_key"] == "Something this flow alone reports."


class _AliasFlow(Flow):
    """Minimal flow used to exercise result-alias behavior."""

    results_description: Dict[str, str] = {}

    def run(self) -> None:  # pragma: no cover - never executed
        raise NotImplementedError


def _aliased(raw: Dict[str, object]) -> Dict[str, object]:
    flow = _AliasFlow.__new__(_AliasFlow)
    flow.results = Flow.Results(**raw)  # type: ignore[attr-defined]
    flow.add_canonical_result_aliases()
    return dict(flow.results)


def test_canonical_aliases_are_additive():
    """Aliases add the canonical name; they never rename or drop what the flow reported."""
    out = _aliased({"LUT": 42, "FF": 7, "f_max": 250.0})
    assert out["LUT"] == 42 and out["FF"] == 7 and out["f_max"] == 250.0
    assert out["lut"] == 42
    assert out["ff"] == 7
    assert out["Fmax"] == 250.0


def test_canonical_aliases_do_not_overwrite_an_existing_key():
    out = _aliased({"lut": 1, "LUT": 2})
    assert out["lut"] == 1


def test_canonical_aliases_ignore_missing_keys():
    out = _aliased({"wns": -0.1})
    assert "Fmax" not in out and "lut" not in out


def test_clock_frequency_is_never_aliased_to_fmax():
    """`clock_frequency` is the constraint given to the tool, not the achieved maximum."""
    out = _aliased({"clock_frequency": 200.0})
    assert "Fmax" not in out


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_result_alias_candidates_are_documented_somewhere(flow_class):
    """A flow reporting an alias candidate must also document the canonical key."""
    info = results_info(flow_class)
    documented: List[str] = [k["name"] for k in info["keys"]]
    for canonical, candidates in flow_class.results_canonical_aliases.items():
        reported = [c for c in candidates if c in documented]
        if reported and canonical not in documented:
            pytest.fail(
                f"{flow_class.name} documents {reported} but not the canonical key "
                f"{canonical!r}, which the runner adds to results.json."
            )


# ------------------------------------------------------------------ README flow catalog

README = Path(__file__).resolve().parent.parent / "README.md"

#: Heading of the README section that enumerates every supported tool and its flow names.
CATALOG_HEADING = "### Supported Tools and Flows"

#: Code spans in the catalog that are deliberately *not* flow names -- tool subcommands and
#: executables. Everything else backticked there must resolve, so a typo cannot pass for a flow.
NOT_FLOW_NAMES: Set[str] = {"route_design", "synth_design", "xsim"}


def _readme_catalog() -> str:
    """The body of the README's flow catalog, up to the next heading."""
    text = README.read_text(encoding="utf-8")
    body = text[text.index(CATALOG_HEADING) + len(CATALOG_HEADING) :]
    end = body.find("\n#")
    return body if end < 0 else body[:end]


def _catalog_code_spans() -> Set[str]:
    """Backticked identifiers in the catalog, in the shape a flow name takes."""
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", _readme_catalog()))


def test_readme_names_only_real_flows():
    """Every flow-shaped code span in the catalog must resolve.

    `vivado_postsynthsim` sat in this list for a long time: that is the module name, not the
    registered flow name, so copying it out of the README earned a `FlowNotFoundError`.
    """
    unresolvable = []
    for token in sorted(_catalog_code_spans() - NOT_FLOW_NAMES):
        try:
            get_flow_class(token)
        except FlowNotFoundError:
            unresolvable.append(token)
    assert not unresolvable, (
        f"README's {CATALOG_HEADING!r} names {unresolvable} as flows, but `get_flow_class` "
        "rejects them. Run `xeda list-flows` for the registered names, or add a genuine "
        "non-flow term to NOT_FLOW_NAMES."
    )


def test_readme_catalog_lists_every_flow():
    """A new flow must reach the README catalog, not just the registry."""
    listed = set()
    for token in _catalog_code_spans() - NOT_FLOW_NAMES:
        try:
            listed.add(get_flow_class(token).name)
        except FlowNotFoundError:
            continue
    public_flows = {cls.name for cls in FLOW_CLASSES if not cls.__name__.startswith("_")}
    missing = sorted(public_flows - listed)
    assert (
        not missing
    ), f"These flows are registered but absent from {CATALOG_HEADING!r} in README.md: {missing}."


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_every_declared_input_and_output_is_documented(flow_class):
    """`xeda list-flows --json` shows what each declared input and output is."""
    info = flow_info(flow_class)
    undocumented = [d["name"] for d in info["inputs"] + info["outputs"] if not d["description"]]
    assert not undocumented, f"{flow_class.name}: {undocumented}"


def test_every_simulator_exposes_shared_settings_and_evidence_results():
    """The public catalog must expose the common simulator contract on every sim flow."""
    required_results = {
        "sim.evidence",
        "sim.ended_by",
        "sim.time",
        "sim.time_unit",
        "sim.errors",
        "sim.warnings",
    }
    simulator_flows = [cls for cls in FLOW_CLASSES if issubclass(cls, SimFlow)]
    assert simulator_flows
    for flow_class in simulator_flows:
        settings = {field["name"] for field in settings_info(flow_class)["fields"]}
        assert {"timeout", "fail_severity"} <= settings, flow_class.name
        results = {item["name"] for item in results_info(flow_class)["keys"]}
        assert required_results <= results, (flow_class.name, required_results - results)


def test_simulation_contract_docs_describe_the_final_oracle():
    docs = (README.parent / "docs/flows.rst").read_text(encoding="utf-8")
    assert "fail closed" in docs
    assert "VCS and Questa adapters are documentation-only" in docs
    assert "only Verilator" not in docs
    assert (
        "VCS, Vivado simulation/power and the remaining bsc backends retain their existing"
        not in docs
    )


@pytest.mark.parametrize(
    "relative",
    ["docs/design-file.rst", "src/xeda/data/agent/references/design-file.md"],
)
def test_documented_source_suffixes_match_the_loader(relative):
    """Both human and agent tables must give the loader's exact suffix-to-type mapping."""
    text = (README.parent / relative).read_text(encoding="utf-8")
    section = text.split("Source files\n", 1)[1]
    if relative.endswith(".rst"):
        table = section.split(".. list-table::", 1)[1].split("\n\n", 1)[1].split("\n\n", 1)[0]
        rows = re.findall(r"   \* - (.+)\n     - (.+)", table)
    else:
        table = section.split("| Extension | Type |", 1)[1].split("\n\n", 1)[0]
        rows = re.findall(r"^\| (.+) \| (.+) \|$", table, re.M)[1:]
    documented = {}
    for extensions, types in rows:
        extension_groups = extensions.split(" / ")
        type_groups = types.split(" / ")
        assert len(extension_groups) == len(type_groups)
        for group, type_group in zip(extension_groups, type_groups):
            names = re.findall(r"`+([A-Za-z]+)`+", type_group)
            if not names:
                continue  # header
            assert len(names) == 1
            for suffix in re.findall(r"`+\.([a-z]+)`+", group):
                assert suffix not in documented
                documented[suffix] = names[0]
    assert documented == {suffix: member.name for suffix, (member, _) in SOURCE_SUFFIXES.items()}


def test_documented_dry_run_json_matches_the_cli(tmp_path, monkeypatch):
    """Run the published planning command and check its JSON example, including switched outputs."""
    from click.testing import CliRunner

    from xeda.cli import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "blinky.v").write_text("module blinky(); endmodule\n", encoding="utf-8")
    (tmp_path / "blinky.yaml").write_text(
        "name: blinky\nrtl:\n  sources: [blinky.v]\n  top: blinky\n", encoding="utf-8"
    )
    doc = (README.parent / "docs/machine-readable.rst").read_text(encoding="utf-8")
    assert "Planning a run\n" in doc, "the planning command needs a JSON reference"
    section = doc.split("Planning a run\n", 1)[1]
    command = re.search(r"    xeda run (.+)\n", section)
    assert command, "the planning example needs a runnable command"
    result = CliRunner().invoke(cli, ["run", *command.group(1).split()])
    assert result.exit_code == 0, result.output
    actual = json.loads(result.stdout)
    example = re.search(r"    \{\n.*?\n    \}", section, re.S)
    assert example, "the planning example needs a complete JSON document"
    expected = json.loads(textwrap.dedent(example.group()))
    for node in actual["plan"]["nodes"]:
        node["run_path"] = node["run_path"].replace(str(tmp_path), "/path/to")
        node["flowrun_hash"] = "..."
    assert actual == expected
    assert not (tmp_path / "xeda_run").exists()


# A bracketed section header is TOML. The documents below show YAML first, where a flow's
# settings are the `flows.<flow>` mapping, so a `[flows.x]`, `[rtl]` or `[tb]` in their prose is
# TOML left behind by the conversion. A snippet that is explicitly TOML (a `toml` fence or
# code-block) is correct -- TOML design files are still accepted.
TOML_SECTION_HEADER = re.compile(r"\[(?:flows(?:\.[\w<>.\-]+)?|rtl|tb|design|designs)\]")
YAML_FIRST_DOCUMENTS = [
    "README.md",
    "docs/**/*.rst",
    "examples/**/README.md",
    ".claude/skills/xeda/**/*.md",
    "src/xeda/data/agent/**/*.md",
]


def _prose_outside_toml_snippets(text: str, rst: bool):
    """`(line number, line)` for every line that is not inside a TOML snippet."""
    fenced = False  # inside a markdown fence of any language
    in_toml = False
    block_indent = None  # an rst `code-block:: toml` ends at the first line indented no deeper
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if rst:
            if block_indent is not None:
                if stripped and len(line) - len(line.lstrip()) <= block_indent:
                    block_indent = None
                else:
                    continue
            opening = re.match(r"(\s*)\.\.\s+(?:code-block|sourcecode)::\s*toml\b", line)
            if opening:
                block_indent = len(opening.group(1))
                continue
        elif stripped.startswith("```"):
            fenced = not fenced
            in_toml = fenced and stripped[3:].strip().lower() == "toml"
            continue
        if not in_toml:
            yield number, line


def _yaml_first_files() -> List[Path]:
    root = README.parent
    return sorted({p for pattern in YAML_FIRST_DOCUMENTS for p in root.glob(pattern)})


def test_the_documents_shown_in_yaml_name_no_toml_section_header():
    assert len(_yaml_first_files()) > 20  # the globs reach the documents
    found = [
        f"{path.relative_to(README.parent)}:{number}: {line.strip()}"
        for path in _yaml_first_files()
        for number, line in _prose_outside_toml_snippets(
            path.read_text(encoding="utf-8"), path.suffix == ".rst"
        )
        if TOML_SECTION_HEADER.search(line)
    ]
    assert (
        not found
    ), "TOML section header in YAML-first prose; write `flows.<flow>`:\n" + "\n".join(found)


def test_the_toml_header_oracle_sees_prose_and_spares_toml_snippets():
    def flagged(text: str, rst: bool) -> List[int]:
        return [
            n
            for n, line in _prose_outside_toml_snippets(text, rst)
            if TOML_SECTION_HEADER.search(line)
        ]

    markdown = "see `[flows.x]`\n```toml\n[flows.x]\n```\n```yaml\n# [tb]\n```\n[rtl] again\n"
    assert flagged(markdown, rst=False) == [1, 6, 8]
    rst = (
        "the ``[flows.x]`` section\n\n.. code-block:: toml\n\n   [rtl]\n   top = 1\n\n"
        ".. code-block:: yaml\n\n   [tb]\n\nback to ``[tb]`` prose\n"
    )
    assert flagged(rst, rst=True) == [1, 10, 12]


# ------------------------------------------------- removed names are documented only as removed

#: names that no longer exist: a maintained document may mention one only where it says so
REMOVED_NAMES = ("open_xc7", "openxc7", "lpf_cfg", "pcf_cfg", "pdc_cfg", "bitstream_file")


def _maintained_documents() -> list[Path]:
    root = Path(__file__).parent.parent
    return [
        *sorted((root / "docs").glob("*.rst")),
        root / "README.md",
        *sorted((root / "src/xeda/data/agent").rglob("*.md")),
        *sorted((root / ".claude/skills/xeda").rglob("*.md")),
    ]


def test_no_maintained_document_recommends_a_removed_flow_or_setting():
    """The flow guide, the README and the agent skill (not the changelog, which is history):
    `open_xc7`, the loader's build settings and the old pin-file settings appear only in a
    paragraph that says they were removed, and nowhere as something to run or set."""
    import re

    documents = _maintained_documents()
    assert len(documents) > 8
    offending = []
    for document in documents:
        for paragraph in re.split(r"\n\s*\n", document.read_text()):
            lowered = paragraph.lower()
            # by exact spelling: `openXC7` is the toolchain's name, `openxc7` was a flow's
            named = any(re.search(rf"\b{name}\b", paragraph) for name in REMOVED_NAMES)
            if named and "removed" not in lowered:
                offending.append((document.name, paragraph.strip()[:120]))
            if re.search(
                r"xeda run open_?xc7|openfpgaloader\.nextpnr|loader.*nextpnr\.yosys", lowered
            ):
                offending.append((document.name, paragraph.strip()[:120]))
    assert not offending


def test_the_documents_state_what_the_lut_count_is_and_is_not():
    """PB7: `lut` is per toolchain, with its stage and method, and not certified comparable
    with Vivado's."""
    root = Path(__file__).parent.parent
    for document in (
        root / "docs/flows.rst",
        root / "docs/machine-readable.rst",
        root / "src/xeda/data/agent/references/troubleshooting.md",
    ):
        text = " ".join(document.read_text().split())
        assert "LUT:STAGE" in text and "LUT:METHOD" in text, document
        assert re.search(r"(not|neither is) certified comparable", text, re.IGNORECASE), document
