"""Every example's flow settings are valid for the flow they name.

`test_the_examples_still_load` loads the designs, which never validates their `[flows.*]`
sections: those are only read when a flow runs. Four sections had rotted unnoticed -- one duplicated `vivado_synth` under its pre-2022 name
(`vivado_prj_synth`), two named `cxxrtl`, which was never a flow (it is `yosys_sim`), one asked
`vivado_alt_synth` for a synthesis strategy it only has for implementation.
"""

from pathlib import Path

import pytest
import yaml

from xeda.design import Design
from xeda.flow_runner import get_flow_class
from xeda.utils import toml_load
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
        for i in range(len(project.designs)):
            design = project.get_design(i)
            yield from ((design.flow if design else None) or {}).items()
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


def _raw_flow_entries(node):
    """Number of `flow`/`flows` mapping entries directly on a raw (design-like) mapping."""
    count = 0
    for key in ("flow", "flows"):
        value = node.get(key)
        if isinstance(value, dict):
            count += len(value)
    return count


def _raw_flow_count(path):
    """Structural count of flow sections in the raw (unparsed-by-xeda) file contents."""
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(path.read_text())
    else:
        data = toml_load(path)
    if not isinstance(data, dict):
        return 0
    count = _raw_flow_entries(data)
    if path.name == "xedaproject.toml":
        designs = data.get("design", data.get("designs"))
        if isinstance(designs, dict):
            designs = [designs]
        if isinstance(designs, list):
            for design in designs:
                if isinstance(design, dict):
                    count += _raw_flow_entries(design)
    return count


def test_the_sweep_finds_every_examples_flow_sections():
    """Guards the sweep against passing vacuously: the number of sections `_flow_sections`
    yields must match a structural count of `flow`/`flows` mapping entries taken directly from
    the raw, unparsed file contents."""
    mismatched = {
        p: (found, expected)
        for p in FILES
        for found, expected in [(len(list(_flow_sections(p))), _raw_flow_count(p))]
        if found != expected
    }
    assert not mismatched, f"flow section count mismatch (found, expected): {mismatched}"
