"""Every example's flow settings are valid for the flow they name, with each of its targets.

`test_the_examples_still_load` loads the designs, which never validates their `flows` sections:
those are only read when a flow runs. Four sections had rotted unnoticed. One duplicated
`vivado_synth` under its pre-2022 name (`vivado_prj_synth`). Two named `cxxrtl`, which was never a
flow (it is `yosys_sim`). One asked `vivado_alt_synth` for a synthesis strategy it only has for
implementation.

Targets are optional overlays, so a plain load never reads the `flows` of a target. The sweep
loads every design once with no target and once with each of its targets.
"""

import json
from pathlib import Path

import pytest

from xeda.design import Design
from xeda.flow_runner import get_flow_class
from xeda.flow_runner.bindings import default_nodes, effective_bindings, split_bindings
from xeda.frozen import thaw
from xeda.utils import toml_load
from xeda.xedaproject import XedaProject
from xeda.yaml_loader import load_yaml

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
FILES = sorted(
    p
    for p in EXAMPLES_DIR.rglob("*")
    if p.suffix in (".yaml", ".yml", ".toml", ".json") and "xeda_run" not in p.parts
)


def _raw(path):
    """The file as written: a mapping, or None for a file that is not one."""
    if path.suffix in (".yaml", ".yml"):
        data = load_yaml(path)
    elif path.suffix == ".json":
        data = json.loads(path.read_text())
    else:
        data = toml_load(path)
    return data if isinstance(data, dict) else None


def _raw_designs(path):
    """The designs of a file as written: the file itself, or the entries of a project."""
    data = _raw(path)
    if data is None:
        return []
    if path.stem != "xedaproject":
        return [data]
    designs = data.get("design", data.get("designs"))
    if isinstance(designs, dict):
        designs = [designs]
    return [d for d in designs or [] if isinstance(d, dict)]


def _targets(path):
    """Every selection to check: None (no target), then each target of each design."""
    selections = []
    for index, raw in enumerate(_raw_designs(path)):
        expanded = Design.expanded(raw)
        selections.append((index, None))
        selections.extend((index, name) for name in expanded.get("targets") or {})
    return selections


def _flow_sections(path, index, target):
    if path.stem == "xedaproject":
        project = XedaProject.from_file(path)
        if target is None:
            yield from (project.flows or {}).items()
        design = project.get_design(index, target=target)
        yield from ((thaw(design.flows) if design else None) or {}).items()
    else:
        yield from thaw(Design.load(path, target=target).flows or {}).items()


def _cases():
    for path in FILES:
        try:
            selections = _targets(path)
        except Exception:  # an unreadable file fails its own case, not the collection
            selections = [(0, None)]
        for index, target in selections:
            label = str(path.relative_to(EXAMPLES_DIR)) + (f"[{target}]" if target else "")
            yield pytest.param(path, index, target, id=label)


@pytest.mark.parametrize("path, index, target", list(_cases()))
def test_every_flow_section_is_valid_for_its_flow(path, index, target):
    invalid = []
    for flow, section in _flow_sections(path, index, target):
        try:
            flow_class = get_flow_class(flow)
            settings, bindings = split_bindings({flow: section}, location=str(path))
            effective_bindings([bindings], default_nodes([flow_class]))
            flow_class.Settings.from_input(
                settings[flow_class.name],
                design_root=path.parent,
                runner_cwd=path.parent,
            )
        except Exception as e:  # reporting every kind of failure is the point
            invalid.append(f"[flows.{flow}]: {type(e).__name__}: {e}")
    assert not invalid, "\n".join(invalid)


def _flow_names(node):
    """Names of the flows that a raw (design-like) mapping holds directly, with dotted keys
    expanded."""
    names = set()
    for key in ("flow", "flows"):
        value = Design.expanded(node).get(key)
        if isinstance(value, dict):
            names.update(value)
    return names


def _raw_flow_count(path, index, target):
    """Structural count of flow sections in the raw file contents: the flows of the design and,
    when a target is selected, those the target adds."""
    raw = _raw_designs(path)[index]
    names = _flow_names(raw)
    if target is not None:
        names |= _flow_names(Design.expanded(raw)["targets"][target])
    count = len(names)
    if path.stem == "xedaproject" and target is None:
        count += len(_flow_names(_raw(path)))
    return count


def test_the_sweep_finds_every_examples_flow_sections():
    """Guards the sweep against passing vacuously: the number of sections `_flow_sections`
    yields must match a structural count of the flow names taken directly from the raw,
    unparsed file contents, for the design alone and for each of its targets."""
    mismatched = {
        (str(p.relative_to(EXAMPLES_DIR)), target): (found, expected)
        for p in FILES
        for index, target in _targets(p)
        for found, expected in [
            (len(list(_flow_sections(p, index, target))), _raw_flow_count(p, index, target))
        ]
        if found != expected
    }
    assert not mismatched, f"flow section count mismatch (found, expected): {mismatched}"


def test_the_sweep_covers_the_targets_of_the_examples():
    """The examples with targets are in the sweep, target by target."""
    swept = {(p.name, target) for p in FILES for _, target in _targets(p) if target}
    assert {
        ("ascon.yaml", "ecp5"),
        ("ascon.yaml", "artix7"),
        ("gcd.yaml", "ecp5"),
        ("gcd.yaml", "artix7"),
        ("uart_echo.yaml", "arty"),
        ("uart_echo.yaml", "ulx3s"),
        ("uart_echo.yaml", "stlv7325"),
        ("uart_echo.yaml", "switches"),
        ("uart_echo.yaml", "echo_transform"),
        ("uart_link.yaml", "artix7"),
        ("uart_link.yaml", "ecp5"),
        ("uart_link.yaml", "arty"),
        ("uart_link.yaml", "basys3"),
        ("uart_link.yaml", "ulx3s"),
        ("uart_link.yaml", "stlv7325"),
        ("uart_link.yaml", "core_w8"),
        ("uart_link.yaml", "core_w1"),
    } <= swept
