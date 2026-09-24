"""A flow gets its own copy of the design, as it gets its own copy of its settings.

The design is hashed before a flow runs, recorded in `settings.json` beside that hash, and
handed on to the flow's dependencies -- so a flow that edits it (yosys emptied
`rtl.parameters`, GHDL rewrote the VHDL standard, Verilator merged testbench defines into the
RTL's, cocotb sets `tb.top`) changed what its dependencies were given and what was recorded
under a hash computed from something else. Each of those was fixed where it was; this is the
launcher's guarantee that the next one cannot do the same.
"""

import json
from typing import ClassVar

import pytest

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner


@pytest.fixture
def toy_flows():
    """Provide flows that mutate and inspect design copies."""
    seen = {}

    class DesignDep(Flow):
        """A dependency that records the design it was given."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            seen["dependency"] = dict(self.design.rtl.parameters)

        def parse_reports(self) -> bool:
            return True

    class DesignEditor(Flow):
        """A flow that edits its design before its dependency runs."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.design.rtl.parameters = {}
            self.add_dependency(DesignDep, DesignDep.Settings())

        def run(self) -> None:
            seen["flow"] = dict(self.design.rtl.parameters)

        def parse_reports(self) -> bool:
            return True

    yield DesignEditor, seen
    for cls in (DesignDep, DesignEditor):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


def test_a_flow_editing_its_design_changes_nobody_else_s(toy_flows, tmp_path):
    """A flow editing its design changes nobody else s."""
    editor, seen = toy_flows
    (tmp_path / "top.vhd").write_text("entity top is generic (W : natural := 1); end;\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.vhd"], "top": "top", "parameters": {"W": 5}},
    )
    before = design.rtl_hash

    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(editor, design)

    assert flow is not None and flow.succeeded
    assert seen == {"flow": {}, "dependency": {"W": 5}}, "the dependency got the design as given"
    assert design.rtl.parameters == {"W": 5} and design.rtl_hash == before
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["design"]["rtl"]["parameters"] == {"W": 5}
    assert recorded["rtl_hash"] == before
