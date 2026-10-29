"""The generated lightweight flow catalog stays fresh and matches chain validation."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.flow.io import declared_outputs

from . import io_flows
from .flow_registry import preserved_flow_registry
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.python_compat

ROOT = Path(__file__).resolve().parents[1]


def test_generated_flow_catalog_is_current():
    result = subprocess.run(
        [sys.executable, "tools/generate_flow_catalog.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=HANG_GUARD,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_catalog_generator_excludes_registered_extension_flows():
    code = r"""
import json
import runpy
from xeda.flow import Flow
from xeda.introspect import all_flow_classes

generator = runpy.run_path("tools/generate_flow_catalog.py")

class CatalogExtensionFlow(Flow):
    def run(self) -> None:
        pass

catalog = json.loads(generator["render"]())
builtin_classes = {
    flow for flow in all_flow_classes()
    if flow.__module__.startswith("xeda.flows.")
}
builtin_names = {flow.name for flow in builtin_classes}
assert set(catalog) == builtin_names
assert "catalog_extension_flow" not in catalog
assert "vivado_synth" in catalog
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=HANG_GUARD,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_builtin_completion_matches_the_runtime_chain_validator():
    """Compare exact candidate lists for builtin spellings, outputs, followers, and bad input."""
    code = r"""
from xeda.flow_runner.chains import complete_request
from xeda.flow.io import declared_outputs
from xeda.flow_catalog import complete_builtin_request
from xeda.introspect import all_flow_classes

classes = [
    cls for cls in all_flow_classes()
    if cls.__module__.startswith("xeda.flows")
]
cases = {
    "", "+", "nosuch+ne", "nextpnr++", "nextpnr.config.x+",
    "yosys_fpga+nextpnr+n", "openfpgaloader+", "nextpnr+openfpgaloader",
    " yosys_fpga+ne", "yosys_fpga +ne", " nextpnr .c",
    "yosys__fpga+", "yosys__fpga+ne", "_yosys_fpga_+ne",
    "g_h_d_l_synth+",
    # The final edge can be valid while an earlier edge makes the full request invalid.
    "openfpgaloader+nextpnr+fp",
    "yosys_fpga+nextpnr+yosys_fpga",
    "yosys_fpga+nextpnr+fpga_pack+open",
    # a link can pass through the flows between its two flows
    "yosys_fpga+fpga_pack+", "yosys_fpga+fpga_pack+o", "yosys_fpga+fpga_pack+ne",
    "yosys_fpga+fpga_pack+nextpnr", "yosys_fpga+openfpgaloader+",
    "yosys_fpga+openfpgaloader+fpga_pack", "nextpnr+openfpgaloader+", "nextpnr+fpga_pack+",
    "nextpnr+openfpgaloader+fpga_pack", "yosys_fpga.netlist+fpga_pack+",
    "yosys_fpga.netlist+openfpgaloader+", "yosys_fpga.netlist+", "yosys_fpga.netlist+o",
    "yosys_fpga.netlist+f", "yosys_fpga+fpga_pack.", "yosys_fpga+fpga_pack.b",
    "yosys_fpga+openfpgaloader.", "nextpnr.config+openfpgaloader+", "nextpnr.config+",
    "yosys_fpga+nextpnr+openfpgaloader+", "YosysFpga+FpgaPack+", "yosys-fpga+fpga-pack+o",
    "yosys_fpga+open_xc7+fpga_pack+",
}

by_spelling = {name: cls for cls in classes for name in (cls.name,)}
for cls in classes:
    spellings = {
        cls.name,
        cls.name.replace("_", "-"),
        cls.__name__,
    }
    for spelling in spellings:
        cases.update({spelling, spelling[:1], spelling[:3], spelling + "+"})
        for output in declared_outputs(cls):
            cases.update(
                spelling + "." + output[:length]
                for length in range(len(output) + 1)
            )
            qualified = spelling + "." + output + "+"
            cases.add(qualified)
            for candidate in complete_request(qualified, classes):
                child = candidate[len(qualified):]
                cases.update(qualified + child[:length] for length in range(1, len(child) + 1))

        # every valid link of two flows, unqualified and qualified, followed by `+`
        for output in ("", *("." + name for name in declared_outputs(cls))):
            head = spelling + output + "+"
            for candidate in complete_request(head, classes):
                follower = candidate[len(head):]
                cases.add(head + follower + "+")
                for third in complete_request(head + follower + "+", classes):
                    cases.add(third)
                for made in declared_outputs(by_spelling[follower]):
                    cases.add(head + follower + "." + made[:1])

# Include each actual follower and a partial follower prefix for every builtin producer spelling.
for cls in classes:
    for spelling in {cls.name}:
        for candidate in complete_request(spelling + "+", classes):
            suffix = candidate[len(spelling) + 1:]
            cases.add(spelling + "+" + suffix[:1])
            cases.add(spelling + "+" + suffix)
            second = spelling + "+" + suffix + "+"
            cases.add(second)
            for third in complete_request(second, classes):
                cases.add(third)

for incomplete in sorted(cases):
    if "+" not in incomplete and "." not in incomplete:
        continue  # Plain flow-name completion remains a separate Click choice path.
    expected = complete_request(incomplete, classes)
    actual = complete_builtin_request(incomplete)
    assert actual == expected, (incomplete, expected, actual)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=HANG_GUARD,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _spellings(cls):
    return {cls.name, cls.name.replace("_", "-"), cls.__name__}


def _agreement_cases(classes, complete_request):
    """The unfinished requests to compare: every spelling of every flow, and then every valid
    chain of up to four flows (with each flow's outputs) followed by `+`, with the prefixes of the
    next element and of its outputs."""
    by_spelling = {spelling: cls for cls in classes for spelling in _spellings(cls)}
    cases = {""}
    heads = {""} | {spelling + "+" for spelling in by_spelling}
    for _ in range(4):
        grown = set()
        for head in heads:
            for token in complete_request(head, classes):
                spelling = token[len(head) :]
                cases.update(head + spelling[:length] for length in range(len(spelling) + 1))
                for output in declared_outputs(by_spelling[spelling]):
                    cases.update(
                        f"{head}{spelling}.{output[:length]}" for length in range(len(output) + 1)
                    )
                    grown.add(f"{head}{spelling}.{output}+")
                grown.add(token + "+")
        cases |= grown
        heads = grown
    return {case for case in cases if "+" in case or "." in case}


def _flows_that_skip():
    return sorted(
        [
            getattr(io_flows, name)
            for name in (
                "TSkipMake",
                "TSkipMid",
                "TSkipEnd",
                "TSkipSide",
                "TSkipSink",
                "TRouteA",
                "TRouteB",
                "TRouteC",
                "TChainProducer",
                "TChainAliased",
                "TChainAction",
            )
        ],
        key=lambda cls: cls.name,
    )


def test_completion_from_the_catalog_matches_the_runtime_across_a_skipped_flow(monkeypatch):
    """No built-in chain repeats a flow through a link that skips it, so the toy flows do."""
    from xeda import flow_catalog
    from xeda.flow_runner.chains import complete_request

    with preserved_flow_registry():
        classes = _flows_that_skip()
        monkeypatch.setattr(sys, "path", list(sys.path))  # the generator puts `src` on the path
        render = runpy.run_path(str(ROOT / "tools" / "generate_flow_catalog.py"))["render"]
        catalog = json.loads(render(classes))
        monkeypatch.setattr(flow_catalog, "_builtin_flows", lambda: list(catalog.items()))
        # the catalog lists the flow a link skips, and lists it for the follower alone
        assert catalog["t_skip_make"]["followers"][""] == [
            "t_chain_aliased",
            "t_route_b",
            "t_skip_mid",
            "t_skip_side",
            "t_skip_end",
            "t_skip_sink",
        ]
        assert catalog["t_skip_make"]["routes"][""] == {
            "t_skip_end": ["t_skip_mid"],
            "t_skip_sink": ["t_skip_mid"],
        }
        assert "routes" not in catalog["t_skip_mid"]
        cases = _agreement_cases(classes, complete_request)
        assert "t_skip_make+t_skip_end+t_skip_side+" in cases
        assert len(cases) > 200
        for incomplete in sorted(cases):
            expected = complete_request(incomplete, classes)
            actual = flow_catalog.complete_builtin_request(incomplete)
            assert actual == expected, (incomplete, expected, actual)
        # a skipped flow is refused as the next element and in a later link
        after_end = flow_catalog.complete_builtin_request("t_skip_make+t_skip_end+")
        assert "t_skip_make+t_skip_end+t_skip_side" in after_end
        assert not [name for name in after_end if name.endswith(("t_skip_mid", "t_skip_sink"))]
        assert flow_catalog.complete_builtin_request("t_skip_make+t_skip_end+t_skip_side+") == []
        assert flow_catalog.complete_builtin_request("t_skip_make+t_skip_end+t_skip_mid+") == []


def test_a_catalog_without_routes_completes_every_follower_as_a_direct_one(monkeypatch):
    from xeda import flow_catalog

    records = {
        "a": {"followers": {"": ["b", "c"]}},
        "b": {"followers": {"": ["c"]}},
        "c": {"followers": {"": []}},
    }
    monkeypatch.setattr(flow_catalog, "_builtin_flows", lambda: list(records.items()))
    assert flow_catalog.complete_builtin_request("a+") == ["a+b", "a+c"]
    assert flow_catalog.complete_builtin_request("a+b+") == ["a+b+c"]
    assert flow_catalog.complete_builtin_request("a+c+") == []
