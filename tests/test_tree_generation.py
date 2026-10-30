"""Generated sources that a generator writes into its design tree, through whole launches.

A tree entry of `rtl.sources` is a path below the design root that the generator writes. A launch
copies each tree file that the generation wrote into `.xeda-tree/` of the storage, and the flows
read that copy. The designs of one design root take the lock of that tree while they generate,
so their generations run one at a time.
"""

import os
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.design import TREE_DIRECTORY
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner, launch_generation, run_lock
from xeda.flow_runner.launch_generation import (
    Generation,
    Generations,
    execution_design,
    generate,
)
from xeda.flow_runner.launch_result import launch_result
from xeda.generation import RECORD_NAME, outputs_in
from xeda.outcomes import Disposition, Outcome, Result
from xeda.spec import Spec

from .flow_registry import preserved_flow_registry
from .generation_world import input_reader_flow, reader_flow
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="tree entries need POSIX")

#: Appends a line to its counter (the first argument). When the second argument is two named
#: pipes `started:gate`, it tells the test that it runs, and waits until the test opens the gate.
#: Then it writes each further argument: a path below the design root, or `@<name>` for a name in
#: its storage. The text of a file is its name and the content of `spec.txt`.
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
storage = Path(os.environ["XEDA_GENERATED"])
counter, hold, *names = sys.argv[1:]
with open(counter, "a") as stream:
    stream.write("ran\\n")
if hold != "-":
    started, gate = hold.split(":")
    open(started, "wb").close()
    open(gate, "rb").read()
spec = (root / "spec.txt").read_text()
for name in names:
    path = storage / name[1:] if name.startswith("@") else root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// " + name + " " + spec)
"""


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


class Tree:
    """A design root `tmp_path / directory` that holds designs with generators, and a run root
    beside it."""

    def __init__(self, tmp_path: Path, directory: str = "root") -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / directory
        self.root.mkdir(parents=True)
        (self.root / "gen.py").write_text(GENERATOR)
        (self.root / "spec.txt").write_text("one\n")
        self.run_root = tmp_path / "xeda_run"

    def design(
        self,
        name: str,
        sources: list[str],
        writes: list[str],
        *,
        hold: str = "-",
        dependencies: list[str] | None = None,
        **generator,
    ) -> Path:
        """Write the design file of `name`, whose generator writes `writes`, and return it."""
        recipe = {
            "executable": sys.executable,
            "args": ["$DESIGN_ROOT/gen.py", str(self.counter(name)), hold, *writes],
            "sources": ["spec.txt", "gen.py"],
            **generator,
        }
        document = {
            **({"dependencies": dependencies} if dependencies else {}),
            "name": name,
            "rtl": {"sources": sources, "top": "top", "generator": recipe},
        }
        file = self.root / f"{name}.yaml"
        file.write_text(yaml.safe_dump(document, sort_keys=False))
        return file

    def counter(self, name: str) -> Path:
        return self.tmp_path / f"{name}-runs.log"

    def runs(self, name: str) -> int:
        """How many times the generator of `name` has run."""
        counter = self.counter(name)
        return len(counter.read_text().splitlines()) if counter.exists() else 0

    def storages(self, name: str) -> list[Path]:
        base = self.run_root / ".generated" / name
        return sorted(p for p in base.glob("*") if p.is_dir()) if base.is_dir() else []

    def launch(self, file: Path, flow: type[Flow], **settings) -> Result:
        runner = DefaultRunner(self.run_root, display_results=False, **settings)
        design = Design.load(file)
        return launch_result(
            runner, Spec(flow.name, design), lambda: runner.run_flow(flow, design, {})
        )


def _seen(result: Result, flow: type[Flow]) -> str:
    assert result.outcome is Outcome.PASSED, result.error
    return result.nodes[flow.name].record.metrics["seen"]


def _error(result: Result, flow: type[Flow]):
    node = result.nodes[flow.name]
    assert node.error is not None, node
    return node.error


# ---------------------------------------------------------------------- the copy in the storage


def test_a_tree_file_that_the_generation_wrote_is_read_from_its_copy_in_the_storage(tmp_path):
    tree = Tree(tmp_path)
    file = tree.design("mxu", ["gen_rtl/MXU/mxu.v"], ["gen_rtl/MXU/mxu.v"])
    flow = reader_flow()
    assert _seen(tree.launch(file, flow), flow) == "// gen_rtl/MXU/mxu.v one\n"
    (storage,) = tree.storages("mxu")
    copy = storage / TREE_DIRECTORY / "gen_rtl" / "MXU" / "mxu.v"
    assert copy.read_text() == "// gen_rtl/MXU/mxu.v one\n"
    record = yaml.safe_load((storage / RECORD_NAME).read_text())
    assert [name for name, _ in record["outputs"]] == [".xeda-tree/gen_rtl/MXU/mxu.v"]

    again = tree.launch(file, flow)
    assert again.nodes[flow.name].disposition is Disposition.REUSED
    assert tree.runs("mxu") == 1

    # The flows read the copy: an edit of the tree file changes nothing.
    (tree.root / "gen_rtl" / "MXU" / "mxu.v").write_text("// edited\n")
    third = tree.launch(file, flow)
    assert third.nodes[flow.name].disposition is Disposition.REUSED
    other = input_reader_flow()
    ran = tree.launch(file, other)
    assert ran.nodes[other.name].disposition is Disposition.RAN
    assert _seen(ran, other) == "// gen_rtl/MXU/mxu.v one\n"
    assert tree.runs("mxu") == 1
    design = Design.load(file)
    generation = Generation(storage, storage.name, outputs_in(storage, ["gen_rtl/MXU/mxu.v"]))
    (source,) = execution_design(design, Generations.single(generation)).rtl.sources
    assert source.output_name == "gen_rtl/MXU/mxu.v"
    assert source.file == copy


@pytest.mark.parametrize("entry", ["gen_rtl/old.v", "gen_rtl/*.v"])
def test_an_old_tree_file_that_the_generation_did_not_write_is_not_read(tmp_path, entry):
    tree = Tree(tmp_path)
    old = tree.root / "gen_rtl" / "old.v"
    old.parent.mkdir()
    old.write_text("// left by an earlier run\n")
    flow = reader_flow()
    error = _error(tree.launch(tree.design("stale", [entry], []), flow), flow)
    assert error.code == "missing_generated"
    assert "wrote below the design root" in error.message
    assert f"`{old.resolve()}` exists, but the last generation did not write it" in error.message
    assert "must be made to write it" in error.message


def test_designs_that_share_a_pattern_each_take_only_the_file_that_their_generation_wrote(
    tmp_path,
):
    tree = Tree(tmp_path)
    first = tree.design("first", ["gen_rtl/*.v"], ["gen_rtl/first.v"])
    second = tree.design("second", ["gen_rtl/*.v"], ["gen_rtl/second.v"])
    flow = reader_flow()
    assert _seen(tree.launch(first, flow), flow) == "// gen_rtl/first.v one\n"
    assert _seen(tree.launch(second, flow), flow) == "// gen_rtl/second.v one\n"
    assert _seen(tree.launch(first, flow), flow) == "// gen_rtl/first.v one\n"
    assert tree.runs("first") == tree.runs("second") == 1


def test_a_generator_that_writes_the_name_xeda_keeps_in_its_storage_fails(tmp_path):
    tree = Tree(tmp_path)
    file = tree.design("reserved", ["gen_rtl/top.v"], ["gen_rtl/top.v", "@.xeda-tree/top.v"])
    result = tree.launch(file, reader_flow())
    assert result.outcome is Outcome.FAILED
    assert result.error is not None and result.error.code == "generator_failed"
    assert "`.xeda-tree` is a name Xeda keeps for itself" in result.error.message
    assert tree.storages("reserved") == [], "a failed generation leaves no storage"


def test_a_link_below_the_design_root_on_the_way_to_a_tree_file_fails_the_generation(tmp_path):
    tree = Tree(tmp_path)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    (tree.root / "gen_rtl").symlink_to(elsewhere, target_is_directory=True)
    file = tree.design("linked", ["gen_rtl/top.v"], ["gen_rtl/top.v"])
    result = tree.launch(file, reader_flow())
    assert result.error is not None and result.error.code == "generator_failed"
    assert "gen_rtl` is a symbolic link" in result.error.message
    assert tree.storages("linked") == []


# ------------------------------------------------------------------------------------ hints


def test_a_tree_entry_that_the_generator_wrote_into_its_storage_gets_a_hint(tmp_path):
    tree = Tree(tmp_path)
    flow = reader_flow()
    error = _error(tree.launch(tree.design("misplaced", ["gen/top.v"], ["@gen/top.v"]), flow), flow)
    assert error.code == "missing_generated"
    (storage,) = tree.storages("misplaced")
    assert f"The storage of the generator holds `{storage / 'gen' / 'top.v'}`" in error.message
    assert "write the entry as `$XEDA_GENERATED/gen/top.v`" in error.message


def test_a_storage_entry_that_the_generator_wrote_where_it_runs_gets_a_hint(tmp_path):
    """The generator runs in `build` and writes `gen/top.v` there: the tree entry is the path
    below the design root, `build/gen/top.v`, not the path below the directory it runs in."""
    tree = Tree(tmp_path)
    (tree.root / "build").mkdir()
    file = tree.design("placed", ["$XEDA_GENERATED/gen/top.v"], ["build/gen/top.v"], cwd="build")
    flow = reader_flow()
    error = _error(tree.launch(file, flow), flow)
    assert error.code == "missing_generated"
    written = tree.root.resolve() / "build" / "gen" / "top.v"
    assert f"A file of that name exists at `{written}`" in error.message
    assert "write the entry as `build/gen/top.v`" in error.message


# ------------------------------------------------------------------------------------ locks


def _fifos(tmp_path: Path) -> tuple[Path, Path]:
    started, gate = tmp_path / "started", tmp_path / "gate"
    os.mkfifo(started)
    os.mkfifo(gate)
    return started, gate


def _launch_held(tree: Tree, first: Path, second: Path, monkeypatch) -> tuple[list[Path], dict]:
    """Launch `first`, whose generator waits on the pipes of `_fifos`, and launch `second` while
    the first generator runs. Return the lock files that the second launch waited for, and the
    results by name (`first running`: the first launch still ran when the second one waited or
    ended)."""
    started, gate = tree.tmp_path / "started", tree.tmp_path / "gate"
    flow = reader_flow()
    results: dict = {}
    paths: list[Path] = []
    waiting = threading.Event()
    second_done = threading.Event()

    def launch(name: str, file: Path) -> None:
        try:
            results[name] = tree.launch(file, flow)
        finally:
            if name == "second":
                second_done.set()

    def hook(path: Path) -> None:
        paths.append(path)
        waiting.set()

    one = threading.Thread(target=launch, args=("first", first))
    one.start()
    with open(started, "rb"):  # opens when the first generator runs, under its locks
        pass
    monkeypatch.setattr(run_lock, "wait_hook", hook)
    two = threading.Thread(target=launch, args=("second", second))
    two.start()
    deadline = time.monotonic() + HANG_GUARD
    try:
        while not (waiting.is_set() or second_done.is_set()) and time.monotonic() < deadline:
            second_done.wait(0.05)
        results["first running"] = one.is_alive()
    finally:
        with open(gate, "wb") as opened:
            opened.write(b"go")
        one.join(HANG_GUARD)
        two.join(HANG_GUARD)
    assert not one.is_alive() and not two.is_alive(), "a launch did not finish"
    return paths, results


def test_the_designs_of_one_root_generate_one_at_a_time(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    started, gate = _fifos(tmp_path)
    first = tree.design("first", ["gen_rtl/first.v"], ["gen_rtl/first.v"], hold=f"{started}:{gate}")
    second = tree.design("second", ["gen_rtl/second.v"], ["gen_rtl/second.v"])
    paths, results = _launch_held(tree, first, second, monkeypatch)
    assert results["first running"]
    assert [path.parent.name for path in paths] == ["trees"], "it waited for the tree"
    assert paths[0].parent.parent == tree.run_root.resolve() / ".locks"
    assert results["first"].outcome is Outcome.PASSED, results["first"].error
    assert results["second"].outcome is Outcome.PASSED, results["second"].error
    assert tree.runs("first") == tree.runs("second") == 1


def test_the_designs_of_two_roots_generate_side_by_side(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    other = Tree(tmp_path, "other")
    started, gate = _fifos(tmp_path)
    first = tree.design("first", ["gen_rtl/first.v"], ["gen_rtl/first.v"], hold=f"{started}:{gate}")
    second = other.design("second", ["gen_rtl/second.v"], ["gen_rtl/second.v"])
    paths, results = _launch_held(tree, first, second, monkeypatch)
    assert paths == [], "the second launch waited for no lock"
    assert results["first running"], "the second launch ended while the first one generated"
    assert results["second"].outcome is Outcome.PASSED, results["second"].error
    assert results["first"].outcome is Outcome.PASSED, results["first"].error


def _requests(monkeypatch) -> list[list[tuple[str, bool]]]:
    """Record the storage and tree requests of each acquisition of the generation stage."""
    calls: list[list[tuple[str, bool]]] = []
    real = launch_generation.acquire_all

    def spy(requests, **kwargs):
        calls.append([(r.kind, r.exclusive) for r in requests if r.kind in ("storage", "tree")])
        return real(requests, **kwargs)

    monkeypatch.setattr(launch_generation, "acquire_all", spy)
    return calls


def test_a_unit_promoted_under_its_lock_requests_its_storage_and_its_tree_exclusive(
    tmp_path, monkeypatch
):
    """The hint before the acquisition says that the storage is valid; the judgment under the
    shared lock finds it is not. The acquisition starts again with the storage and the tree
    exclusive."""
    tree = Tree(tmp_path)
    design = Design.load(tree.design("mxu", ["gen_rtl/mxu.v"], ["gen_rtl/mxu.v"]))
    tree.run_root.mkdir()
    real = launch_generation._reason
    hinted: list[bool] = []

    def valid_first(*args, **kwargs):
        if not hinted:
            hinted.append(True)
            return None, {}
        return real(*args, **kwargs)

    monkeypatch.setattr(launch_generation, "_reason", valid_first)
    calls = _requests(monkeypatch)
    with launch_generation.generated(
        design, run_root=tree.run_root.resolve(), environ=dict(os.environ)
    ) as generations:
        assert generations is not None
    assert calls[:3] == [
        [("storage", False)],
        [("storage", True), ("tree", True)],
        [("storage", False)],
    ]
    assert tree.runs("mxu") == 1


def test_a_dependency_in_the_same_root_shares_one_tree_lock(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.design("lib", ["gen_rtl/lib.v"], ["gen_rtl/lib.v"])
    top = tree.design("top", ["gen_rtl/top.v"], ["gen_rtl/top.v"], dependencies=["lib.yaml"])
    taken: list[Path] = []
    monkeypatch.setattr(run_lock, "acquired_hook", taken.append)
    calls = _requests(monkeypatch)
    flow = reader_flow()
    seen = _seen(tree.launch(top, flow), flow)
    assert "// gen_rtl/lib.v one" in seen and "// gen_rtl/top.v one" in seen
    assert [("tree", True), ("tree", True)] == [r for r in calls[0] if r[0] == "tree"]
    assert len({path for path in taken if path.parent.name == "trees"}) == 1


def test_generate_takes_the_storage_and_the_tree_in_one_acquisition(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    design = Design.load(tree.design("mxu", ["gen_rtl/mxu.v"], ["gen_rtl/mxu.v"]))
    tree.run_root.mkdir()
    calls = _requests(monkeypatch)
    generation = generate(design, run_root=tree.run_root.resolve(), environ=dict(os.environ))
    assert generation is not None and tree.runs("mxu") == 1
    assert calls == [[("storage", True), ("tree", True)]]
