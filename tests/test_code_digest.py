"""The code digests of a launch: they read the files when the launch asks, name them relative to
the root of the code, and cover what a flow imports.

A plugin flow is a package or a module written under `tmp_path`, imported for the test and
removed from the process after it.
"""

import importlib
import shutil
import sys
import uuid
from pathlib import Path
from textwrap import dedent
from types import ModuleType

import pytest

from xeda import Design
from xeda.flow import Flow, FlowFatalError
from xeda.flow_runner import DefaultRunner, code_digest
from xeda.flow_runner.code_digest import CodeDigests, flow_code_digest, xeda_code_digest

from .flow_registry import preserved_flow_registry

pytestmark = pytest.mark.python_compat

FLOW_MODULE = """
    from pathlib import Path
    from typing import ClassVar

    from xeda.flow import Flow

    from . import helper


    class {cls}(Flow):
        '''Writes the text of its helper and of its template.'''

        results_description: ClassVar[dict[str, str]] = {{}}
        design_source_types = frozenset()

        def run(self) -> None:
            template = Path(__file__).parent / "templates" / "message.txt"
            (self.run_path / "out.txt").write_text(helper.TEXT + template.read_text())
"""

LOOSE_FLOW_MODULE = """
    from pathlib import Path
    from typing import ClassVar

    from xeda.flow import Flow

    import {helper}


    class {cls}(Flow):
        '''Writes the text of its helper and of its template.'''

        results_description: ClassVar[dict[str, str]] = {{}}
        design_source_types = frozenset()

        def run(self) -> None:
            template = Path(__file__).parent / "templates" / "message.txt"
            (self.run_path / "out.txt").write_text({helper}.TEXT + template.read_text())
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"))


def _forget_modules(prefix: str) -> None:
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        del sys.modules[name]


class Plugin:
    """A plugin flow package (or a module outside a package) and the way to import it."""

    def __init__(self, root: Path, package: str, flow_class: type[Flow]) -> None:
        self.root, self.package, self.flow_class = root, package, flow_class

    @property
    def directory(self) -> Path:
        return self.root / self.package


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """A flow in the module `flows` of a package, which also has a helper module and a template."""
    package = f"plugin_{uuid.uuid4().hex[:8]}"
    root = tmp_path / "code"
    _write(root / package / "__init__.py", "")
    _write(root / package / "helper.py", 'TEXT = "one\\n"\n')
    _write(root / package / "templates" / "message.txt", "message one\n")
    _write(root / package / "flows.py", FLOW_MODULE.format(cls="PackagedToy"))
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    with preserved_flow_registry():
        try:
            module = importlib.import_module(f"{package}.flows")
            yield Plugin(root, package, module.PackagedToy)
        finally:
            _forget_modules(package)


@pytest.fixture
def loose_plugin(tmp_path, monkeypatch):
    """A flow in a module that is in no package. It imports a helper module and a package."""
    root = tmp_path / "scripts"
    unique = uuid.uuid4().hex[:8]
    module, helper, library = f"loose_{unique}", f"helper_{unique}", f"library_{unique}"
    _write(root / f"{helper}.py", f'import {library}\nTEXT = "one" + {library}.SUFFIX + "\\n"\n')
    _write(root / library / "__init__.py", 'SUFFIX = ""\n')
    _write(root / library / "data.txt", "data\n")
    _write(root / "templates" / "message.txt", "message one\n")
    _write(root / "unrelated.py", "UNRELATED = 1\n")
    _write(root / f"{module}.py", LOOSE_FLOW_MODULE.format(cls="LooseToy", helper=helper))
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    with preserved_flow_registry():
        try:
            imported = importlib.import_module(module)
            plugin = Plugin(root, module, imported.LooseToy)
            plugin.helper, plugin.library = helper, library  # type: ignore[attr-defined]
            yield plugin
        finally:
            for name in (module, helper, library):
                _forget_modules(name)


@pytest.fixture
def design(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    return Design(name="toy", rtl={"sources": ["a.v"], "top": "a"}, design_root=tmp_path)


def _launch(tmp_path, plugin, design) -> Flow:
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.launch_flow(plugin.flow_class, design, {})
    assert flow.succeeded
    return flow


# The digests of Xeda's own code


def test_the_xeda_code_digest_covers_helpers_templates_and_data():
    """An editable install keeps its version string across edits: a helper flows use (template
    filters in utils.py, cocotb.py, tool.py) or bundled board data must count as xeda's code."""
    from xeda.digest import package_files

    files = {
        p.relative_to(code_digest.XEDA_PACKAGE).as_posix()
        for p in package_files(code_digest.XEDA_PACKAGE)
    }
    assert {"utils.py", "cocotb.py", "tool.py", "data/boards.toml"} <= files
    assert "flows/yosys/templates/yosys_synth.ys" in files
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)
    assert xeda_code_digest() == xeda_code_digest() and len(xeda_code_digest()) == 32


def test_a_flow_of_xeda_s_own_has_no_separate_code_digest():
    from xeda.flows import YosysFpga

    assert flow_code_digest(YosysFpga) == ""


def test_the_xeda_code_digest_reads_the_files_each_time_and_not_where_they_are(
    tmp_path, monkeypatch
):
    """A copy of the package at another place has the same digest, and an edit of a template in
    it changes the digest of the next call, in the same process."""
    copy = tmp_path / "elsewhere" / "xeda"
    shutil.copytree(
        code_digest.XEDA_PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    monkeypatch.setattr(code_digest, "XEDA_PACKAGE", copy)
    before = xeda_code_digest()
    moved = tmp_path / "moved" / "xeda"
    moved.parent.mkdir()
    copy.rename(moved)
    monkeypatch.setattr(code_digest, "XEDA_PACKAGE", moved)
    assert xeda_code_digest() == before
    template = moved / "flows" / "yosys" / "templates" / "yosys_synth.ys"
    template.write_text(template.read_text() + "# edited\n")
    assert xeda_code_digest() != before


# The digest of a plugin flow in a package


def test_an_edit_of_a_template_makes_the_next_launch_stale_in_the_same_process(
    tmp_path, plugin, design
):
    _launch(tmp_path, plugin, design)
    assert _launch(tmp_path, plugin, design).reused
    (plugin.directory / "templates" / "message.txt").write_text("message two\n")
    flow = _launch(tmp_path, plugin, design)
    assert not flow.reused
    assert flow.stale_reason == f"the {flow.name} flow's code changed"
    assert (flow.run_path / "out.txt").read_text() == "one\nmessage two\n"


def test_an_edit_of_a_helper_module_makes_the_next_launch_stale_in_the_same_process(
    tmp_path, plugin, design
):
    """The helper is a module of the package that the module of the flow imports."""
    _launch(tmp_path, plugin, design)
    _write(plugin.directory / "helper.py", 'TEXT = "two\\n"\n')
    flow = _launch(tmp_path, plugin, design)
    assert not flow.reused
    assert flow.stale_reason == f"the {flow.name} flow's code changed"


def _import_copy(monkeypatch, plugin, names, copy_root: Path) -> type[Flow]:
    """Import the same code from the copy of its tree in `copy_root`, in place of the original."""
    shutil.copytree(plugin.root, copy_root, ignore=shutil.ignore_patterns("__pycache__"))
    for name in names:
        _forget_modules(name)
    monkeypatch.syspath_prepend(str(copy_root))
    importlib.invalidate_caches()
    module = importlib.import_module(plugin.flow_class.__module__)
    flow_class = getattr(module, plugin.flow_class.__name__)
    assert Path(module.__file__).is_relative_to(copy_root)
    return flow_class


def test_a_moved_package_has_the_same_flow_code_digest(tmp_path, monkeypatch, plugin):
    digest = flow_code_digest(plugin.flow_class)
    assert len(digest) == 32
    moved = _import_copy(monkeypatch, plugin, [plugin.package], tmp_path / "elsewhere" / "code")
    assert moved is not plugin.flow_class
    assert flow_code_digest(moved) == digest


def test_the_flow_code_digest_covers_the_whole_package_and_names_files_relative_to_it(plugin):
    before = flow_code_digest(plugin.flow_class)
    (plugin.directory / "data" / "table.csv").parent.mkdir()
    (plugin.directory / "data" / "table.csv").write_text("a,b\n")
    assert flow_code_digest(plugin.flow_class) != before


def test_the_flow_code_digest_ignores_compiled_files_and_files_outside_the_package(
    tmp_path, plugin
):
    before = flow_code_digest(plugin.flow_class)
    (plugin.root / "neighbor.py").write_text("X = 1\n")
    (plugin.directory / "__pycache__").mkdir(exist_ok=True)
    (plugin.directory / "__pycache__" / "extra.pyc").write_bytes(b"\0")
    assert flow_code_digest(plugin.flow_class) == before


def test_the_files_of_a_run_root_in_the_package_do_not_count(plugin):
    """A launch started in the package directory writes its run root there."""
    before = flow_code_digest(plugin.flow_class)
    for name in ("xeda_run", "runs_elsewhere"):
        run_root = plugin.directory / name
        _write(run_root / ".xeda-run-root", "{}\n")
        _write(run_root / "design" / "flow" / "out.txt", "output\n")
    assert flow_code_digest(plugin.flow_class) == before


# The digest of a plugin flow in a module outside a package


def test_a_flow_in_a_module_covers_what_the_module_imports(loose_plugin):
    root = loose_plugin.root
    digest = flow_code_digest(loose_plugin.flow_class)
    # The helper module, the package that the helper imports, and the template count.
    for path, text in (
        (root / f"{loose_plugin.helper}.py", f"import {loose_plugin.library}\nTEXT = 'x'\n"),
        (root / loose_plugin.library / "data.txt", "other data\n"),
        (root / "templates" / "message.txt", "message two\n"),
    ):
        original = path.read_text()
        path.write_text(text)
        changed = flow_code_digest(loose_plugin.flow_class)
        assert changed != digest, path
        path.write_text(original)
        assert flow_code_digest(loose_plugin.flow_class) == digest


def test_a_flow_in_a_namespace_package_covers_the_modules_it_imports_relatively(
    tmp_path, monkeypatch
):
    """The directory has no `__init__.py`, so it is no package for the code root, and the
    relative import names a module next to the flow."""
    unique = uuid.uuid4().hex[:8]
    namespace = f"namespace_{unique}"
    root = tmp_path / "code"
    _write(root / namespace / "helper.py", 'TEXT = "one"\n')
    _write(root / namespace / "other.py", "OTHER = 1\n")
    _write(root / namespace / "flows.py", FLOW_MODULE.format(cls="NamespaceToy"))
    _write(root / namespace / "templates" / "message.txt", "message one\n")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    with preserved_flow_registry():
        try:
            flow_class = importlib.import_module(f"{namespace}.flows").NamespaceToy
            digest = flow_code_digest(flow_class)
            (root / namespace / "other.py").write_text("OTHER = 2\n")
            assert flow_code_digest(flow_class) == digest
            (root / namespace / "helper.py").write_text('TEXT = "two"\n')
            assert flow_code_digest(flow_class) != digest
        finally:
            _forget_modules(namespace)


def test_a_file_next_to_a_module_that_it_does_not_import_does_not_count(loose_plugin):
    digest = flow_code_digest(loose_plugin.flow_class)
    (loose_plugin.root / "unrelated.py").write_text("UNRELATED = 2\n")
    (loose_plugin.root / "notes.txt").write_text("a design directory has many files\n")
    assert flow_code_digest(loose_plugin.flow_class) == digest


def test_a_moved_module_has_the_same_flow_code_digest(tmp_path, monkeypatch, loose_plugin):
    digest = flow_code_digest(loose_plugin.flow_class)
    names = [loose_plugin.package, loose_plugin.helper, loose_plugin.library]
    moved = _import_copy(monkeypatch, loose_plugin, names, tmp_path / "elsewhere" / "scripts")
    assert flow_code_digest(moved) == digest


def test_a_helper_and_a_template_directory_that_are_links_keep_the_digest(tmp_path, loose_plugin):
    """The files lie outside the directory of the module, and the digest is as for the files in
    it."""
    digest = flow_code_digest(loose_plugin.flow_class)
    shared = tmp_path / "shared"
    shared.mkdir()
    helper = loose_plugin.root / f"{loose_plugin.helper}.py"
    shutil.move(helper, shared / helper.name)
    shutil.move(loose_plugin.root / "templates", shared / "templates")
    shutil.move(loose_plugin.root / loose_plugin.library, shared / loose_plugin.library)
    helper.symlink_to(shared / helper.name)
    (loose_plugin.root / "templates").symlink_to(shared / "templates")
    (loose_plugin.root / loose_plugin.library).symlink_to(shared / loose_plugin.library)
    assert flow_code_digest(loose_plugin.flow_class) == digest


def test_an_edit_of_a_helper_of_a_module_makes_the_next_launch_stale(
    tmp_path, loose_plugin, design
):
    _launch(tmp_path, loose_plugin, design)
    assert _launch(tmp_path, loose_plugin, design).reused
    helper = loose_plugin.root / f"{loose_plugin.helper}.py"
    helper.write_text(helper.read_text() + "# edited\n")
    assert not _launch(tmp_path, loose_plugin, design).reused


# A class without a source file on disk


def _classes_without_a_file(monkeypatch) -> dict[str, type[Flow]]:
    """Flow classes as a notebook, the interactive interpreter and `exec` make them."""
    import linecache

    source = (
        "from xeda.flow import Flow\n"
        "class {name}(Flow):\n"
        "    '''A flow with no file.'''\n"
        "    results_description = {{}}\n"
    )
    classes = {}
    # A notebook cell: a module whose file name is not a file, known to `linecache`.
    cell = ModuleType("notebook_cell")
    cell.__file__ = "<ipython-input-1-0000>"
    monkeypatch.setitem(sys.modules, "notebook_cell", cell)
    monkeypatch.setitem(
        linecache.cache, cell.__file__, (1, None, ["x\n"], cell.__file__)  # type: ignore[arg-type]
    )
    exec(compile(source.format(name="CellToy"), cell.__file__, "exec"), cell.__dict__)
    classes["cell"] = cell.CellToy  # type: ignore[attr-defined]
    # The interactive interpreter: `__main__` has no file.
    main = ModuleType("__main__")
    monkeypatch.setitem(sys.modules, "__main__", main)
    exec(compile(source.format(name="MainToy"), "<stdin>", "exec"), main.__dict__)
    classes["main"] = main.MainToy  # type: ignore[attr-defined]
    # A class whose module is not in `sys.modules`.
    namespace: dict = {"__name__": "module_nobody_imported"}
    exec(compile(source.format(name="OrphanToy"), "<string>", "exec"), namespace)
    classes["orphan"] = namespace["OrphanToy"]
    return classes


def test_a_flow_class_without_a_source_file_is_refused_with_its_name(monkeypatch):
    with preserved_flow_registry():
        classes = _classes_without_a_file(monkeypatch)
        for kind, flow_class in classes.items():
            with pytest.raises(FlowFatalError) as refused:
                flow_code_digest(flow_class)
            message = str(refused.value)
            assert f"{flow_class.__module__}.{flow_class.__qualname__}" in message, kind
            assert "no source file on disk" in message and "module file" in message, kind
            with pytest.raises(FlowFatalError):
                code_digest.check_flow_code(flow_class)


def test_planning_refuses_a_flow_class_without_a_source_file(tmp_path, design, monkeypatch):
    """The refusal comes at planning, before the run directory is made."""
    with preserved_flow_registry():
        flow_class = _classes_without_a_file(monkeypatch)["cell"]
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        with pytest.raises(FlowFatalError, match="CellToy"):
            runner.plan(flow_class, design)
        with pytest.raises(FlowFatalError, match="CellToy"):
            runner.launch_flow(flow_class, design, {})
        assert not (tmp_path / "xeda_run").exists() or not any((tmp_path / "xeda_run").iterdir())


# A launch keeps its digests for its length and no longer


def test_a_launch_reads_the_xeda_package_once(monkeypatch):
    calls = []
    real = code_digest.xeda_code_digest
    monkeypatch.setattr(code_digest, "xeda_code_digest", lambda: calls.append(1) or real())
    digests = CodeDigests()
    assert digests.xeda() == digests.xeda()
    assert len(calls) == 1
    CodeDigests().xeda()
    assert len(calls) == 2


def _count_reads(monkeypatch) -> list[Path]:
    """Return the list that gets each file the digests read from now on."""
    from xeda import digest

    read: list[Path] = []
    real = digest.content_digest
    monkeypatch.setattr(
        digest, "content_digest", lambda path: read.append(Path(path)) or real(path)
    )
    return read


def test_a_settled_relaunch_reads_no_code_file_and_an_edit_reads_only_that_file(
    plugin, monkeypatch
):
    """Each launch checks every code file by its metadata, as the trace checks its inputs."""
    import os
    import time

    from xeda import digest

    monkeypatch.setattr(digest, "RACY_NS", 300_000_000)  # 0.3 s rather than 2 s, not to wait
    time.sleep(0.5)
    cls = plugin.flow_class
    first = (flow_code_digest(cls), xeda_code_digest())
    read = _count_reads(monkeypatch)
    assert (flow_code_digest(cls), xeda_code_digest()) == first
    assert read == [], "a settled file was read again"
    helper = plugin.directory / "helper.py"
    _write(helper, 'TEXT = "two\\n"\n')
    assert flow_code_digest(cls) != first[0]
    assert read == [helper.resolve()]
    # An edit of the same size, with the old modification time put back, changes the inode change
    # time, and the next launch notices it.
    time.sleep(0.5)
    flow_code_digest(cls)
    stat = helper.stat()
    settled = flow_code_digest(cls)
    helper.write_text('TEXT = "six\\n"\n')
    os.utime(helper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert flow_code_digest(cls) != settled


def test_all_the_flows_of_a_tree_get_one_snapshot_of_it_in_a_launch(plugin):
    """Two flow classes of one package see the same files, also when the tree changes between
    their first lookups. The next launch reads the tree again."""
    _write(plugin.directory / "more.py", FLOW_MODULE.format(cls="SecondToy"))
    second = importlib.import_module(f"{plugin.package}.more").SecondToy
    assert second is not plugin.flow_class
    digests = CodeDigests()
    first = digests.flow(plugin.flow_class)
    _write(plugin.directory / "helper.py", 'TEXT = "changed"\n')
    assert digests.flow(second) == first
    assert digests.flow(plugin.flow_class) == first
    assert CodeDigests().flow(second) != first
