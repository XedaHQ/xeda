"""A launch runs the generator of the design, into a storage that the configuration names.

The storage is `<run root>/.generated/<design>/<key>/`. Loading a design and planning a launch run
and write nothing. The generator runs in the launch, after every declarative check and before the
first producer, and a second launch of the same configuration reuses the storage. The flows get a
copy of the design whose generated sources name their files in the storage; the design that the
caller gave never changes.
"""

import os
import stat
import sys
import threading
from pathlib import Path

import pytest
import yaml

from xeda.flow_runner import run_lock
from xeda.frozen import thaw
from xeda.flow_runner.launch_generation import generation_key
from xeda.flow_runner.run_lock import lock_file
from xeda.generation import RECORD_FORMAT, RECORD_NAME
from xeda.outcomes import Outcome

from .flow_registry import preserved_flow_registry
from .generation_world import World, blocking_reader_flow, input_reader_flow, reader_flow
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no storage locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _probe(storage: Path, mode: str) -> str:
    """Whether another process could lock `storage` in `mode` now: "free" or "blocked"."""
    from .test_storage_locks import _probe as probe

    return probe(storage, mode)


def _tree(base: Path) -> dict[str, tuple[int, int]]:
    """Every path under `base` with its size and modification time: what a write changes."""
    found = {}
    for path in sorted(base.rglob("*")):
        st = path.lstat()
        found[str(path.relative_to(base))] = (st.st_size, st.st_mtime_ns)
    return found


# ---------------------------------------------------------------- loading and planning write nothing


def test_loading_a_design_with_a_generator_runs_nothing_and_writes_nothing(tmp_path):
    world = World(tmp_path)
    before = _tree(tmp_path)
    design = world.load()
    assert design.rtl.generator is not None
    assert [(s.path, s.generated, s.located) for s in design.rtl.sources] == [
        (Path("gen/top.v"), True, False)
    ]
    assert world.runs == 0 and _tree(tmp_path) == before
    assert not world.run_root.exists()


def test_loading_and_planning_a_generator_design_in_a_read_only_tree_writes_nothing(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    runner = world.runner()
    frozen = [world.root, *world.root.rglob("*")]
    try:
        for path in frozen:
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        before = _tree(tmp_path)
        design = world.load()
        plan = runner.plan(flow, design)
        _plan, states = runner.plan_with_freshness(flow, design)
        assert [node.name for node in plan.nodes] == [flow.name]
        assert states[flow.name].state == "unknown"
        assert states[flow.name].reason == "the generator has not run"
        assert _tree(tmp_path) == before
    finally:
        for path in frozen:
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    assert world.runs == 0 and not world.run_root.exists()


# ---------------------------------------------------------------- the storage and the launch


def test_a_launch_generates_into_the_storage_and_the_flow_reads_it(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    (storage,) = world.storages()
    assert (storage / "gen" / "top.v").read_text() == "// one\n// W=8\n"
    assert (storage / RECORD_NAME).is_file()
    assert result.nodes[flow.name].record.metrics["seen"] == "// one\n// W=8\n"
    assert storage.parent.name == "generated" and len(storage.name) == 16
    assert not (world.root / "gen").exists(), "the design tree is not written"
    assert world.runs == 1


def test_a_second_launch_of_one_configuration_generates_nothing(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    assert world.launch(flow).outcome is Outcome.PASSED
    again = world.launch(flow)
    assert again.outcome is Outcome.PASSED
    assert world.runs == 1
    assert again.nodes[flow.name].disposition.value == "reused"


def test_the_design_tree_is_unchanged_by_a_launch(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    before = _tree(world.root)
    assert world.launch(flow).outcome is Outcome.PASSED
    assert _tree(world.root) == before


def test_the_generator_runs_in_the_design_root_with_the_two_variables_set(tmp_path):
    world = World(tmp_path)
    assert world.launch(reader_flow()).outcome is Outcome.PASSED
    (storage,) = world.storages()
    cwd, design_root, generated = world.where
    assert Path(cwd) == Path(design_root) == world.root.resolve()
    assert Path(generated) == storage.resolve()


@pytest.mark.parametrize("cwd", ["sub", "$DESIGN_ROOT/sub", "${DESIGN_DIR}/sub"])
def test_a_generator_with_a_cwd_runs_there(tmp_path, cwd):
    world = World(tmp_path, generator={"cwd": cwd})
    (world.root / "sub").mkdir()
    # the script writes into XEDA_GENERATED, whatever its working directory is
    assert world.launch(reader_flow()).outcome is Outcome.PASSED
    cwd, design_root, _ = world.where
    assert Path(cwd) == Path(design_root) / "sub"


@pytest.mark.parametrize("cwd", ["$DESIGN_ROOT/sub", "${DESIGN_DIR}/sub", "absolute"])
def test_equivalent_generator_working_directories_have_one_key(tmp_path, cwd):
    world = World(tmp_path, generator={"cwd": "sub"})
    (world.root / "sub").mkdir()
    key = generation_key(world.load())[0]
    value = str(world.root / "sub") if cwd == "absolute" else cwd
    world.design_file.write_text(world.design_file.read_text().replace("'sub'", repr(value)))
    assert generation_key(world.load())[0] == key


@pytest.mark.parametrize("cwd", ["$DESIGN_ROOT/../outside", "${DESIGN_DIR}/../outside"])
def test_an_expanded_generator_working_directory_cannot_leave_the_design_root(tmp_path, cwd):
    from xeda.design import DesignValidationError

    world = World(tmp_path, generator={"cwd": cwd})
    (tmp_path / "outside").mkdir()
    with pytest.raises(DesignValidationError, match="below the design root"):
        world.load()


def test_a_generator_built_before_loading_keeps_variables_until_the_root_is_known(tmp_path):
    from xeda.design import Generator

    generator = Generator(cwd="$DESIGN_ROOT/../outside")
    with pytest.raises(ValueError, match="below the design root"):
        generator.lookup_directory(tmp_path / "design")


def test_a_chisel_generator_runs_in_its_project_directory(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "mill.json"
    mill = bin_dir / "mill"
    mill.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "target = sys.argv[sys.argv.index('--target-dir') + 1]\n"
        "(Path(target) / 'gen').mkdir(exist_ok=True)\n"
        "(Path(target) / 'gen' / 'top.v').write_text('// chisel\\n')\n"
        f"Path({str(record)!r}).write_text(json.dumps({{'cwd': os.getcwd(), 'argv': sys.argv[1:]}}))\n"
    )
    mill.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    world = World(
        tmp_path,
        generator={
            "class": "chisel",
            "project": "top",
            "args": ["--target-dir", "$XEDA_GENERATED"],
            "sources": ["spec.txt"],
        },
    )
    # the generic arguments of the helper do not apply to a Chisel recipe
    text = world.design_file.read_text().replace(f"    executable: {sys.executable!r}\n", "")
    world.design_file.write_text(text)
    result = world.launch(reader_flow())
    assert result.outcome is Outcome.PASSED, result.error
    import json

    seen = json.loads(record.read_text())
    (storage,) = world.storages()
    assert Path(seen["cwd"]).resolve() == world.root.resolve()
    assert seen["argv"][0] == "top.run"
    assert Path(seen["argv"][-1]) == storage.resolve()


def test_generated_source_paths_stay_names_before_and_after_a_launch(tmp_path):
    world = World(tmp_path)
    design = world.load()
    before = [s.path for s in design.rtl.sources if s.generated]
    assert world.launch(reader_flow(), design=design).outcome is Outcome.PASSED
    assert [s.path for s in design.rtl.sources if s.generated] == before == [Path("gen/top.v")]
    assert not design.rtl.sources[0].located


def test_a_generator_failure_is_an_error_of_the_whole_launch(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    (world.root / "fail").write_text("")
    result = world.launch(flow)
    assert result.outcome is Outcome.FAILED
    assert result.error is not None and result.error.code == "generator_failed"
    assert result.error.node is None and "failed" in result.error.message
    assert [(n.name, n.outcome) for n in result.nodes.values()] == [(flow.name, Outcome.SKIPPED)]
    assert world.storages() == [], "a failed generation leaves no storage"
    left = [path for path in world.run_root.glob("generated/*") if path.suffix != ".lock"]
    assert not left, "no run directory"
    # the design can be mended and launched again
    (world.root / "fail").unlink()
    assert world.launch(flow).outcome is Outcome.PASSED


def test_a_declared_name_the_generator_did_not_write_fails_the_node(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/top.v", "gen/other.v"])
    result = world.launch(flow)
    assert result.outcome is Outcome.FAILED
    node = result.nodes[flow.name]
    assert node.error is not None and node.error.code == "missing_generated"
    assert "gen/other.v" in node.error.message
    assert world.runs == 1 and len(world.storages()) == 1


def test_a_declared_name_that_exists_in_the_working_directory_gets_a_hint(tmp_path):
    """The generator wrote the file where it runs instead of under `$XEDA_GENERATED`: the message
    says what lies there and where Xeda reads, and does not say the generator wrote it."""
    flow = reader_flow()
    world = World(tmp_path)
    script = world.root / "gen.py"
    script.write_text(
        script.read_text().replace('out = Path(os.environ["XEDA_GENERATED"])', "out = Path.cwd()")
    )
    node = world.launch(flow).nodes[flow.name]
    assert node.error is not None and node.error.code == "missing_generated"
    written = world.root.resolve() / "gen" / "top.v"
    assert written.is_file()
    assert f"A file of that name exists at `{written}`" in node.error.message
    assert "only under `$XEDA_GENERATED`" in node.error.message
    assert "`generated_sources`" in node.error.message, "the earlier advice stays"


def test_a_missing_name_with_no_file_in_the_working_directory_gets_no_hint(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/top.v", "gen/other.v"])
    node = world.launch(flow).nodes[flow.name]
    assert node.error is not None and "exists at" not in node.error.message


def test_a_pattern_that_matches_nothing_fails_the_node_and_names_the_declaration(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/top.v", "lib/*.v"])
    node = world.launch(flow).nodes[flow.name]
    assert node.error is not None and node.error.code == "missing_generated"
    assert "lib/*.v" in node.error.message


def test_a_pattern_gives_the_flow_each_file_it_matches_in_order(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/*.v"])
    script = (world.root / "gen.py").read_text()
    (world.root / "gen.py").write_text(
        script
        + "(out / 'gen' / 'a.v').write_text('// a\\n')\n"
        + "(out / 'gen' / 'z.txt').write_text('not verilog\\n')\n"
    )
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    assert result.nodes[flow.name].record.metrics["seen"] == "// a\n// one\n// W=8\n"


def test_the_configuration_names_a_generated_source_by_its_declaration(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/*.v"])
    runner = world.runner()
    design = world.load()
    plan = runner.plan(flow, design)
    planned = plan.nodes[0]
    assert thaw(planned.configuration["rtl"]["sources"]) == [{"path": "gen/*.v", "type": "Verilog"}]
    result = world.launch(flow, runner, design)
    assert result.outcome is Outcome.PASSED, result.error
    record = result.nodes[flow.name].record
    assert thaw(record.configuration["rtl"]["sources"]) == [{"path": "gen/*.v", "type": "Verilog"}]
    assert record.identity == planned.flowrun_hash


def test_a_record_names_a_generated_input_by_its_name_and_the_key_of_its_storage(tmp_path):
    flow = input_reader_flow()
    world = World(tmp_path)
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    record = result.nodes[flow.name].record
    (item,) = record.inputs
    (source,) = item.sources
    assert (source.path, source.generated) == ("gen/top.v", True) and source.digest
    (storage,) = world.storages()
    assert item.generation == storage.name
    assert str(world.run_root) not in repr(record.to_dict())


# ---------------------------------------------------------------- the key of the storage


def _key(world: World, environ=None) -> str:
    return generation_key(world.load(), environ or dict(os.environ))[0]


def test_the_key_does_not_depend_on_where_the_design_is(tmp_path):
    counter = tmp_path / "runs.log"
    here = World(tmp_path / "a", directory="design", counter=counter)
    there = World(tmp_path / "b" / "deeper", directory="design", counter=counter)
    key = _key(here)
    assert key == _key(there) and len(key) == 16


def test_the_key_depends_on_every_part_of_the_configuration(tmp_path):
    base = World(tmp_path)
    reference = _key(base)

    def variant(**kwargs) -> str:
        return _key(World(tmp_path, **kwargs))

    assert variant(width="16") != reference, "an argument of the recipe"
    assert variant(generator={"env": {"A": "1"}}) != reference, "the env of the recipe"
    assert variant(generator={"cwd": "sub"}) != reference, "the working directory"
    assert variant(parameters="{W: 4}") != reference, "a design parameter"
    assert variant(sources=["gen/top.v", "gen/more.v"]) != reference, "the declarations"
    assert variant(extra="tb:\n  top: tb\n") != reference, "the testbench"
    assert variant(extra="language:\n  vhdl:\n    standard: '08'\n") != reference, "the language"
    (base.root / "spec.txt").write_text("two\n")
    assert _key(base) != reference, "the content of a generator input"


def test_the_key_ignores_what_the_generator_writes_and_the_environment(tmp_path, monkeypatch):
    flow = reader_flow()
    world = World(tmp_path)
    reference = _key(world)
    assert world.launch(flow).outcome is Outcome.PASSED
    (storage,) = world.storages()
    (storage / "gen" / "top.v").write_text("// edited\n")
    assert _key(world) == reference, "generated bytes"
    monkeypatch.setenv("SOMETHING_INHERITED", "1")
    assert _key(world) == reference, "the inherited environment"


def test_a_change_of_the_executable_makes_another_key(tmp_path):
    script = tmp_path / "tool"
    script.write_text(f"#!{sys.executable}\nprint(1)\n")
    script.chmod(0o755)
    world = World(tmp_path, generator={"executable": str(script), "args": []})
    first = _key(world)
    script.write_text(f"#!{sys.executable}\nprint(2)\n")
    assert _key(world) != first


def test_the_variants_of_a_design_keep_their_own_bytes(tmp_path):
    flow = reader_flow()
    small = World(tmp_path, directory="small", width="8")
    large = World(tmp_path, directory="large", width="16")
    runner = small.runner(hashed_run_dirs=True)
    seen = {}
    for world in (small, large):
        result = world.launch(flow, runner)
        assert result.outcome is Outcome.PASSED, result.error
        seen[world] = result.nodes[flow.name].record.metrics["seen"]
    assert seen[small].endswith("W=8\n") and seen[large].endswith("W=16\n")
    assert len(small.storages()) == 2
    texts = sorted((s / "gen" / "top.v").read_text() for s in small.storages())
    assert texts == ["// one\n// W=16\n", "// one\n// W=8\n"]


# ---------------------------------------------------------------- the environment is evidence


def test_a_change_of_the_inherited_environment_regenerates_in_the_same_storage(
    tmp_path, monkeypatch
):
    flow = reader_flow()
    world = World(tmp_path)
    monkeypatch.setenv("LICENSE_HINT", "a")
    assert world.launch(flow).outcome is Outcome.PASSED
    (before,) = world.storages()
    assert world.launch(flow).outcome is Outcome.PASSED and world.runs == 1
    monkeypatch.setenv("LICENSE_HINT", "b")
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 2 and world.storages() == [before]
    monkeypatch.delenv("LICENSE_HINT")
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 3


def test_a_variable_of_a_session_is_no_evidence(tmp_path, monkeypatch):
    flow = reader_flow()
    world = World(tmp_path)
    monkeypatch.setenv("SHLVL", "1")
    assert world.launch(flow).outcome is Outcome.PASSED
    monkeypatch.setenv("SHLVL", "2")
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 1


def test_a_generator_with_its_own_env_inherits_nothing(tmp_path, monkeypatch):
    flow = reader_flow()
    world = World(tmp_path, generator={"env": {"PATH": os.environ["PATH"]}})
    monkeypatch.setenv("LICENSE_HINT", "a")
    assert world.launch(flow).outcome is Outcome.PASSED
    monkeypatch.setenv("LICENSE_HINT", "b")
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 1


# ---------------------------------------------------------------- what planning says


def test_a_plan_says_unknown_until_the_generator_has_run(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    runner = world.runner()

    def state(**settings):
        return world.runner(**settings).plan_with_freshness(flow, world.load())[1][flow.name]

    assert (state().state, state().reason) == ("unknown", "the generator has not run")
    assert world.launch(flow, runner).outcome is Outcome.PASSED
    assert state().state == "fresh"
    (world.root / "spec.txt").write_text("two\n")
    assert state().state == "unknown", "the key changed: no storage for it yet"
    assert world.runs == 1, "planning generated nothing"
    assert state(rebuild_all=True).state == "stale"
    assert state(clean=True).reason == "clean"


def test_a_plan_says_stale_for_a_node_that_reads_a_declaration_nothing_matched(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, sources=["gen/top.v", "gen/other.v"])
    assert world.launch(flow).nodes[flow.name].error.code == "missing_generated"
    state = world.runner().plan_with_freshness(flow, world.load())[1][flow.name]
    assert state.state == "stale" and "gen/other.v" in state.reason


def test_a_plan_says_unknown_for_a_generator_nothing_can_judge(tmp_path):
    flow = reader_flow()
    world = World(tmp_path, generator={"always_runs": True})
    assert world.launch(flow).outcome is Outcome.PASSED
    state = world.runner().plan_with_freshness(flow, world.load())[1][flow.name]
    assert (state.state, state.reason) == ("unknown", "the generator has not run")


def test_a_plan_does_not_hash_the_content_of_generated_sources(tmp_path):
    flow = reader_flow()
    world = World(tmp_path)
    plan = world.runner().plan(flow, world.load())
    assert plan.nodes[0].flowrun_hash and not world.run_root.exists()


# ---------------------------------------------------------------- two launches at once


def _gated_generator(world: World, started: Path, gate: Path) -> None:
    """Make the generator signal that it runs and wait until the test opens the gate."""
    script = (world.root / "gen.py").read_text()
    (world.root / "gen.py").write_text(
        "import os\n"
        f"open({str(started)!r}, 'wb').close()\n"
        f"open({str(gate)!r}, 'rb').read()\n" + script
    )


def test_two_launches_generate_one_configuration_once(tmp_path, monkeypatch):
    flow = reader_flow()
    world = World(tmp_path)
    started, gate = tmp_path / "started", tmp_path / "gate"
    os.mkfifo(started)
    os.mkfifo(gate)
    _gated_generator(world, started, gate)
    results = {}
    waiting = threading.Event()
    paths = []

    def hook(path: Path) -> None:
        paths.append(path)
        waiting.set()

    def launch(name: str) -> None:
        results[name] = world.launch(flow, world.runner())

    first = threading.Thread(target=launch, args=("first",))
    first.start()
    with open(started, "rb"):  # opens when the generator of the first launch has started
        pass
    monkeypatch.setattr(run_lock, "wait_hook", hook)
    second = threading.Thread(target=launch, args=("second",))
    second.start()
    try:
        assert waiting.wait(HANG_GUARD), "the second launch did not wait for the storage"
    finally:
        with open(gate, "wb") as opened:
            opened.write(b"go")
        first.join(HANG_GUARD)
        second.join(HANG_GUARD)
    assert [r.outcome for r in results.values()] == [Outcome.PASSED, Outcome.PASSED]
    assert world.runs == 1, "the second launch waited, found the record and reused the storage"
    assert world.storages()
    assert paths[0] == lock_file(world.run_root / "generated" / flow.name), (
        "the second launch waited for the run directory of the first, the first lock of the set, "
        "holding nothing"
    )


# ---------------------------------------------------------------- leases


def _fifos(tmp_path: Path) -> tuple[Path, Path]:
    started, gate = tmp_path / "reading", tmp_path / "reading-gate"
    os.mkfifo(started)
    os.mkfifo(gate)
    return started, gate


def _counting_generator(world: World) -> None:
    """Make the generator write the number of its runs into `gen/top.v`."""
    (world.root / "gen.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "with open(sys.argv[1], 'a') as counter:\n"
        "    counter.write('ran\\n')\n"
        "count = len(Path(sys.argv[1]).read_text().splitlines())\n"
        "out = Path(os.environ['XEDA_GENERATED'])\n"
        "(out / 'gen').mkdir(exist_ok=True)\n"
        "(out / 'gen' / 'top.v').write_text(f'// run {count}\\n')\n"
    )


def test_a_forced_regeneration_waits_while_a_tool_reads_the_storage(tmp_path, monkeypatch):
    started, gate = _fifos(tmp_path)
    flow = blocking_reader_flow(started, gate)
    world = World(tmp_path)
    _counting_generator(world)
    first: dict = {}
    second: dict = {}
    waiting = threading.Event()

    def hook(path: Path) -> None:
        waiting.set()

    def read() -> None:
        first["result"] = world.launch(flow)

    def force() -> None:
        second["result"] = world.launch(flow, world.runner(rebuild_all=True))

    reader = threading.Thread(target=read)
    reader.start()
    with open(started, "rb"):  # the tool of the first launch holds the storage
        pass
    (storage,) = world.storages()
    assert _probe(storage, "exclusive") == "blocked" and _probe(storage, "shared") == "free"
    monkeypatch.setattr(run_lock, "wait_hook", hook)
    forcing = threading.Thread(target=force)
    forcing.start()
    try:
        assert waiting.wait(HANG_GUARD), "the forced launch did not wait for the reader"
        assert (storage / "gen" / "top.v").read_text() == "// run 1\n"
        assert world.runs == 1
    finally:
        with open(gate, "wb") as opened:
            opened.write(b"go")
        reader.join(HANG_GUARD)
    # the forced launch's own reader waits for the gate too
    started_again = threading.Thread(target=lambda: open(started, "rb").close())
    started_again.start()
    started_again.join(HANG_GUARD)
    with open(gate, "wb") as opened:
        opened.write(b"go")
    forcing.join(HANG_GUARD)
    assert first["result"].nodes[flow.name].record.metrics["seen"] == "// run 1\n|// run 1\n"
    assert second["result"].outcome is Outcome.PASSED, second["result"].error
    assert (storage / "gen" / "top.v").read_text() == "// run 2\n"
    assert world.runs == 2
    assert _probe(storage, "exclusive") == "free", "the lease ends with the launch"


def test_a_scrub_waits_for_a_reader_of_the_storage(tmp_path, monkeypatch):
    from xeda.flow_runner.scrub import scrub_generations

    started, gate = _fifos(tmp_path)
    flow = blocking_reader_flow(started, gate)
    world = World(tmp_path)
    launched: dict = {}
    scrubbed: dict = {}
    waiting = threading.Event()

    def launch() -> None:
        launched["result"] = world.launch(flow)

    def scrub() -> None:
        scrubbed["result"] = scrub_generations(
            "generated", run_root=world.run_root.resolve(), confirm=lambda candidates: True
        )

    reader = threading.Thread(target=launch)
    reader.start()
    with open(started, "rb"):
        pass
    (storage,) = world.storages()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    scrubbing = threading.Thread(target=scrub)
    scrubbing.start()
    try:
        assert waiting.wait(HANG_GUARD), "the scrub did not wait for the reader"
        assert (storage / "gen" / "top.v").is_file(), "the storage went while a tool read it"
    finally:
        with open(gate, "wb") as opened:
            opened.write(b"go")
        reader.join(HANG_GUARD)
        scrubbing.join(HANG_GUARD)
    assert launched["result"].outcome is Outcome.PASSED
    assert [p.resolve() for p in scrubbed["result"].removed] == [storage.resolve()]
    assert world.storages() == []


def test_a_scrub_of_generations_asks_first_and_removes_only_that_design(tmp_path):
    from xeda.flow_runner.scrub import scrub_generations

    flow = reader_flow()
    world = World(tmp_path)
    assert world.launch(flow).outcome is Outcome.PASSED
    other = world.run_root / ".generated" / "another" / "0123456789abcdef"
    other.mkdir(parents=True)
    root = world.run_root.resolve()
    assert scrub_generations("generated", run_root=root).removed == []
    assert scrub_generations("generated", run_root=root, confirm=lambda c: False).removed == []
    assert len(world.storages()) == 2
    result = scrub_generations("generated", run_root=root, confirm=lambda c: True)
    assert len(result.removed) == 1 and result.gone == []
    assert world.storages() == [other]
    assert scrub_generations("generated", run_root=root, confirm=lambda c: True).removed == []


# ---------------------------------------------------------------- a converter reads generated sources


def test_a_generated_source_passes_through_a_converter_by_its_name(tmp_path):
    """The generator writes Bluespec and Verilog. The converter converts the first and passes the
    second through, and the record of the converter names the second one by its name in the output
    of the generator, with no path to a storage. A second launch reuses both nodes."""
    from xeda.outcomes import Disposition

    from .test_converted_designs import Events, consumer_flows

    converter, reader = consumer_flows(Events())
    world = World(
        tmp_path,
        sources=["gen/Top.bsv", "gen/ext.v"],
        extra=f"flows:\n  {reader.name}:\n    inputs:\n      design: {converter.name}\n",
    )
    script = (world.root / "gen.py").read_text()
    (world.root / "gen.py").write_text(
        script
        + "(out / 'gen' / 'Top.bsv').write_text('package Top; endpackage\\n')\n"
        + "(out / 'gen' / 'ext.v').write_text('module ext(); endmodule\\n')\n"
    )
    result = world.launch(reader)
    assert result.outcome is Outcome.PASSED, result.error
    made = result.nodes[converter.name].record.outputs["design"]
    entries = [thaw(entry) for entry in made.design["sources"]]
    passed = [entry for entry in entries if "source" in entry]
    assert [(e["file"], e["type"]) for e in passed] == [("gen/ext.v", "Verilog")]
    assert str(world.run_root) not in str(made.design)
    again = world.launch(reader)
    assert again.outcome is Outcome.PASSED, again.error
    assert {n.disposition for n in again.nodes.values()} == {Disposition.REUSED}
    assert world.runs == 1


# ---------------------------------------------------------------- the lease is of a current storage


def test_a_generator_that_changes_what_it_reads_on_every_run_fails_the_launch(tmp_path):
    """Nothing vouches for its output, so the launch refuses it and does not read a storage that
    the judgment under the lease did not accept."""
    flow = reader_flow()
    world = World(tmp_path)
    script = (world.root / "gen.py").read_text()
    (world.root / "gen.py").write_text(
        script
        + "with open(os.environ['DESIGN_ROOT'] + '/spec.txt', 'a') as spec:\n"
        + "    spec.write('more\\n')\n"
    )
    result = world.launch(flow)
    assert result.outcome is Outcome.FAILED
    assert result.error is not None and result.error.code == "generator_failed"
    assert "changes what it reads" in result.error.message
    assert [n.outcome for n in result.nodes.values()] == [Outcome.SKIPPED]


def test_the_lease_is_of_the_files_the_judgment_under_it_found(tmp_path, monkeypatch):
    """The files of the generation that just ran may be stale when the lease is taken: the
    storage is described by what the judgment under the lease returns."""
    from xeda.flow_runner import launch_generation
    from xeda.flow_runner.launch_generation import Generation, generated

    world = World(tmp_path)
    world.run_root.mkdir()
    real = launch_generation.generate

    def stale(*args, **kwargs):
        found = real(*args, **kwargs)
        return Generation(found.storage, found.key, {"gen/top.v": ()}, found.description)

    monkeypatch.setattr(launch_generation, "generate", stale)
    with generated(world.load(), run_root=world.run_root.resolve(), environ=dict(os.environ)) as g:
        assert g is not None and dict(g.by_owner[None].matches) == {"gen/top.v": ("gen/top.v",)}


def test_a_scrub_refuses_a_storage_that_became_a_link_after_the_confirmation(tmp_path):
    from xeda.flow_runner.scrub import scrub_generations
    from xeda.run_dir import RunDirectoryError

    world = World(tmp_path)
    assert world.launch(reader_flow()).outcome is Outcome.PASSED
    (storage,) = world.storages()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep\n")

    def swap(candidates) -> bool:
        moved = storage.rename(tmp_path / "moved")
        storage.symlink_to(outside, target_is_directory=True)
        assert moved.is_dir()
        return True

    with pytest.raises(RunDirectoryError, match="link"):
        scrub_generations("generated", run_root=world.run_root.resolve(), confirm=swap)
    assert (outside / "precious.txt").read_text() == "keep\n"
    assert storage.is_symlink() and (tmp_path / "moved" / "gen" / "top.v").is_file()


def test_a_scrub_does_not_delete_through_a_link_to_another_storage(tmp_path):
    from xeda.flow_runner.scrub import scrub_generations
    from xeda.run_dir import RunDirectoryError

    world = World(tmp_path)
    assert world.launch(reader_flow()).outcome is Outcome.PASSED
    (storage,) = world.storages()
    other = world.run_root / ".generated" / "another" / "0123456789abcdef"
    other.mkdir(parents=True)
    (other / "keep.v").write_text("// keep\n")

    def swap(candidates) -> bool:
        storage.rename(tmp_path / "moved")
        storage.symlink_to(other, target_is_directory=True)
        return True

    with pytest.raises(RunDirectoryError, match="link"):
        scrub_generations("generated", run_root=world.run_root.resolve(), confirm=swap)
    assert (other / "keep.v").is_file()


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("delivery broke")])
@pytest.mark.parametrize("passed", [True, False])
def test_the_lease_ends_when_the_end_of_a_launch_is_interrupted(
    tmp_path, monkeypatch, failure, passed
):
    """An interrupt or an error while the launch delivers or cleans up must not keep the lease
    of the storage, whether the launch passed or failed."""
    from xeda.flow_runner import DefaultRunner

    flow = reader_flow()
    world = World(tmp_path)
    if not passed:
        monkeypatch.setattr(flow, "run", lambda self: (_ for _ in ()).throw(ValueError("no")))
    monkeypatch.setattr(
        DefaultRunner, "_finish_launch", lambda self, deliver: (_ for _ in ()).throw(failure)
    )
    runner = world.runner()
    with pytest.raises(type(failure)):
        runner.run_flow(flow, world.load(), {})
    (storage,) = world.storages()
    assert _probe(storage, "exclusive") == "free"
    assert runner._generation is None


def test_a_plan_sees_the_files_of_a_generated_input_as_the_launch_does(tmp_path):
    """The launch hands the files that a declaration matched to the declared input. The plan
    maps the names in the same way, so a node that the launch reuses is `fresh` in the plan."""
    flow = input_reader_flow()
    world = World(tmp_path)
    assert world.launch(flow).outcome is Outcome.PASSED
    state = world.runner().plan_with_freshness(flow, world.load())[1][flow.name]
    assert (state.state, state.reason) == ("fresh", "")
    again = world.launch(flow)
    assert again.nodes[flow.name].disposition.value == "reused"
    assert world.runs == 1


def test_a_generated_source_is_named_and_hashed_whatever_the_run_root_is(tmp_path):
    from xeda.flow_runner.launch_generation import Generations, execution_design, generate

    seen = []
    for run_root in (tmp_path / "one" / "xeda_run", tmp_path / "elsewhere" / "deeper" / "run"):
        world = World(tmp_path)
        run_root.mkdir(parents=True)
        design = world.load()
        generation = generate(design, run_root=run_root.resolve(), environ=dict(os.environ))
        executable = execution_design(design, Generations.single(generation))
        (source,) = executable.rtl.sources
        seen.append(
            (
                executable.source_artifact_name(source, ".v"),
                executable.parts_hash({"rtl"}),
                executable.source_path_as_named(source),
            )
        )
    assert seen[0] == seen[1]
    assert seen[0][2] == Path("gen/top.v") and str(tmp_path) not in seen[0][0]


def test_a_generator_sees_pwd_as_its_working_directory(tmp_path):
    world = World(tmp_path)
    assert world.launch(reader_flow()).outcome is Outcome.PASSED
    cwd, _, _, pwd = world.noted
    assert pwd == cwd


def test_an_old_record_makes_the_generator_run_again_in_the_same_storage(tmp_path):
    """A storage that an earlier release made (another record format) is generated once more,
    in place, so output made under other conditions (another `PWD`, another working
    directory) is not trusted now."""
    flow = reader_flow()
    world = World(tmp_path)
    assert world.launch(flow).outcome is Outcome.PASSED
    (storage,) = world.storages()
    record = storage / RECORD_NAME
    data = yaml.safe_load(record.read_text())
    assert data["format"] == RECORD_FORMAT
    data["format"] = RECORD_FORMAT - 1
    record.write_text(yaml.safe_dump(data))
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 2
    assert world.storages() == [storage], "the same storage"
    assert yaml.safe_load(record.read_text())["format"] == RECORD_FORMAT
    assert world.launch(flow).outcome is Outcome.PASSED
    assert world.runs == 2, "the new record is trusted"


def test_a_recipe_env_may_not_set_pwd(tmp_path):
    from xeda.design import DesignValidationError

    world = World(tmp_path, generator={"env": {"PWD": "/elsewhere"}})
    with pytest.raises(DesignValidationError, match="PWD"):
        world.load()
