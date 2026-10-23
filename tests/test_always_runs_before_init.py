"""`Flow.always_runs()` is answered the same before `init()` as after it.

A dry run asks whether a flow always runs on a flow that is built as the launch builds it and not
initialized (`plan_freshness`), because `init()` makes tools, reads the design's files and may
register inputs: none of it is for a dry run. The answer must not depend on any of that. For every
flow, and for each setting of the flows that decide it (a simulator's random seed, nextpnr's
seed and constraint URL, the programmer), set to each value the setting accepts, the answer
before `init()` is the answer after it.
"""

import os

import pytest

from xeda.flow import MetricSpec
from xeda import Design
from xeda.flow import Flow, FlowSettingsError, SimFlow
from xeda.run_dir import RunDirectory

from .settings_samples import flow_classes, minimal_settings
from .test_isolation import (
    DESIGNS,
    EXTRA_SETTINGS,
    FPGA_FAKED,
    FAKE_TOOLCHAIN,
    SQRT_DESIGN,
    _world,
)
from .tool_utils import use_fake_fpga_tools, use_fake_tools

FLOWS = flow_classes()


def _deciding(cls: type[Flow]) -> bool:
    """Whether some class of `cls`'s MRO other than `Flow` answers `always_runs` itself."""
    return any("always_runs" in vars(klass) for klass in cls.__mro__ if klass is not Flow)


def _bare_and_initialized(cls, settings, world, monkeypatch):
    """The answers of the flow built as the launch builds it, before and after its `init()`."""
    rtl, tb = DESIGNS.get(cls.name, SQRT_DESIGN)
    design = Design(
        name="sqrt",
        design_root=world.work,
        rtl=rtl,
        tb=tb,
        language={"vhdl": {"standard": "2008"}},
    )
    run_path = world.root / "sqrt" / cls.name
    flow = cls(
        settings.model_copy(deep=True),
        design.model_copy(deep=True),
        run_path,
        runner_cwd=world.work,
        run_directory=RunDirectory.claimed(run_path, world.root),
    )
    before = flow.always_runs()
    run_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(run_path)
    flow.init()
    return before, flow.always_runs()


def _prepare(cls, tmp_path, monkeypatch):
    world = _world(tmp_path)
    use_fake_tools(monkeypatch)
    if cls.name in FPGA_FAKED:
        from xeda.run_root import ensure_run_root

        ensure_run_root(world.root)
        use_fake_fpga_tools(monkeypatch, world.root / FAKE_TOOLCHAIN)
    # no tool is run for the answer; a flow makes its tools in `init()`, whose version probe is
    # all there is to start
    monkeypatch.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")
    monkeypatch.setattr("xeda.tool.Tool.version_gte", lambda self, *args: True)
    return world


def _settings(cls, world, extra=None):
    base = {**minimal_settings(cls), **EXTRA_SETTINGS.get(cls.name, {}), **(extra or {})}
    return cls.Settings.from_input(base, design_root=world.work, runner_cwd=world.work)


@pytest.mark.parametrize("cls", [c for c, _ in FLOWS], ids=lambda c: c.name)
def test_the_answer_before_init_is_the_answer_after_it(cls, tmp_path, monkeypatch):
    world = _prepare(cls, tmp_path, monkeypatch)
    before, after = _bare_and_initialized(cls, _settings(cls, world), world, monkeypatch)
    assert before == after, cls.name


DECIDING = [c for c, _ in FLOWS if _deciding(c)]
#: the values a setting that decides is given: absent, switches, numbers, text, and the word that
#: asks for a fresh seed (every setting of `settings_samples.PROBES` that can be one of them)
VALUES = [None, True, False, 0, 3, "", "x", "random"]


def test_the_sweep_covers_the_flows_that_decide():
    assert {"nextpnr", "openfpgaloader", "verilator", "ghdl_sim"} <= {c.name for c in DECIDING}
    assert all(issubclass(c, SimFlow) or c.name in {"nextpnr", "openfpgaloader"} for c in DECIDING)


@pytest.mark.parametrize("cls", DECIDING, ids=lambda c: c.name)
def test_the_answer_before_init_holds_for_every_value_of_every_setting(cls, tmp_path, monkeypatch):
    world = _prepare(cls, tmp_path, monkeypatch)
    checked = reasons = 0
    for name in cls.Settings.model_fields:
        if name.endswith("_"):
            continue
        for probe in VALUES:
            try:
                settings = _settings(cls, world, {name: probe})
            except (FlowSettingsError, ValueError, TypeError):
                continue
            try:
                before, after = _bare_and_initialized(cls, settings, world, monkeypatch)
            except Exception:  # noqa: BLE001 - a value the flow refuses at init
                continue
            checked += 1
            reasons += before is not None
            assert before == after, f"{cls.name}: {name}={probe!r}"
    assert checked
    if cls.name == "openfpgaloader":
        assert reasons == checked, "the programmer always runs"
    os.chdir(world.work)


def test_the_sweep_finds_a_flow_that_answers_from_what_init_made(tmp_path, monkeypatch):
    from typing import ClassVar

    from .flow_registry import preserved_flow_registry

    with preserved_flow_registry():

        class ToyLate(Flow):
            """Decides in `always_runs` from a value its `init()` made."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            def init(self) -> None:
                self.late = True

            def always_runs(self):
                return "it was initialized" if getattr(self, "late", False) else None

            def run(self) -> None:
                pass

        world = _world(tmp_path)
        before, after = _bare_and_initialized(
            ToyLate, _settings(ToyLate, world), world, monkeypatch
        )
    assert (before, after) == (None, "it was initialized")
