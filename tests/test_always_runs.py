"""A flow that cannot be reused says why, per instance (`Flow.always_runs`); seeds are settings
with fixed defaults, so a default configuration is reusable.

A flow always runs when it changes the world outside its run directory (programs a device), when
its settings ask for a fresh random seed, or when it reads something no trace can verify (a pin
constraint file fetched from a URL). The launcher then skips the freshness check, keeps no trace,
and reports the reason.
"""

from pathlib import Path
from typing import ClassVar, Optional

import pytest

from xeda import Design
from xeda.cocotb import Cocotb, CocotbSettings
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr, Openfpgaloader, Verilator

RUNS: list[str] = []


@pytest.fixture
def design(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    return Design(name="toy", rtl={"sources": ["a.v"], "top": "a"}, design_root=tmp_path)


def _flow(cls, design, tmp_path, **settings):
    return cls(
        cls.Settings.from_input(settings, design_root=design.root_path, runner_cwd=tmp_path),
        design,
        run_path=tmp_path / "run",
    )


def test_a_flow_that_always_runs_says_why_and_keeps_no_trace(tmp_path, design):
    class ToyProgrammer(Flow):
        """Pretends to program a device when asked to."""

        results_description: ClassVar[dict[str, str]] = {}
        program: ClassVar[bool] = True

        def always_runs(self) -> Optional[str]:
            return "it programs a device" if self.program else None

        def run(self) -> None:
            RUNS.append(self.name)

    try:
        for rebuild_all in (False, True):
            RUNS.clear()
            runner = DefaultRunner(
                tmp_path / "xeda_run", display_results=False, rebuild_all=rebuild_all
            )
            flow = runner.launch_flow(ToyProgrammer, design, {})
            assert RUNS == ["toy_programmer"] and not flow.reused
            assert flow.stale_reason == "it programs a device"
            assert not (flow.run_path / "trace.json").exists()
        ToyProgrammer.program = False  # the same flow, not programming: an ordinary node
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        runner.launch_flow(ToyProgrammer, design, {})
        RUNS.clear()
        flow = runner.launch_flow(ToyProgrammer, design, {})
        assert RUNS == [] and flow.reused
    finally:
        for name in (ToyProgrammer.name, ToyProgrammer.__name__):
            registered_flows.pop(name, None)


def test_is_action_is_gone():
    assert not hasattr(Flow, "is_action")


def test_openfpgaloader_programs_a_device(tmp_path, design):
    flow = _flow(Openfpgaloader, design, tmp_path, fpga={"part": "LFE5U-25F-6BG256C"})
    assert flow.always_runs() == "it programs a device"


def test_nextpnr_draws_a_new_seed_only_when_asked(tmp_path, design):
    fpga = {"part": "LFE5U-25F-6BG256C"}
    assert _flow(Nextpnr, design, tmp_path, fpga=fpga).always_runs() is None
    randomized = _flow(Nextpnr, design, tmp_path, fpga=fpga, randomize_seed=True)
    assert randomized.always_runs() == "it draws a new random seed"


def test_verilator_s_random_initialization_has_a_fixed_seed(tmp_path, design):
    """`+verilator+seed+0` would pick a seed from the system: the default is a fixed 1."""
    assert Verilator.Settings().random_seed == 1
    assert _flow(Verilator, design, tmp_path, random_init=True).always_runs() is None
    fresh = _flow(Verilator, design, tmp_path, random_seed="random", random_init=True)
    assert fresh.always_runs() == "it draws a new random seed"
    # the seed is used only by the random initialization, which is off by default
    assert Verilator.Settings().random_init is False
    unused = _flow(Verilator, design, tmp_path, random_seed="random")
    assert unused.always_runs() is None  # no random initialization, no seed drawn


def test_verilator_rejects_the_seed_that_means_random(tmp_path, design):
    from xeda.flow import FlowSettingsError

    with pytest.raises(FlowSettingsError, match="random_seed"):
        _flow(Verilator, design, tmp_path, random_seed=0)


def test_cocotb_seeds_python_s_random_with_a_fixed_default(tmp_path):
    assert CocotbSettings().random_seed == 1
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    (tmp_path / "tb.py").write_text("import cocotb\n")
    design = Design(
        name="toy",
        rtl={"sources": ["a.v"], "top": "a"},
        tb={"sources": ["tb.py"], "top": "tb", "cocotb": True},
        design_root=tmp_path,
    )
    env = Cocotb(sim_name="verilator").env(design)
    assert env["COCOTB_RANDOM_SEED"] == "1"
    fresh = Cocotb(sim_name="verilator", random_seed="random").env(design)
    assert "COCOTB_RANDOM_SEED" not in fresh


def test_cocotb_rejects_null_seed_with_migration_message():
    with pytest.raises(ValueError) as exc:
        CocotbSettings.model_validate_json('{"random_seed": null}')
    assert (
        '`random_seed: null` was removed: use "random" for a new seed on every run, or an integer'
        in str(exc.value)
    )


def test_a_cocotb_testbench_asking_for_a_fresh_seed_always_runs(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    (tmp_path / "tb.py").write_text("import cocotb\n")
    design = Design(
        name="toy",
        rtl={"sources": ["a.v"], "top": "a"},
        tb={"sources": ["tb.py"], "top": "tb", "cocotb": True},
        design_root=tmp_path,
    )
    assert _flow(Verilator, design, tmp_path).always_runs() is None
    fresh = _flow(Verilator, design, tmp_path, cocotb={"random_seed": "random"})
    assert fresh.always_runs() == "it draws a new random seed"


def test_a_board_constraint_file_fetched_from_a_url_always_runs(tmp_path, design):
    """Plan 3 fetches such a file once, pinned by hash; until then no trace can verify it."""
    assert _flow(Nextpnr, design, tmp_path, board="ulx3s_85f").always_runs() is None
    database = tmp_path / "boards.toml"
    database.write_text(
        '[REMOTE]\nfpga.part = "LFE5U-85F-6BG381C"\n' 'lpf = "https://example.invalid/pins.lpf"\n'
    )
    board = {"board": "REMOTE", "custom_boards_file": database}
    flow = _flow(Nextpnr, design, tmp_path, **board)
    assert flow.always_runs() == "its constraints are fetched from a URL"
    explicit = tmp_path / "pins.lpf"
    explicit.write_text("\n")
    pinned = _flow(Nextpnr, design, tmp_path, **board)
    pinned.inputs.constraints = [explicit]
    assert pinned.always_runs() is None
    assert Path(explicit).is_file()
