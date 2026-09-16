"""Documentation coverage of the agent-facing surface.

Flows, their settings and their result keys are what a coding agent reads to decide how to drive
xeda, so an undocumented field is a real gap rather than a cosmetic one. These tests pin the
coverage that has been reached so it cannot silently regress.
"""

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from xeda.flow import Flow, describe_results
from xeda.flow_runner import FlowNotFoundError, get_flow_class
from xeda.introspect import all_flow_classes, results_info, settings_info

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
    missing = sorted(set(FLOW_IDS) - listed)
    assert not missing, (
        f"These flows are registered but absent from {CATALOG_HEADING!r} in README.md: {missing}."
    )
