"""The code digests of a launch: they are of the code that the process imported, name the files
relative to the root of the code, and cover what a flow imports. A file of that code that changes
on disk after the import makes the process refuse (`stale_runtime`).

A plugin flow is a package or a module written under `tmp_path`, imported for the test and
removed from the process after it. A test that changes a file of it after the import shows that
the file counts by the refusal. A new process runs the new code (`test_code_epoch.py`).
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
from xeda.errors import StaleRuntimeError
from xeda._internal import code_epoch
from xeda._internal.flow import Flow, FlowFatalError
from xeda._internal.flow_runner import DefaultRunner, code_digest
from xeda._internal.package_paths import impl_root, package_root
from xeda._internal.flow_runner.code_digest import (
    CodeDigests,
    _named_files,
    check_code,
    code_fingerprint,
    flow_code_digest,
    xeda_code_digest,
)

from xeda._internal.spec import Spec
from xeda._internal.workspace import Workspace

from .flow_registry import preserved_flow_registry
from .source_layout import layout_script

pytestmark = pytest.mark.python_compat

FLOW_MODULE = layout_script("""
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
""")

LOOSE_FLOW_MODULE = layout_script("""
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
""")


SUBCLASS_MODULE = """
    from .flows import PackagedToy


    class SecondToy(PackagedToy):
        '''A flow of the same package.'''
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
    _write(root / package / "more.py", SUBCLASS_MODULE)
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
    return Design(name="toy", rtl={"sources": ["a.v"], "top": "a"}, root=tmp_path)


def _launch(tmp_path, plugin, design) -> Flow:
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.launch_flow(plugin.flow_class, design, {})
    assert flow.succeeded
    return flow


def _refused(tmp_path, plugin, design, changed: str) -> None:
    """Assert that a launch refuses with `stale_runtime`, naming `changed`, and that it leaves the
    trace and the output of the last run as they were."""
    trace = next((tmp_path / "xeda_run").rglob("trace.json"))
    before = trace.read_bytes(), (trace.parent / "out.txt").read_bytes()
    with pytest.raises(StaleRuntimeError, match=f"changed on disk after .*{changed}") as raised:
        _launch(tmp_path, plugin, design)
    assert raised.value.code == "stale_runtime"
    assert (trace.read_bytes(), (trace.parent / "out.txt").read_bytes()) == before


# The digests of Xeda's own code


def test_the_xeda_code_digest_covers_helpers_templates_and_data():
    """An editable install keeps its version string across edits: a helper flows use (template
    filters in utils.py, cocotb.py, tool.py) or bundled board data must count as xeda's code."""
    from xeda._internal.code_epoch import package_files

    files = {
        p.relative_to(code_digest.XEDA_PACKAGE).as_posix()
        for p in package_files(code_digest.XEDA_PACKAGE)
    }
    inside = impl_root().relative_to(package_root()).as_posix()
    wanted = {"utils.py", "cocotb.py", "tool.py", "data/boards.toml"}
    assert {f"{inside}/{name}" for name in wanted} <= files
    assert f"{inside}/flows/yosys/templates/yosys_synth.ys" in files
    assert {"__init__.py", "errors.py", "introspect.py", "data/schemas/run.schema.json"} <= files
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)
    assert xeda_code_digest() == xeda_code_digest() and len(xeda_code_digest()) == 32


def test_a_flow_of_xeda_s_own_has_no_separate_code_digest():
    from xeda._internal.flows import YosysFpga

    assert flow_code_digest(YosysFpga) == ""


def _package_record(package: Path) -> code_epoch.CodeRoot:
    """Return a record of a copy of the xeda package, by the rule of the record of the process."""
    return code_epoch.CodeRoot(
        "The copy",
        lambda: [
            (file.relative_to(package).as_posix(), file)
            for file in code_epoch.package_files(package)
        ],
    )


def test_the_xeda_code_digest_is_of_the_files_and_not_where_they_are(tmp_path):
    """A copy of the package at another place has the digest of the package, also once it moved.
    An edit of a template in it refuses the next check."""
    copy = tmp_path / "elsewhere" / "xeda"
    shutil.copytree(
        code_digest.XEDA_PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    assert code_epoch.digest_pairs(_package_record(copy).verified_pairs()) == xeda_code_digest()
    moved = tmp_path / "moved" / "xeda"
    moved.parent.mkdir()
    copy.rename(moved)
    record = _package_record(moved)
    assert code_epoch.digest_pairs(record.verified_pairs()) == xeda_code_digest()
    template = (
        moved
        / impl_root().relative_to(package_root())
        / "flows"
        / "yosys"
        / "templates"
        / "yosys_synth.ys"
    )
    template.write_text(template.read_text() + "# edited\n")
    with pytest.raises(StaleRuntimeError, match="yosys_synth.ys"):
        record.verified_pairs()


# The digest of a plugin flow in a package


def test_an_edit_of_a_template_after_the_import_refuses_the_next_launch(tmp_path, plugin, design):
    _launch(tmp_path, plugin, design)
    assert _launch(tmp_path, plugin, design).reused
    (plugin.directory / "templates" / "message.txt").write_text("message two\n")
    _refused(tmp_path, plugin, design, "templates/message.txt")


def test_an_edit_of_a_helper_module_after_the_import_refuses_the_next_launch(
    tmp_path, plugin, design
):
    """The helper is a module of the package that the module of the flow imports. The process
    runs the helper that it imported, so no launch may name the new one."""
    _launch(tmp_path, plugin, design)
    _write(plugin.directory / "helper.py", 'TEXT = "two\\n"\n')
    _refused(tmp_path, plugin, design, "helper.py")


def test_a_package_that_loses_its_init_file_stays_stale_and_starts_no_new_record(plugin):
    """A branch switch that removes the `__init__.py` of the package does not make the module of
    the flow a code root of its own, which would record the changed helper as imported."""
    flow_code_digest(plugin.flow_class)
    (plugin.directory / "__init__.py").unlink()
    _write(plugin.directory / "helper.py", 'TEXT = "two\\n"\n')
    with pytest.raises(StaleRuntimeError):
        flow_code_digest(plugin.flow_class)


def test_a_changed_plugin_refuses_every_entry_before_the_request_writes(tmp_path, plugin, design):
    """The entry checks the code of every registered flow, so `clean` empties nothing."""
    out = _launch(tmp_path, plugin, design).run_path / "out.txt"
    _write(plugin.directory / "helper.py", 'TEXT = "two\\n"\n')
    workspace = Workspace(tmp_path / "xeda_run")
    with pytest.raises(StaleRuntimeError, match="helper.py"):
        workspace.run(Spec(plugin.flow_class.name, design), clean=True)
    with pytest.raises(StaleRuntimeError, match="helper.py"):
        Workspace(tmp_path / "other").plan(Spec("yosys_fpga", design))
    assert out.read_text() == "one\nmessage one\n"


def test_a_new_import_of_the_flow_keeps_the_record_of_its_code(plugin):
    """The record of a code root is taken once: an import of the modules again creates new
    classes of the same root, and the root is still the one that the process imported first."""
    digest = flow_code_digest(plugin.flow_class)
    _write(plugin.directory / "helper.py", 'TEXT = "two\\n"\n')
    _forget_modules(plugin.package)
    importlib.invalidate_caches()
    again = importlib.import_module(f"{plugin.package}.flows").PackagedToy
    assert again is not plugin.flow_class
    with pytest.raises(StaleRuntimeError, match="helper.py"):
        flow_code_digest(again)
    _write(plugin.directory / "helper.py", 'TEXT = "one\\n"\n')
    assert flow_code_digest(again) == digest  # the content that the process imported


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
    flow_code_digest(plugin.flow_class)
    (plugin.directory / "data" / "table.csv").parent.mkdir()
    (plugin.directory / "data" / "table.csv").write_text("a,b\n")
    with pytest.raises(StaleRuntimeError, match=f"{plugin.package}/data/table.csv"):
        flow_code_digest(plugin.flow_class)


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


def test_a_directory_named_like_a_run_root_without_its_marker_counts(plugin):
    """Only a directory that holds the marker of a run root is left out, never one by its name."""
    flow_code_digest(plugin.flow_class)
    _write(plugin.directory / "xeda_run" / "table.csv", "a,b\n")
    with pytest.raises(StaleRuntimeError, match="xeda_run/table.csv"):
        flow_code_digest(plugin.flow_class)


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
        with pytest.raises(StaleRuntimeError):
            flow_code_digest(loose_plugin.flow_class)
        path.write_text(original)  # the content that the process imported: no refusal
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
            with pytest.raises(StaleRuntimeError, match="helper.py"):
                flow_code_digest(flow_class)
        finally:
            _forget_modules(namespace)


def test_a_dotted_import_through_a_directory_without_an_init_file_is_followed(
    tmp_path, monkeypatch
):
    """`import tools.codec` reaches `tools/codec.py` through a namespace directory. The directory
    counts only by the module found, so another file in it does not."""
    unique = uuid.uuid4().hex[:8]
    module, namespace = f"dotted_{unique}", f"tools_{unique}"
    root = tmp_path / "scripts"
    _write(root / namespace / "codec.py", 'TEXT = "one"\n')
    _write(root / namespace / "unused.py", "UNUSED = 1\n")
    _write(root / "templates" / "message.txt", "message one\n")
    _write(
        root / f"{module}.py",
        LOOSE_FLOW_MODULE.format(cls="DottedToy", helper=f"{namespace}.codec"),
    )
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    with preserved_flow_registry():
        try:
            flow_class = importlib.import_module(module).DottedToy
            digest = flow_code_digest(flow_class)
            (root / namespace / "unused.py").write_text("UNUSED = 2\n")
            assert flow_code_digest(flow_class) == digest
            (root / namespace / "codec.py").write_text('TEXT = "two"\n')
            with pytest.raises(StaleRuntimeError, match="codec.py"):
                flow_code_digest(flow_class)
        finally:
            _forget_modules(module)
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


def test_an_edit_of_a_helper_of_a_module_refuses_the_next_launch(tmp_path, loose_plugin, design):
    _launch(tmp_path, loose_plugin, design)
    assert _launch(tmp_path, loose_plugin, design).reused
    helper = loose_plugin.root / f"{loose_plugin.helper}.py"
    helper.write_text(helper.read_text() + "# edited\n")
    _refused(tmp_path, loose_plugin, design, f"{loose_plugin.helper}.py")


# A class without a source file on disk


RAN: list[str] = []


def _classes_without_a_file(monkeypatch) -> dict[str, type[Flow]]:
    """Flow classes as a notebook, the interactive interpreter and `exec` make them."""
    import linecache

    source = layout_script(
        "from xeda.flow import Flow\n"
        "class {name}(Flow):\n"
        "    '''A flow with no file.'''\n"
        "    results_description = {{}}\n"
        "    design_source_types = frozenset()\n"
        "    def run(self):\n"
        "        RAN.append(self.name)\n"
    )
    classes = {}
    # A notebook cell: a module whose file name is not a file, known to `linecache`.
    cell = ModuleType("notebook_cell")
    cell.__file__ = "<ipython-input-1-0000>"
    cell.RAN = RAN  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "notebook_cell", cell)
    monkeypatch.setitem(
        linecache.cache, cell.__file__, (1, None, ["x\n"], cell.__file__)  # type: ignore[arg-type]
    )
    exec(compile(source.format(name="CellToy"), cell.__file__, "exec"), cell.__dict__)
    classes["cell"] = cell.CellToy  # type: ignore[attr-defined]
    # The interactive interpreter: `__main__` has no file.
    main = ModuleType("__main__")
    main.RAN = RAN  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "__main__", main)
    exec(compile(source.format(name="MainToy"), "<stdin>", "exec"), main.__dict__)
    classes["main"] = main.MainToy  # type: ignore[attr-defined]
    # A class whose module is not in `sys.modules`.
    namespace: dict = {"__name__": "module_nobody_imported", "RAN": RAN}
    exec(compile(source.format(name="OrphanToy"), "<string>", "exec"), namespace)
    classes["orphan"] = namespace["OrphanToy"]
    return classes


def test_a_flow_class_without_a_source_file_always_runs_and_has_no_digest(
    tmp_path, design, monkeypatch
):
    """The reason names the class. A launch runs the flow every time, keeps no trace, and never
    asks for a code digest."""
    asked = []
    monkeypatch.setattr(
        code_digest, "flow_code_digest", lambda cls, snapshots=None: asked.append(cls.name) or ""
    )
    with preserved_flow_registry():
        classes = _classes_without_a_file(monkeypatch)
        for kind, flow_class in classes.items():
            label = f"{flow_class.__module__}.{flow_class.__qualname__}"
            reason = flow_class(
                flow_class.Settings(), design, tmp_path / kind, runner_cwd=tmp_path
            ).always_runs()
            assert reason is not None and label in reason, kind
            assert "no source file on disk" in reason, kind
            assert "cannot tell whether its code changed" in reason, kind
            with pytest.raises(FlowFatalError, match="no code digest"):
                flow_code_digest(flow_class)
            for launch in (1, 2):
                RAN.clear()
                runner = DefaultRunner(tmp_path / f"run_{kind}", display_results=False)
                flow = runner.launch_flow(flow_class, design, {})
                assert flow.succeeded and not flow.reused, (kind, launch)
                assert RAN == [flow.name], (kind, launch)
                assert flow.stale_reason == reason, (kind, launch)
                assert not (flow.run_path / "trace.json").exists(), kind
    assert not {flow_class.name for flow_class in classes.values()} & set(asked)


def test_a_dry_run_reports_a_flow_class_without_a_source_file_as_always_running(
    tmp_path, design, monkeypatch
):
    with preserved_flow_registry():
        for kind, flow_class in _classes_without_a_file(monkeypatch).items():
            runner = DefaultRunner(tmp_path / f"run_{kind}", display_results=False)
            runner.launch_flow(flow_class, design, {})
            _, states = runner.plan_with_freshness(flow_class, design)
            state = states[flow_class.name]
            assert state.state == "stale", kind
            assert "no source file on disk" in state.reason, kind


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
    """Return the list that gets each code file that a check reads from now on."""
    read: list[Path] = []
    real = code_epoch.content_digest
    monkeypatch.setattr(
        code_epoch, "content_digest", lambda path: read.append(Path(path)) or real(path)
    )
    return read


def test_a_settled_check_reads_no_code_file_and_an_edit_is_refused(plugin, monkeypatch):
    """Each launch checks every code file by its metadata, as the trace checks its inputs. A file
    read again with the content that the process imported is no change."""
    import os
    import time

    monkeypatch.setattr(code_epoch, "RACY_NS", 300_000_000)  # 0.3 s rather than 2 s, not to wait
    time.sleep(0.5)
    cls = plugin.flow_class
    first = (flow_code_digest(cls), xeda_code_digest())
    read = _count_reads(monkeypatch)
    assert (flow_code_digest(cls), xeda_code_digest()) == first
    assert read == [], "a settled file was read again"
    helper = plugin.directory / "helper.py"
    os.utime(helper)  # new metadata, the same content
    assert flow_code_digest(cls) == first[0]
    assert read == [helper.resolve()]
    # An edit of the same size, with the old modification time put back, changes the inode change
    # time, and the next check notices it.
    time.sleep(0.5)
    flow_code_digest(cls)
    stat = helper.stat()
    helper.write_text('TEXT = "six\\n"\n')
    os.utime(helper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(StaleRuntimeError, match="helper.py"):
        flow_code_digest(cls)


def test_the_flows_of_a_tree_share_one_record_and_the_check_reads_it_again(plugin):
    """Two flow classes of one package have one record of it. A launch keeps the digests that it
    computed (`CodeDigests`), and its check before a trace (`check_code`) reads the files again."""
    second = importlib.import_module(f"{plugin.package}.more").SecondToy
    assert second is not plugin.flow_class
    digests = CodeDigests()
    first = digests.flow(plugin.flow_class)
    assert flow_code_digest(second) == first
    _write(plugin.directory / "helper.py", 'TEXT = "changed"\n')
    assert digests.flow(plugin.flow_class) == first
    with pytest.raises(StaleRuntimeError):
        CodeDigests().flow(second)
    with pytest.raises(StaleRuntimeError):
        check_code(plugin.flow_class)


def test_a_class_in_the_tree_of_its_base_adds_nothing_to_the_flow_code_digest(plugin):
    """The tree of a package counts once, whatever number of the classes of the MRO are in it."""
    second = importlib.import_module(f"{plugin.package}.more").SecondToy
    assert flow_code_digest(second) != ""
    # the files of the package are the same; `more.py` is one of them
    names = [name for name, _ in _named_files(plugin.flow_class)]
    assert f"{plugin.package}/more.py" in names
    assert flow_code_digest(second) == flow_code_digest(plugin.flow_class)


def test_the_code_fingerprint_names_no_place(tmp_path, monkeypatch, plugin):
    """The fingerprint holds the digest of Xeda and of each flow, so the same code imported at
    another place has the same one."""
    fingerprint = code_fingerprint([plugin.flow_class])
    assert fingerprint == {
        "xeda": xeda_code_digest(),
        plugin.flow_class.name: flow_code_digest(plugin.flow_class),
    }
    moved = _import_copy(monkeypatch, plugin, [plugin.package], tmp_path / "elsewhere" / "code")
    assert code_fingerprint([moved]) == fingerprint


def test_two_code_roots_with_the_same_file_count_twice(tmp_path, monkeypatch):
    """Two modules in two places, with the same name and the same text, are two roots: taking one
    of them away from the classes of a flow changes the digest."""
    classes = []
    for place in ("one", "two"):
        _write(tmp_path / place / "twin.py", "class Twin:\n    pass\n")
        spec = importlib.util.spec_from_file_location(
            f"twin_{place}_{uuid.uuid4().hex[:8]}", tmp_path / place / "twin.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        classes.append(module.Twin)
    monkeypatch.setattr(code_digest, "plugin_classes", lambda flow_class: classes)
    both = flow_code_digest(Flow)
    monkeypatch.setattr(code_digest, "plugin_classes", lambda flow_class: classes[:1])
    assert flow_code_digest(Flow) != both
