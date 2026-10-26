"""A dependency with a generator of its own is generated too.

The launch runs the generator of every dependency that has one, into a storage of its own keyed by
the same rule as the design's, before the generator of the design that depends on it. The sources
that such a generator writes keep it as their owner when the design merges them, and name files in
its storage in the execution copy.
"""

import os
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow_runner.launch_generation import generating_units, inspect_generation
from xeda.outcomes import Outcome

from .flow_registry import preserved_flow_registry
from .generation_world import reader_flow
from .test_generation_storage import _tree

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no storage locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


#: Writes `gen/<name>.v` for the module `<name>` into its storage, and notes the order in which
#: the generators of a launch ran.
SCRIPT = """\
import os, sys
from pathlib import Path

name, log = sys.argv[1], sys.argv[2]
out = Path(os.environ["XEDA_GENERATED"])
(out / "gen").mkdir(exist_ok=True)
(out / "gen" / f"{name}.v").write_text(f"module {name}; endmodule // {os.environ['DESIGN_ROOT'][-8:]}\\n")
with open(log, "a") as stream:
    stream.write(name + "\\n")
"""


class Chain:
    """`top` depends on `lib1`, which depends on `lib2`, ...: each library has a generator that
    writes `gen/<name>.v`, and `top` has a hand-written `top.v` (and a generator of its own when
    `own` says so)."""

    def __init__(self, tmp_path: Path, depth: int = 1, own: bool = False) -> None:
        self.tmp_path = tmp_path
        self.log = tmp_path / "order.log"
        self.run_root = tmp_path / "xeda_run"
        self.names = [f"lib{n}" for n in range(1, depth + 1)]
        for index, name in enumerate(self.names):
            root = tmp_path / name
            root.mkdir()
            (root / "gen.py").write_text(SCRIPT)
            (root / "spec.txt").write_text("one\n")
            deeper = self.names[index + 1] if index + 1 < depth else None
            (root / f"{name}.yaml").write_text(self._library(name, deeper))
        top = tmp_path / "top"
        top.mkdir()
        (top / "top.v").write_text("module top; endmodule\n")
        (top / "gen.py").write_text(SCRIPT)
        (top / "spec.txt").write_text("one\n")
        self.design_file = top / "top.yaml"
        generator = self._generator("top", "    generated_sources: [gen/top.v]\n") if own else ""
        sources = "[top.v, gen/top.v]" if own else "[top.v]"
        self.design_file.write_text(
            f"name: top\ndependencies: [../{self.names[0]}/{self.names[0]}.yaml]\nrtl:\n"
            f"  sources: {sources}\n  top: top\n{generator}"
        )

    def _generator(self, name: str, extra: str = "") -> str:
        return (
            "  generator:\n"
            f"    executable: {sys.executable!r}\n"
            f"    args: ['$DESIGN_ROOT/gen.py', {name!r}, {str(self.log)!r}]\n"
            f"    sources: [gen.py, spec.txt]\n{extra}"
        )

    def _library(self, name: str, deeper: str | None) -> str:
        dependency = f"dependencies: [../{deeper}/{deeper}.yaml]\n" if deeper else ""
        return (
            f"name: {name}\n{dependency}rtl:\n  sources: [gen/{name}.v]\n  top: {name}\n"
            + self._generator(name)
        )

    def load(self) -> Design:
        return Design.from_file(self.design_file)

    def runner(self, **settings):
        from xeda.flow_runner import DefaultRunner

        return DefaultRunner(self.run_root, display_results=False, **settings)

    def launch(self, flow, **settings):
        from xeda.flow_runner.launch_result import launch_result
        from xeda.spec import Spec

        runner = self.runner(**settings)
        design = self.load()
        return launch_result(
            runner, Spec(flow.name, design), lambda: runner.run_flow(flow, design, {})
        )

    @property
    def ran(self) -> list[str]:
        return self.log.read_text().split() if self.log.exists() else []

    def storages(self, name: str) -> list[Path]:
        base = self.run_root / ".generated" / name
        return sorted(p for p in base.glob("*") if p.is_dir()) if base.is_dir() else []


def test_the_design_lists_the_generated_sources_of_its_dependency_with_their_owner(tmp_path):
    chain = Chain(tmp_path)
    design = chain.load()
    sources = design.rtl.sources
    assert [s.generated for s in sources] == [True, False]
    generated = [s for s in sources if s.generated]
    assert generated[0].owner == (tmp_path / "lib1").resolve().as_posix()
    assert [(owner, unit.name) for owner, unit in generating_units(design)] == [
        ((tmp_path / "lib1").resolve().as_posix(), "lib1")
    ]


def test_a_dependency_that_generates_is_generated_and_read(tmp_path):
    chain = Chain(tmp_path)
    flow = reader_flow()
    before = _tree(tmp_path / "lib1")
    result = chain.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    seen = result.nodes[flow.name].record.metrics["seen"]
    assert "module lib1;" in seen and "module top;" in seen
    (storage,) = chain.storages("lib1")
    assert (storage / "gen" / "lib1.v").is_file()
    assert chain.storages("top") == [], "the design itself has no generator"
    assert _tree(tmp_path / "lib1") == before, "the tree of the dependency is not written"
    assert chain.ran == ["lib1"]


def test_a_second_launch_reuses_every_storage(tmp_path):
    chain = Chain(tmp_path, depth=2, own=True)
    flow = reader_flow()
    assert chain.launch(flow).outcome is Outcome.PASSED
    assert chain.ran == ["lib2", "lib1", "top"], "a dependency generates before its dependent"
    assert chain.launch(flow).outcome is Outcome.PASSED
    assert chain.ran == ["lib2", "lib1", "top"]


def test_a_chain_of_dependencies_generates_each_in_its_own_storage(tmp_path):
    chain = Chain(tmp_path, depth=2)
    flow = reader_flow()
    result = chain.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    seen = result.nodes[flow.name].record.metrics["seen"]
    assert all(f"module {name};" in seen for name in ("lib1", "lib2", "top"))
    assert chain.ran == ["lib2", "lib1"]
    assert len(chain.storages("lib1")) == len(chain.storages("lib2")) == 1


def test_a_change_of_the_recipe_of_a_dependency_regenerates_only_its_storage(tmp_path):
    chain = Chain(tmp_path, depth=2, own=True)
    flow = reader_flow()
    assert chain.launch(flow).outcome is Outcome.PASSED
    first = {name: chain.storages(name) for name in ("lib1", "lib2", "top")}
    path = tmp_path / "lib1" / "lib1.yaml"
    path.write_text(
        path.read_text().replace(
            "sources: [gen.py, spec.txt]",
            "sources: [gen.py, spec.txt]\n    always_runs: false\n    check: false",
        )
    )
    assert chain.launch(flow).outcome is Outcome.PASSED
    assert chain.ran == ["lib2", "lib1", "top", "lib1"]
    assert chain.storages("lib2") == first["lib2"] and chain.storages("top") == first["top"]
    assert len(chain.storages("lib1")) == 2


def test_the_inherited_environment_is_evidence_for_every_generator(tmp_path, monkeypatch):
    chain = Chain(tmp_path, depth=1, own=True)
    flow = reader_flow()
    monkeypatch.setenv("LICENSE_HINT", "a")
    assert chain.launch(flow).outcome is Outcome.PASSED
    monkeypatch.setenv("LICENSE_HINT", "b")
    assert chain.launch(flow).outcome is Outcome.PASSED
    assert chain.ran == ["lib1", "top", "lib1", "top"]
    assert len(chain.storages("lib1")) == 1


def test_a_declaration_of_a_dependency_that_matches_nothing_fails_the_reader(tmp_path):
    chain = Chain(tmp_path)
    script = tmp_path / "lib1" / "gen.py"
    script.write_text(script.read_text().replace('f"{name}.v"', '"other.v"'))
    flow = reader_flow()
    node = chain.launch(flow).nodes[flow.name]
    assert node.error is not None and node.error.code == "missing_generated"
    assert "gen/lib1.v" in node.error.message and "lib1" in node.error.message


def test_a_plan_is_unknown_until_every_storage_is_current(tmp_path):
    chain = Chain(tmp_path, depth=2, own=True)
    flow = reader_flow()

    def state():
        return chain.runner().plan_with_freshness(flow, chain.load())[1][flow.name]

    assert (state().state, state().reason) == ("unknown", "the generator has not run")
    assert chain.launch(flow).outcome is Outcome.PASSED
    assert state().state == "fresh"
    (tmp_path / "lib2" / "spec.txt").write_text("two\n")
    assert state().state == "unknown", "the storage of the deepest dependency is stale"
    assert len(chain.ran) == 3


def test_two_generators_may_not_declare_one_name(tmp_path):
    chain = Chain(tmp_path)
    (tmp_path / "top" / "top.yaml").write_text(
        (tmp_path / "top" / "top.yaml")
        .read_text()
        .replace("sources: [top.v]", "sources: [top.v, gen/lib1.v]")
        + chain._generator("top")
    )
    with pytest.raises(DesignValidationError, match="two generators declare"):
        chain.load()


def test_inspecting_a_design_without_a_generator_anywhere_finds_none(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "a.v").write_text("module a; endmodule\n")
    design = Design(name="d", design_root=root, rtl={"sources": ["a.v"], "top": "a"})
    assert generating_units(design) == []
    assert inspect_generation(design, None).generator is False
    assert os.path.isdir(root)


def test_launches_that_merge_the_same_dependencies_in_other_orders_do_not_wait_for_each_other(
    tmp_path,
):
    """A generation holds one exclusive lock and waits for no other lock; the leases are shared
    and taken after all the generations. So two launches that rebuild the same storages, with the
    dependencies listed in opposite orders, both finish."""
    import threading

    from xeda.flow_runner.launch_result import launch_result
    from xeda.spec import Spec
    from tests.tool_utils import HANG_GUARD

    chain = Chain(tmp_path, depth=1)
    other = tmp_path / "lib2"
    other.mkdir()
    (other / "gen.py").write_text(SCRIPT)
    (other / "spec.txt").write_text("one\n")
    (other / "lib2.yaml").write_text(chain._library("lib2", None))
    designs = []
    for name, order in (("x", ["lib1", "lib2"]), ("y", ["lib2", "lib1"])):
        root = tmp_path / name
        root.mkdir()
        (root / "t.v").write_text(f"module {name}; endmodule\n")
        listed = ", ".join(f"../{lib}/{lib}.yaml" for lib in order)
        (root / f"{name}.yaml").write_text(
            f"name: {name}\ndependencies: [{listed}]\nrtl:\n  sources: [t.v]\n  top: {name}\n"
        )
        designs.append(Design.from_file(root / f"{name}.yaml"))
    flow = reader_flow()
    results: list[Outcome] = []

    def launch(design: Design) -> None:
        for _ in range(3):
            runner = chain.runner(rebuild_all=True)
            result = launch_result(
                runner, Spec(flow.name, design), lambda: runner.run_flow(flow, design, {})
            )
            results.append(result.outcome)

    threads = [threading.Thread(target=launch, args=(design,)) for design in designs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(HANG_GUARD)
    assert not any(thread.is_alive() for thread in threads), "the launches wait for each other"
    assert results == [Outcome.PASSED] * 6
