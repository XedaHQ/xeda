"""Every advertised flow must be resolvable by the name the CLI advertises.

`xeda list-flows` used to list flows (`yosys_sim`, the since-removed `open_xc7`) that `xeda run` and
`xeda list-settings` could not resolve, because the registry was keyed by class name and the
snake_case lookup fell back to a lossy `snakecase_to_camelcase` round-trip.
"""

import ast
import sys
from importlib import import_module
from inspect import isabstract, isclass
from pathlib import Path

import pytest

import xeda
from xeda import flows
from xeda.flow import Flow
from xeda.flow_runner import FlowNotFoundError, get_flow_class

from .flow_registry import preserved_flow_registry

# The product's flows. The registry also holds the flows other test modules define: they are
# registered when their module is imported and removed by their own tests, so the registry at
# collection time says nothing about what is there when these tests run.
ALL_FLOW_CLASSES = sorted(set(flows.__builtin_flows__), key=lambda cls: cls.name)


def test_flows_all_lists_only_concrete_flow_classes():
    for name in flows.__all__:
        if name.startswith("__"):
            continue
        cls = getattr(flows, name)
        assert isclass(cls), f"xeda.flows.__all__ exports non-class {name!r}"
        assert issubclass(cls, Flow), f"xeda.flows.__all__ exports non-Flow {name!r}"
        assert not isabstract(cls), f"xeda.flows.__all__ exports abstract flow {name!r}"


def test_every_builtin_flow_is_exported():
    exported = {getattr(flows, n) for n in flows.__all__ if not n.startswith("__")}
    missing = sorted(cls.__name__ for cls in flows.__builtin_flows__ if cls not in exported)
    assert not missing, f"builtin flows missing from xeda.flows.__all__: {missing}"


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_resolvable_by_canonical_name(flow_class):
    assert get_flow_class(flow_class.name) is flow_class


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_is_not_resolvable_by_class_name(flow_class):
    with pytest.raises(FlowNotFoundError) as refused:
        get_flow_class(flow_class.__name__)
    assert refused.value.suggestions[0] == flow_class.name


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_settings_schema_is_generatable(flow_class):
    """`xeda list-settings <flow>` and the JSON output both depend on this."""
    schema = flow_class.Settings.model_json_schema(by_alias=True)
    assert "properties" in schema


def test_flow_name_lookup_is_exact():
    for spelling in ("vivado-synth", "VIVADO_SYNTH"):
        with pytest.raises(FlowNotFoundError):
            get_flow_class(spelling)


# --- The list of built-in flows ----------------------------------------------------------------

PACKAGE = Path(xeda.__file__).parent


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _base_name(base: ast.expr) -> str:
    """The last name of a base class expression: `Flow` for both `Flow` and `flow.Flow`."""
    if isinstance(base, ast.Name):
        return base.id
    return base.attr if isinstance(base, ast.Attribute) else ""


def _flow_classes_by_source() -> dict[str, set[str]]:
    """`{module name: names of the classes it defines at its top level that derive from Flow}`.

    This reads the source with `ast` and follows the names of the base classes, so it does not
    depend on how the package finds its modules. A class counts when one of its bases is `Flow` or
    a class of this scan that counts. A base that the source builds some other way (an alias set
    under `TYPE_CHECKING`) does not count, as in Python.
    """
    classes: dict[str, tuple[str, list[str]]] = {}
    for path in sorted((PACKAGE / "flow").rglob("*.py")) + sorted(
        (PACKAGE / "flows").rglob("*.py")
    ):
        module = _module_name(path)
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef):
                bases = [_base_name(base) for base in node.bases]
                classes[node.name] = (module, bases)
    flow_names = {"Flow"}
    while True:
        more = {
            name
            for name, (_module, bases) in classes.items()
            if name not in flow_names and flow_names.intersection(bases)
        }
        if not more:
            break
        flow_names |= more
    by_module: dict[str, set[str]] = {}
    for name in flow_names:
        if name in classes:
            by_module.setdefault(classes[name][0], set()).add(name)
    return by_module


def test_builtin_flows_are_every_concrete_flow_the_package_defines():
    expected = set()
    for module_name, names in _flow_classes_by_source().items():
        if not module_name.startswith("xeda.flows."):
            continue
        module = import_module(module_name)
        for name in names:
            cls = getattr(module, name)
            assert cls.__module__ == module_name
            if not isabstract(cls):
                expected.add(cls)
    assert expected, "the source scan found no flow"
    listed = set(flows.__builtin_flows__)
    assert listed == expected, (
        f"missing from __builtin_flows__: {sorted(c.__qualname__ for c in expected - listed)}; "
        f"not defined by the package: {sorted(c.__qualname__ for c in listed - expected)}"
    )


def test_builtin_flows_have_no_duplicate_and_are_sorted_by_name():
    builtin = flows.__builtin_flows__
    assert isinstance(builtin, list)
    assert len(builtin) == len(set(builtin)), "a class is listed twice"
    names = [cls.name for cls in builtin]
    assert len(names) == len(set(names)), "two classes have one flow name"
    assert names == sorted(names)


def _discover_in(monkeypatch, tmp_path: Path, files: dict[str, str]):
    """Run the discovery over a package of `files` that has the name `xeda.flows.<tmp name>`."""
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(flows, "__path__", [str(tmp_path)])
    before = set(sys.modules)
    try:
        return flows._discover_builtin_flows()
    finally:
        for name in set(sys.modules) - before:
            sys.modules.pop(name, None)
            parent, _, child = name.rpartition(".")
            if parent == flows.__name__ and hasattr(flows, child):
                delattr(flows, child)


def test_discovery_fails_on_a_subpackage_that_cannot_be_imported(monkeypatch, tmp_path):
    with preserved_flow_registry(), pytest.raises(ImportError, match="no_such_module_for_test"):
        _discover_in(
            monkeypatch,
            tmp_path,
            {"broken/__init__.py": "import no_such_module_for_test_xyz\n"},
        )


def test_discovery_fails_on_two_classes_with_one_flow_name(monkeypatch, tmp_path):
    flow = "from xeda.flow import Flow\n\n\nclass TwinFlow(Flow):\n    def run(self) -> None:\n        pass\n"
    with preserved_flow_registry(), pytest.raises(ImportError, match="two built-in flows"):
        _discover_in(monkeypatch, tmp_path, {"one.py": flow, "two.py": flow})


def test_discovery_lists_a_class_at_its_defining_module_only(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "flow_from_outside_for_test.py").write_text(
        "from xeda.flow import Flow\n\n\n"
        "class OutsideFlow(Flow):\n    def run(self) -> None:\n        pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(outside))
    files = {
        "package/one.py": (
            "from xeda.flow import Flow\nfrom flow_from_outside_for_test import OutsideFlow\n\n\n"
            "class LoneFlow(Flow):\n    def run(self) -> None:\n        pass\n"
        ),
        "package/again.py": "from .one import LoneFlow\n",
        "package/__init__.py": "",
    }
    with preserved_flow_registry():
        found = _discover_in(monkeypatch, tmp_path / "root", files)
    assert [cls.__qualname__ for cls in found] == ["LoneFlow"]
