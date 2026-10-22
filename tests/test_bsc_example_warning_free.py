"""Every configured Bluespec example compiles with all compiler warnings promoted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from xeda import Design
from xeda.design import SourceType
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc

from .design_edits import with_updates
from .tool_utils import require_bsc, run_report

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "bluespec"


def _design(path: Path, target: str | None, overrides: dict[str, Any] | None = None) -> Design:
    return Design.from_file(path, target=target, overrides=overrides)


def _cases():
    for path in sorted(EXAMPLES_DIR.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        targets = sorted((raw.get("targets") or {}).keys())
        for target in [None, *targets]:
            design = _design(path, target)
            sources = (*design.rtl.sources, *design.tb.sources)
            if SourceType.Bluespec not in {source.type for source in sources}:
                continue
            if (
                not design.tb.top
                and not design.rtl.top
                and target is None
                and path.name.endswith("-library.yaml")
            ):
                # A reusable library's default design is intentionally bench-free and top-free.
                continue
            if not design.tb.top and not design.rtl.top:
                raise AssertionError(f"Bluespec configuration has no top: {path}:{target}")
            suffix = f"[{target}]" if target else "[default]"
            yield pytest.param(
                path,
                target,
                {},
                id=f"{path.relative_to(EXAMPLES_DIR)}{suffix}",
            )

    # ASCON128A is a documented source define, not a YAML target. Compile its actual test top too.
    path = EXAMPLES_DIR / "ascon" / "ascon.yaml"
    yield pytest.param(
        path,
        None,
        {"rtl": {"defines": {"ASCON128A": True}}},
        id="ascon/ascon.yaml[ASCON128A]",
    )

    # Exercise the app's inherited RTL with ASCON128A on its default and every target.
    app_path = EXAMPLES_DIR / "uart_link" / "apps" / "ascon.yaml"
    app_raw = yaml.safe_load(app_path.read_text()) or {}
    for target in [None, *sorted((app_raw.get("targets") or {}).keys())]:
        suffix = f"[{target}]" if target else "[default]"
        yield pytest.param(
            app_path,
            target,
            {"rtl": {"defines": {"ASCON128A": True}}},
            id=f"uart_link/apps/ascon.yaml{suffix}[ASCON128A]",
        )


@pytest.mark.parametrize("path,target,overrides", list(_cases()))
def test_every_bluespec_configuration_compiles_without_warnings(
    path: Path,
    target: str | None,
    overrides: dict[str, Any],
    tmp_path: Path,
):
    """Promote every BSC warning while compiling each runnable default and target design."""
    require_bsc()
    design = _design(path, target, overrides)
    if design.tb.top:
        assert len(design.tb.top) == 1, f"Expected one testbench top for {path}:{target}"
        design = with_updates(
            design,
            rtl={"top": design.tb.top[0], "sources": (*design.rtl.sources, *design.tb.sources)},
            tb={"top": (), "sources": ()},
        )

    settings = {"promote_warnings": ["ALL"], "suppress_warnings": []}
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(Bsc, design, settings)
    assert flow is not None
    assert not flow.settings.suppress_warnings
    assert flow.succeeded, run_report(flow)
