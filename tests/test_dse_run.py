"""`xeda dse`, end to end: a real design-space exploration on the fake Vivado.

The unit tests around it stop short of running a flow: `Optimizer` stubs that return no batch,
`FlowOutcome` pickled by hand. This runs the command as a user does -- a `pebble` process pool
launching real flow runs, the Fmax search, `best.json` written as the search improves, and the
`--json` document -- and checks what comes out is one consistent, re-runnable record.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flows import VivadoSynth
from xeda.flow_runner.dse.dse_runner import Dse, Optimizer, _variation_delta
from xeda.flow_runner.dse.fmax import FmaxOptimizer
from xeda.flow_runner.settings_layers import merge_layers
from xeda.dataclass import Field
from xeda.flow import FpgaSynthFlow
from .io_flows import _Place


class _DsePlace(_Place):
    """A declared placer with one independent search setting."""

    results_description = {}

    class Settings(_Place.Settings):
        tag: str = Field("base", description="The search variant.")

    def run(self):
        super().run()
        producer_dir = self.inputs.netlist.parent
        recorded = json.loads((producer_dir / "settings.json").read_text())
        self.results["producer"] = str(producer_dir)
        self.results["producer_period"] = recorded["effective_flow_settings"]["clocks"][
            "main_clock"
        ]["period"]
        self.results["period"] = self.settings.main_clock.period
        self.results["pid"] = os.getpid()


class _DeclaredOptimizer(Optimizer):
    """One deterministic worker batch, sufficient to observe candidate plans."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = False
        self.outcomes = []

    def next_batch(self):
        if self.started:
            return None
        self.started = True
        base = self.base_settings.model_dump()
        return [
            merge_layers(base, delta, settings_cls=self.flow_class.Settings)
            for delta in (
                {"tag": "one"},
                {"tag": "two"},
                {"tag": "clock", "clock_period": 12.0},
            )
        ]

    def process_outcome(self, outcome, idx):
        self.outcomes.append(outcome)
        if self.best is None:
            self.best = outcome
            return True
        return False


def test_worker_deltas_do_not_replay_serialized_path_defaults():
    assert _variation_delta(
        {"custom_boards_file": Path("/boards.json"), "tag": "varied"},
        {"custom_boards_file": "/boards.json", "tag": "base"},
    ) == {"tag": "varied"}


def test_declared_dse_variants_resolve_their_graph_in_worker_processes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", "clock_port": "clk"}
    )
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=3)
    best = runner.run(
        _DsePlace, design, flow_settings={"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}
    )
    assert best is not None
    outcomes = {o.settings.tag: o for o in runner.optimizer.outcomes}
    assert set(outcomes) == {"one", "two", "clock"}
    assert outcomes["one"].results["producer"] == outcomes["two"].results["producer"]
    assert outcomes["clock"].results["producer"] != outcomes["one"].results["producer"]
    for name, outcome in outcomes.items():
        assert outcome.results["pid"] != os.getpid()
        assert (
            outcome.results["producer_period"]
            == outcome.results["period"]
            == (12.0 if name == "clock" else 10.0)
        )


class _DseLeaf(FpgaSynthFlow):
    """A synthesis flow that declares no inputs and no outputs."""

    results_description = {}

    class Settings(FpgaSynthFlow.Settings):
        tag: str = Field("base", description="The search variant.")

    def run(self):
        self.results["period"] = self.settings.main_clock.period
        self.results["pid"] = os.getpid()


def test_dse_variants_of_a_flow_that_declares_no_io_run_as_one_node_plans(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", "clock_port": "clk"}
    )
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=3)
    best = runner.run(
        _DseLeaf, design, flow_settings={"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}
    )
    assert best is not None
    outcomes = {o.settings.tag: o for o in runner.optimizer.outcomes}
    assert set(outcomes) == {"one", "two", "clock"}
    assert outcomes["clock"].results["period"] == 12.0 and outcomes["one"].results["period"] == 10.0


def test_a_dse_with_purge_deletes_the_runs_that_did_not_improve_and_keeps_the_best(
    tmp_path, monkeypatch
):
    """`post_cleanup_purge` on an exploration means the exploration deletes the runs that did
    not improve, once their outcome is in. It is not the launch-wide purge of `xeda run`, which
    would delete the best run too: each launch of the exploration leaves its run directory."""
    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", "clock_port": "clk"}
    )
    runner = Dse(
        _DeclaredOptimizer,
        run_root=tmp_path / "run",
        variations={},
        max_workers=1,
        post_cleanup_purge=True,
    )
    runner.run(_DseLeaf, design, flow_settings={"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0})
    outcomes = {o.settings.tag: o for o in runner.optimizer.outcomes}
    best = outcomes["one"].run_path
    assert best is not None and best.is_dir(), "the best run is kept"
    assert outcomes["two"].run_path is None and outcomes["clock"].run_path is None


def test_declared_dse_conflicts_fail_before_logs_or_worker_creation(tmp_path, monkeypatch):
    from xeda.flow import FlowSettingsError

    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [], "top": "t"},
        flow={"__synth": {"fpga": "LFE5U-85F-6BG381C"}},
    )
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
    with pytest.raises(FlowSettingsError, match="disagree"):
        runner.run(
            _DsePlace,
            design,
            flow_overrides={},
            flow_settings={},
            xedaproject=str(_write_dse_project(tmp_path)),
        )
    assert not (tmp_path / "run").exists()


def test_declared_dse_adjustments_are_resolved_before_any_write(tmp_path, monkeypatch):
    from xeda.flow_runner.dse import dse_runner

    monkeypatch.chdir(tmp_path)
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    plans = []
    resolve = runner.resolve

    def resolved(*args, **kwargs):
        plan = resolve(*args, **kwargs)
        plans.append(plan)
        return plan

    def before_logging(*args):
        assert plans[-1].node(_DsePlace.name).settings == runner.optimizer.base_settings
        raise RuntimeError("checked before writing logs")

    monkeypatch.setattr(runner, "resolve", resolved)
    monkeypatch.setattr(dse_runner, "add_file_logger", before_logging)
    with pytest.raises(RuntimeError, match="checked before writing logs"):
        runner.run_flow(_DsePlace, design, {"fpga": "LFE5U-25F-6BG256C"})


def _write_dse_project(tmp_path):
    path = tmp_path / "project.toml"
    path.write_text('[flows.__dse_place]\nfpga.part = "LFE5U-25F-6BG256C"\n')
    return path


TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


def test_an_exploration_records_one_rerunnable_best_run(tmp_path):
    """An exploration records one rerunnable best run."""
    for name in ("sqrt.vhdl", "sqrt.yaml", "tb_sqrt.py"):
        shutil.copy(SQRT / name, tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "dse",
            "vivado_synth",
            "--design",
            "sqrt.yaml",
            "--max-workers",
            "2",
            "--init-freq-low",
            "100",
            "--init-freq-high",
            "300",
            "--dse-settings",
            "max_failed_iters=2",
            "--json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    document = json.loads(proc.stdout)
    assert document["success"] is True
    # the design's name, and the file it was read from
    assert document["design"] == "sqrt"
    assert Path(document["design_file"]).samefile(tmp_path / "sqrt.yaml")
    best = document["best"]

    # The best run is a real run directory, holding the results it was chosen for.
    run_path = Path(best["run_path"])
    recorded_results = json.loads((run_path / "results.json").read_text())
    assert recorded_results["success"] is True
    assert best["results"]["Fmax"] == recorded_results["Fmax"]

    # The search's own record of it is the same view the CLI printed.
    (best_file,) = (tmp_path / "xeda_run").glob("fmax_sqrt_vivado_synth_*.json")
    recorded = json.loads(best_file.read_text())
    assert recorded["best"] == best

    # ... and both are inputs again: the best settings validate as the flow's settings, and the
    # design recorded beside them is the design that was explored.
    again = VivadoSynth.Settings.from_input(best["settings"], design_root=tmp_path)
    assert (
        again.main_clock
        and again.main_clock.period == best["settings"]["clocks"]["main_clock"]["period"]
    )
    explored = Design.from_file(tmp_path / "sqrt.yaml")
    assert Design(**recorded["design"]).rtl_hash == explored.rtl_hash

    # the start directory holds the design's files and the run root, nothing else
    assert {p.name for p in tmp_path.iterdir()} == {
        "sqrt.vhdl",
        "sqrt.yaml",
        "tb_sqrt.py",
        "xeda_run",
    }
    assert list((tmp_path / "xeda_run" / "Logs").glob("xeda_*.log"))


def test_an_exploration_that_fails_at_input_makes_no_run_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.toml").write_text(
        'name = "bad"\n[rtl]\nsources = ["missing.vhdl"]\ntop = "bad"\n'
    )
    result = CliRunner().invoke(cli, ["dse", "vivado_synth", "--design", "bad.toml", "--json"])
    assert result.exit_code != 0 and not (tmp_path / "xeda_run").exists()


def _dse(tmp_path, design_toml: str, *args):
    """Configure a design space exploration run."""
    (tmp_path / "sqrt.vhdl").write_text((SQRT / "sqrt.vhdl").read_text())
    (tmp_path / "d.toml").write_text(design_toml)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "dse",
            "vivado_synth",
            "--design",
            "d.toml",
            *args,
            "--json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, json.loads(proc.stdout)


BARE = 'name = "sqrt"\n[rtl]\nsources = ["sqrt.vhdl"]\ntop = "sqrt"\nclock.port = "clk"\n'


def test_a_search_without_its_bounds_names_them(tmp_path):
    """Without `--init-freq-low/high`, the CLI handed the optimizer `None` for both and the user
    got pydantic's `Input should be a valid number [type=float_type, input_value=None]`."""
    proc, document = _dse(tmp_path, BARE + '[flows.vivado_synth]\nfpga.part = "xc7a12tcsg325-1"\n')
    assert proc.returncode != 0 and document["success"] is False
    error = document["error"]
    assert error["type"] == "FlowSettingsError"
    assert "init_freq_low" in error["message"] and "init_freq_high" in error["message"]
    # the optimizer is refused before the design is read: the file is named, the design is not
    assert document["design"] is None
    assert Path(document["design_file"]).samefile(tmp_path / "d.toml")
    assert "None" not in error["message"] and "float_type" not in error["message"]


def test_a_search_without_a_device_says_so_before_starting_any_run(tmp_path):
    """A search without a device says so before starting any run."""
    proc, document = _dse(tmp_path, BARE, "--init-freq-low", "100", "--init-freq-high", "300")
    assert proc.returncode != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowSettingsException"
    assert "`fpga`" in document["error"]["message"]
    assert document["design"] == "sqrt", "the design had loaded"
    assert not list(tmp_path.glob("xeda_run/**/settings.json")), "no run was started"


@pytest.mark.skipif(sys.platform == "win32", reason="no run-directory locks")
def test_coordinator_purge_waits_for_a_worker_directory_reader(tmp_path):
    import select

    from xeda.flow_runner.run_lock import lock_file, run_dir_read_lock
    from .test_read_locks import _environment, _message

    path = tmp_path / "run" / "d" / "worker"
    path.mkdir(parents=True)
    (path / "result.txt").write_text("read by another consumer")
    code = """
import sys
from pathlib import Path
from xeda.flow_runner import run_lock
from xeda.flow_runner.dse.dse_runner import _purge_run
original = run_lock.fcntl.flock
def flock(fd, mode):
    if mode == run_lock.fcntl.LOCK_EX:
        print("waiting", flush=True)
    return original(fd, mode)
run_lock.fcntl.flock = flock
_purge_run(Path(sys.argv[1]), Path(sys.argv[2]), "worker")
print("done", flush=True)
"""
    writer = None
    try:
        with run_dir_read_lock(path):
            inode = lock_file(path).stat().st_ino
            writer = subprocess.Popen(
                [sys.executable, "-c", code, str(path), str(tmp_path / "run")],
                env=_environment(),
                stdout=subprocess.PIPE,
                text=True,
            )
            _message(writer, "waiting")
            assert not select.select([writer.stdout], [], [], 0.2)[0]
            assert path.is_dir()
        _message(writer, "done")
        assert writer.wait(timeout=20) == 0
        assert not path.exists() and lock_file(path).stat().st_ino == inode
    finally:
        if writer is not None:
            if writer.poll() is None:
                writer.kill()
            writer.wait(timeout=20)
            if writer.stdout:
                writer.stdout.close()


class _PromotingOptimizer(_DeclaredOptimizer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.round = 0

    def next_batch(self):
        from xeda.flow_runner.settings_layers import merge_layers

        self.round += 1
        if self.round > 2:
            return None
        delta = {"clocks": {"main_clock": {"period": 10.0 + 2.0 * self.round}}}
        if self.round == 1:
            delta["tag"] = "promoted"
        return [
            merge_layers(
                self.base_settings.model_dump(),
                delta,
                settings_cls=self.flow_class.Settings,
            )
        ]

    def process_outcome(self, outcome, idx):
        self.outcomes.append(outcome)
        self.best = outcome
        self.base_settings = outcome.settings
        return True


def test_dse_two_rounds_after_promoting_agreed_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", "clock_port": "clk"}
    )
    runner = Dse(_PromotingOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
    assert runner.run(
        _DsePlace, design, flow_settings={"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}
    )
    assert len(runner.optimizer.outcomes) == 2
    assert [o.results["producer_period"] for o in runner.optimizer.outcomes] == [12.0, 14.0]
    assert [o.settings.tag for o in runner.optimizer.outcomes] == ["promoted", "promoted"]


class _FmaxPlace(_DsePlace):
    """A declared placer that reports a maximum frequency."""

    results_description = {}

    def run(self):
        super().run()
        self.results["Fmax"] = 150.0


class _ObservedFmax(FmaxOptimizer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def process_outcome(self, outcome, idx):
        self.outcomes.append(outcome)
        return super().process_outcome(outcome, idx)


def test_real_fmax_optimizer_reaches_second_declared_batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [], "top": "t", "clock_port": "clk"}
    )
    runner = Dse(
        _ObservedFmax,
        optimizer_settings={
            "init_freq_low": 100.0,
            "init_freq_high": 300.0,
            "stop_after_no_improves": 2,
        },
        run_root=tmp_path / "run",
        variations={},
        max_workers=1,
        max_failed_iters_with_best=1,
    )
    assert runner.run(
        _FmaxPlace, design, flow_settings={"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}
    )
    assert len(runner.optimizer.outcomes) >= 2


class _IdleFmax(FmaxOptimizer):
    """The Fmax search, stopped before its first batch: it has taken its variations, and no run
    has started."""

    def next_batch(self):
        return None


def _idle_search(tmp_path, monkeypatch, **dse_settings) -> Dse:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.v").write_text("module a(input clk); endmodule\n")
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["a.v"], "top": "a", "clock_port": "clk"}
    )
    runner = Dse(
        _IdleFmax,
        optimizer_settings={"init_freq_low": 100.0, "init_freq_high": 200.0},
        run_root=tmp_path / "run",
        max_workers=1,
        **dse_settings,
    )
    runner.run(
        "vivado_alt_synth", design, flow_settings={"fpga": "xc7a12tcsg325-1", "clock_period": 5.0}
    )
    return runner


def test_a_search_reorders_its_own_variations_never_the_default_table(tmp_path, monkeypatch):
    """A search promotes the choices of its best run to the front of its lists. The table of
    default variations is the class's, and the next search in the process starts from it."""
    pristine = copy.deepcopy(FmaxOptimizer.default_variations)
    # on a copy of the table: a search that wrote to the real one would hand that to later tests
    monkeypatch.setattr(FmaxOptimizer, "default_variations", copy.deepcopy(pristine))
    runner = _idle_search(tmp_path, monkeypatch)
    variations = runner.optimizer.variations
    assert variations == pristine["vivado_alt_synth"]
    for choices in variations.values():
        choices.reverse()
    assert FmaxOptimizer.default_variations == pristine
    assert runner.optimizer.variations != pristine["vivado_alt_synth"]


def test_a_search_reorders_its_own_variations_never_the_callers(tmp_path, monkeypatch):
    given = {"synth.strategy": ["Timing", "ExtraTiming", "ExtraTimingAlt"]}
    runner = _idle_search(tmp_path, monkeypatch, variations=given)
    for choices in runner.optimizer.variations.values():
        choices.reverse()
    assert given == {"synth.strategy": ["Timing", "ExtraTiming", "ExtraTimingAlt"]}
    assert runner.settings.variations == given


def test_a_promotion_leaves_the_default_table_as_it_was(monkeypatch):
    """The promotion itself: the best run's choices move to the front of the search's lists."""
    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import FlowOutcome

    pristine = copy.deepcopy(FmaxOptimizer.default_variations)
    monkeypatch.setattr(FmaxOptimizer, "default_variations", copy.deepcopy(pristine))
    optimizer = FmaxOptimizer(max_workers=2, init_freq_low=100.0, init_freq_high=200.0)
    optimizer.variations = FmaxOptimizer.default_variations["vivado_alt_synth"]
    optimizer.num_variations = 2  # reached when a search stalls
    optimizer.variation_choices = [{"synth.strategy": 2, "impl.strategy": 3}]
    results = Flow.Results()
    results["Fmax"] = 150.0
    results.success = True
    outcome = FlowOutcome(settings=Flow.Settings(), results=results, timestamp=None, run_path=None)
    assert optimizer.process_outcome(outcome, 0)
    assert optimizer.variations["synth.strategy"][0] == "Timing"  # the search promoted it
    assert FmaxOptimizer.default_variations == pristine  # ... in its own copy
