"""Dependency cycles fail during planning; shared dependencies remain valid."""

import pytest

from xeda import Design
from xeda.errors import DesignError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner import dependencies
from xeda.remote.packaging import refuse_unsendable_declared

from .flow_registry import preserved_flow_registry
from .generation_world import reader_flow


def _designs(tmp_path, graph):
    for name, children in graph.items():
        (tmp_path / f"{name}.v").write_text(f"module {name}; endmodule\n")
        (tmp_path / f"{name}.yaml").write_text(
            f"name: {name}\ndependencies: {[f'{child}.yaml' for child in children]!r}\n"
            f"rtl:\n  sources: [{name}.v]\n  top: {name}\n"
        )
    return Design.from_file(tmp_path / "a.yaml")


def _check(design, tmp_path, entry):
    if entry == "remote":
        refuse_unsendable_declared(design)
    else:
        with preserved_flow_registry():
            DefaultRunner(tmp_path / "runs", display_results=False).plan_with_freshness(
                reader_flow(), design
            )


@pytest.mark.parametrize("entry", ["launch", "remote"])
@pytest.mark.parametrize(
    "graph, chain",
    [({"a": ["b"], "b": ["a"]}, "a -> b -> a"), ({"a": ["a"]}, "a -> a")],
)
def test_a_dependency_cycle_is_a_design_error(tmp_path, graph, chain, entry):
    design = _designs(tmp_path, graph)
    with pytest.raises(DesignError, match=f"dependency cycle: {chain}") as error:
        _check(design, tmp_path, entry)
    assert error.value.field == "dependencies[0]"
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("entry", ["launch", "remote"])
def test_a_diamond_dependency_still_plans(tmp_path, entry):
    design = _designs(tmp_path, {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []})
    _check(design, tmp_path, entry)


def test_a_local_cycle_is_refused_before_an_earlier_git_dependency_is_fetched(
    tmp_path, monkeypatch
):
    design = _designs(tmp_path, {"a": ["b"], "b": ["a"]})
    path = tmp_path / "a.yaml"
    path.write_text(
        path.read_text().replace(
            "dependencies: ['b.yaml']",
            "dependencies:\n  - uri: git+https://example.invalid/library.git#lib.yaml\n"
            "    commit: " + "a" * 40 + "\n  - b.yaml",
        )
    )
    design = Design.from_file(path)

    def fetched(*args, **kwargs):
        pytest.fail("a local cycle must be refused before a Git fetch")

    monkeypatch.setattr(dependencies, "fetch", fetched)
    with pytest.raises(DesignError, match="dependency cycle: a -> b -> a"):
        _check(design, tmp_path, "launch")


@pytest.mark.parametrize("entry", ["launch", "remote"])
def test_a_target_that_adds_a_dependency_on_a_design_that_depends_on_its_file_is_no_cycle(
    tmp_path, entry
):
    """`a` with its target depends on `b`, and `b` depends on `a` loaded as a dependency, which
    takes no target and so does not depend on `b`. The file of `a` is on the path twice, but the
    two designs differ."""
    _designs(tmp_path, {"a": [], "b": ["a"]})
    path = tmp_path / "a.yaml"
    path.write_text(path.read_text() + "targets:\n  test:\n    dependencies: [b.yaml]\n")
    _check(Design.from_file(path, target="test"), tmp_path, entry)
