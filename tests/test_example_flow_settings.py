"""Every example's flow settings are valid for the flow they name.

`test_the_examples_still_load` loads the designs, which never validates their `[flows.*]`
sections: those are only read when a flow runs. Four sections had rotted unnoticed -- one duplicated `vivado_synth` under its pre-2022 name
(`vivado_prj_synth`), two named `cxxrtl`, which was never a flow (it is `yosys_sim`), one asked
`vivado_alt_synth` for a synthesis strategy it only has for implementation.
"""

from pathlib import Path

import pytest

from xeda.design import Design
from xeda.flow_runner import get_flow_class
from xeda.xedaproject import XedaProject

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
FILES = sorted(
    [
        *EXAMPLES_DIR.rglob("*.toml"),
        *EXAMPLES_DIR.rglob("*.xeda.yaml"),
        *EXAMPLES_DIR.rglob("*.xeda.yml"),
    ]
)


def _flow_sections(path):
    if path.name == "xedaproject.toml":
        project = XedaProject.from_file(path)
        yield from (project.flows or {}).items()
        for design in project.designs:  # raw mappings until a design is selected
            yield from (design.get("flow") or {}).items()
    else:
        yield from (Design.from_file(path).flow or {}).items()


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(EXAMPLES_DIR)))
def test_every_flow_section_is_valid_for_its_flow(path):
    invalid = []
    for flow, section in _flow_sections(path):
        try:
            get_flow_class(flow).Settings.from_input(
                section,
                design_root=path.parent,
                runner_cwd=path.parent,
            )
        except Exception as e:  # reporting every kind of failure is the point
            invalid.append(f"[flows.{flow}]: {type(e).__name__}: {e}")
    assert not invalid, "\n".join(invalid)


def test_the_sweep_finds_every_examples_flow_sections():
    """Guards the sweep against passing vacuously: each file with a `[flows.` table yields."""
    silent = [
        p
        for p in FILES
        if ("[flows." in p.read_text() or "flows:" in p.read_text()) and not list(_flow_sections(p))
    ]
    assert not silent, f"no flow sections found in: {silent}"
