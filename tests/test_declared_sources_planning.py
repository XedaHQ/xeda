"""A planning check reads a declared generated source without a file.

A launch plans on the declared design, before any generator runs. A source that a generator will
write is then a declaration: it has a type and a name, and no file yet (`Source.file` is `None`).
The checks of a flow that run at planning (`check_design_supported`, `check_settings_supported`)
must handle it. A check may refuse the design, but it may not fail on the missing file.
"""

from __future__ import annotations

import sys

import pytest

from xeda import Design
from xeda._internal.design import SourceType
from xeda._internal.flow import FlowSettingsException
from xeda._internal.flow.flow import registered_flows

#: A generator that has not run: the design only declares what it will write.
GENERATOR = {"executable": sys.executable, "args": ["gen.py"], "sources": ["gen.py"]}


def _cases() -> list[tuple[str, SourceType]]:
    import xeda._internal.flows  # noqa: F401  (registers every flow)

    cases = []
    for name, (_module, flow_class) in sorted(registered_flows.items()):
        for member in sorted(flow_class.reads_sources or (), key=lambda m: m.name):
            cases.append((name, member))
    return cases


CASES = _cases()


@pytest.mark.parametrize(
    "flow_name, member", CASES, ids=[f"{name}-{member.name}" for name, member in CASES]
)
def test_the_planning_checks_of_a_flow_read_a_declared_generated_source(
    flow_name, member, tmp_path
):
    """Each flow, for each type it reads, on a design whose only source is declared and not
    written. Only `rtl` has a generator."""
    flow_class = registered_flows[flow_name][1]
    (tmp_path / "gen.py").write_text("")
    declared = {"path": f"gen/declared_{member.name}.dat", "type": member.name}
    rtl = {"sources": [declared], "top": "top", "generator": GENERATOR}
    design = Design(name="declared", root=tmp_path, rtl=rtl)
    assert all(src.file is None for src in design.rtl.sources)
    settings = flow_class.Settings()
    for check in (
        lambda: flow_class.check_design_supported(design, settings),
        lambda: flow_class.check_settings_supported(settings),
    ):
        try:
            check()
        except FlowSettingsException:
            pass  # a refusal is a valid answer; a crash on the missing file is not


def test_the_cases_cover_the_bluespec_flows():
    assert {"bsc", "bsc_sim"} <= {name for name, _member in CASES}
    assert {member for name, member in CASES if name == "bsc"} >= {
        SourceType.Bluespec,
        SourceType.BluespecHeader,
        SourceType.CHeader,
    }
