"""The package layout has one source (`xeda._internal.package_paths`): no module path, file depth or source
path is written out anywhere else, so a change of the layout touches that module only."""

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import types
from importlib import resources
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

import xeda
from xeda import Design
from xeda._internal import package_paths
from xeda._internal.plan import Plan
from xeda._internal.spec import Spec
from xeda._internal.workspace import Workspace
from xeda.errors import DesignError
from xeda.introspect import flows_info, results_info, settings_info
from xeda._internal.introspect import optimizers_info
from xeda._internal.package_paths import impl_module

from .tool_utils import require_git, use_fake_tools
from .source_layout import (
    AGENT_DIR,
    IMPL_DIR,
    PUBLIC,
    ROOT,
    SCHEMAS_DIR,
    SRC_XEDA,
    internal_names,
)

TOOLS = ROOT / "tools"
TESTS = ROOT / "tests"
#: A dotted path of an internal module inside a string. A file name (`xeda.json`) and a path
#: (`~/.config/xeda.fish`) do not match. `xeda.local_settings(` is the function that `xeda`
#: exports.
MODULE_PATH = re.compile(r"(?<![\w/.~-])xeda\.(?:" + "|".join(internal_names()) + r")\b(?!\()")
#: Strings that name a module and are no path to import: the key of the verb module in the API
#: snapshot, which stays the same wherever the module is.
KEYS = {("api_baseline.py", "xeda.workspace_verbs")}
#: Test files whose subject is a module path that is not public: they write it out on purpose.
SUBJECTS = {"test_public_imports.py", "test_package_layout.py"}
#: A path of an internal module or file inside a string, as the wheel lists it. The shipped
#: schemas stay at a public path.
FILE_PATH = re.compile(
    r"(?<![\w/.~-])xeda/(?!data/schemas)(?:" + "|".join(internal_names()) + r")\b"
)


def _python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for parent in ast.walk(tree):
        if (
            isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and parent.body
            and isinstance(parent.body[0], ast.Expr)
            and isinstance(parent.body[0].value, ast.Constant)
        ):
            found.add(id(parent.body[0].value))
    return found


def _script_parts(tree: ast.AST) -> set[int]:
    """The nodes inside an argument of `layout_script`: a script for a subprocess, which names its
    modules as `xeda.<module>` and gets their real path from that function."""
    return {
        id(part)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "layout_script"
        for argument in call.args
        for part in ast.walk(argument)
    }


def _literals(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    skipped = _docstrings(tree) | _script_parts(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skipped:
                yield node.lineno, node.value


def test_no_literal_internal_module_path():
    found = [
        f"{path.relative_to(ROOT)}:{line}: {text!r}"
        for directory in (SRC_XEDA, TOOLS, TESTS)
        for path in _python_files(directory)
        if path.name != "package_paths.py" and path.name not in SUBJECTS
        for line, text in _literals(path)
        if (MODULE_PATH.search(text) or FILE_PATH.search(text)) and (path.name, text) not in KEYS
    ]
    assert not found, "\n".join(found)


def test_no_module_path_in_the_bootstrap_script():
    text = (IMPL_DIR / "remote" / "bootstrap.sh").read_text(encoding="utf-8")
    assert not re.findall(r"-m xeda\.", text)


def _imported_modules(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def test_no_absolute_internal_import_in_product_code():
    allowed = {"xeda", *(f"xeda.{name}" for name in PUBLIC)}
    found = []
    for path in _python_files(SRC_XEDA):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for line, module in _imported_modules(tree):
            if (module == "xeda" or module.startswith("xeda.")) and module not in allowed:
                found.append(f"{path.relative_to(ROOT)}:{line}: {module}")
    assert not found, "\n".join(found)


def test_no_source_layout_path_outside_one_helper():
    # The patterns are built here, so that this file does not match them.
    patterns = ['"src" ' + '/ "xeda"', "src" + "/xeda/", '"src", ' + '"xeda"']
    found = []
    for directory in (TESTS, TOOLS):
        for path in _python_files(directory):
            if path.name == "source_layout.py":
                continue
            text = path.read_text(encoding="utf-8")
            found += [f"{path.relative_to(ROOT)}: {p}" for p in patterns if p in text]
    assert not found, "\n".join(found)


def _mentions_file(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(node))


def test_no_file_depth_arithmetic():
    found = []
    for path in _python_files(SRC_XEDA):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            deep = (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and _mentions_file(node.value.value)
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr == "parent"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parent"
                and _mentions_file(node.value.value)
            )
            if deep:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not found, "\n".join(found)


def test_remote_module_names_come_from_one_place():
    from xeda._internal.remote.provision import boot_script

    assert "@XEDA_AGENT_MODULE@" not in boot_script()
    assert f"-m {impl_module('remote.agent')}" in boot_script()


def test_the_directories_agree_with_the_installed_modules():
    """The installed package, which may not be the checkout (a test run from an sdist)."""
    import xeda

    assert package_paths.package_root() == Path(xeda.__file__).absolute().parent
    assert package_paths.source_root() == package_paths.package_root().parent
    impl = importlib.import_module(package_paths.IMPL)
    assert Path(str(impl.__file__)).parent.samefile(package_paths.impl_root())
    module = importlib.import_module(impl_module("package_paths"))
    assert Path(str(module.__file__)) == package_paths.impl_root() / "package_paths.py"
    flows = importlib.import_module(impl_module("flows"))
    assert Path(str(flows.__file__)).parent == package_paths.impl_root() / "flows"
    assert resources.files(impl_module("flows")).joinpath("__init__.py").is_file()
    assert package_paths.schemas_dir().is_dir()
    assert (package_paths.schemas_dir() / "design.schema.json").is_file()


def test_the_checkout_layout_has_the_same_relative_paths():
    """The checkout and the installed package agree on where things are, not on the root."""
    assert IMPL_DIR.relative_to(SRC_XEDA) == package_paths.impl_root().relative_to(
        package_paths.package_root()
    )
    assert SCHEMAS_DIR.relative_to(SRC_XEDA) == package_paths.schemas_dir().relative_to(
        package_paths.package_root()
    )
    assert (IMPL_DIR / "package_paths.py").is_file()
    assert (AGENT_DIR / "SKILL.md").is_file()
    assert (SCHEMAS_DIR / "design.schema.json").is_file()


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


PYTHON_NAMES = ("class", "module", "qualified_name", "settings_class", "declared_by")


def test_no_document_names_a_python_module():
    documents: list[tuple[str, object]] = [("list-flows", flows_info())]
    documents.append(("list-optimizers", optimizers_info()))
    names = [flow["name"] for flow in flows_info()]
    for name in names:
        documents.append((f"list-settings {name}", settings_info(name)))
        documents.append((f"list-results {name}", results_info(name)))
    found = []
    for label, document in documents:
        text = json.dumps(document, default=str)
        found += [
            f"{label}: {value!r}"
            for value in _strings(json.loads(text))
            if value.startswith("xeda.")
        ]
    assert not found, "\n".join(found[:20])
    for entry in [*flows_info(), *optimizers_info()]:
        assert not set(PYTHON_NAMES) & set(entry), entry["name"]
    for name in names:
        info = settings_info(name)
        assert "settings_class" not in info
        assert all("declared_by" not in field for field in info["fields"])


# ---------------------------------------------------------------------------------------------
# Identities: a node's identity and run directory do not depend on where the implementation is.
# The values were recorded before the implementation moved into its own package. A new setting
# of a flow changes the identity of its nodes and of the nodes that read them: record them again.
# ---------------------------------------------------------------------------------------------

PART = "LFE5U-25F-6BG381C"
#: The identity of every node of five fixed specs, by `<spec>:<node>`.
IDENTITIES = {
    "source:yosys_fpga": "49adb5ee9f809ea0d1ef303f8866052254464414a6c2799756033fd0271b0c6f",
    "source:nextpnr": "9114085b84a7a4b3754337fa57e8603a15012dd6a2ffa5f39d89ac8ea161d487",
    "converter:bsc": "5b17d483dc05c2cf0a072a3256405bff74d145d61b5ae0127b847b6a00187cc3",
    "converter:yosys_fpga": "e23cec8df0c725132f8d5c2dbda2af8704f14709dda5fc559346584bfc878251",
    "netlist:nextpnr": "434e88e76aea5be1543890a2135d3a4e3ad464ad9f7930de5e534a29ccf79dc9",
    "generator:yosys_fpga": "65943beb89e03fa198e4e403e046d55504b10c2a3721353cd7588986dcdccd8d",
    "dependency:yosys_fpga": "2b1302be4f97960a6d22bf8cb9eb789d8d883580a3f3c4bf909d980c397c14a2",
}
#: A run of the `netlist` spec, as the code before the move left it: its trace and results.
OLD_RUN = ROOT / "tests" / "resources" / "pre_move_run"
FIXED_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_spec(root: Path) -> Spec:
    root.mkdir(parents=True)
    (root / "blink.v").write_text(
        "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
    )
    rtl = {"sources": ["blink.v"], "top": "blink", "clocks": ["clk"]}
    design = Design(name="blink", root=root, rtl=rtl)
    return Spec("nextpnr", design, settings={"nextpnr": {"fpga": {"part": PART}}})


def _converter_spec(root: Path) -> Spec:
    root.mkdir(parents=True)
    (root / "Top.bsv").write_text("package Top;\nendpackage\n")
    rtl = {"sources": ["Top.bsv"], "top": "mkTop", "clocks": [{"port": "CLK"}]}
    design = Design(name="bsv", root=root, rtl=rtl)
    return Spec("yosys_fpga", design, settings={"yosys_fpga": {"fpga": {"part": PART}}})


def _netlist_spec(root: Path) -> Spec:
    root.mkdir(parents=True)
    (root / "n.json").write_text('{"modules": {"top": {}}}\n')
    rtl = {"sources": [{"file": "n.json", "type": "JsonNetlist"}], "top": "top"}
    design = Design(name="net", root=root, rtl=rtl)
    return Spec("nextpnr", design, settings={"nextpnr": {"fpga": {"part": PART}}})


def _generator_spec(root: Path) -> Spec:
    root.mkdir(parents=True)
    (root / "gen.py").write_text("print('generated')\n")
    (root / "spec.txt").write_text("one\n")
    recipe = {
        "executable": "python3",
        "args": ["$DESIGN_ROOT/gen.py"],
        "sources": ["spec.txt", "gen.py"],
    }
    rtl = {
        "sources": ["$XEDA_GENERATED/gen/top.v"],
        "top": "top",
        "clocks": ["clk"],
        "generator": recipe,
    }
    (root / "design.yaml").write_text(yaml.safe_dump({"name": "gen", "rtl": rtl}))
    return Spec(
        "yosys_fpga",
        Design.load(root / "design.yaml"),
        settings={"yosys_fpga": {"fpga": {"part": PART}}},
    )


def _dependency_spec(root: Path) -> Spec:
    """A design with a Git dependency from a bare repository, whose commit is always the same."""
    work = root.parent / "work"
    work.mkdir(parents=True)
    _git("init", "--quiet", "--initial-branch=main", cwd=work)
    (work / "a.v").write_text("module a; endmodule\n")
    dependency = {"name": "dep", "rtl": {"sources": ["a.v"], "top": "a"}}
    (work / "dep.yaml").write_text(yaml.safe_dump(dependency))
    _git("add", "a.v", "dep.yaml", cwd=work)
    _git("commit", "--quiet", "-m", "first", cwd=work)
    commit = _git("rev-parse", "HEAD", cwd=work)
    bare = root.parent / "remote.git"
    _git("clone", "--quiet", "--bare", str(work), str(bare), cwd=root.parent)
    root.mkdir(parents=True)
    (root / "top.v").write_text("module top; endmodule\n")
    uri = f"git+{bare.as_uri()}?commit={commit}#dep.yaml"
    document = {
        "name": "d",
        "rtl": {"sources": ["top.v"], "clocks": ["clk"]},
        "dependencies": [{"uri": uri, "rtl": {"pos": -1}}],
    }
    (root / "d.yaml").write_text(yaml.safe_dump(document))
    return Spec(
        "yosys_fpga",
        Design.load(root / "d.yaml"),
        settings={"yosys_fpga": {"fpga": {"part": PART}}},
    )


SPECS = {
    "source": _source_spec,
    "converter": _converter_spec,
    "netlist": _netlist_spec,
    "generator": _generator_spec,
    "dependency": _dependency_spec,
}


def _plans(tmp_path: Path) -> dict[str, Plan]:
    workspace = Workspace(tmp_path / "runs", hashed_run_dirs=True)
    return {
        label: workspace.plan(make(tmp_path / "designs" / label / "design"))
        for label, make in SPECS.items()
    }


@pytest.fixture
def fixed_world(monkeypatch):
    require_git()
    use_fake_tools(monkeypatch)
    for name, value in FIXED_GIT.items():
        monkeypatch.setenv(name, value)


def test_identities_do_not_change_with_the_move(tmp_path, fixed_world):
    plans = _plans(tmp_path)
    found = {
        f"{label}:{name}": node.identity
        for label, p in plans.items()
        for name, node in p.nodes.items()
    }
    assert found == IDENTITIES
    # A hashed run directory is named by the identity: no name changes either.
    for label, plan in plans.items():
        for name, node in plan.nodes.items():
            assert node.run_dir.name == f"{node.flow}_{IDENTITIES[f'{label}:{name}'][:16]}"


def test_a_run_root_from_before_the_move_plans(tmp_path, fixed_world):
    """A trace that an earlier Xeda wrote says `stale` with a reason, and never raises."""
    workspace = Workspace(tmp_path / "runs", hashed_run_dirs=True)
    spec = _netlist_spec(tmp_path / "net")
    run_dir = workspace.plan(spec).nodes["nextpnr"].run_dir
    run_dir.mkdir(parents=True)
    (workspace.run_root / ".xeda-run-root").write_text(
        json.dumps({"format": 1, "created_by": "xeda", "created": "2026-11-02T20:42:30+00:00"})
    )
    for name in ("trace.json", "results.json"):
        shutil.copy(OLD_RUN / name, run_dir / name)
    # The version of the code under test: the check judges the rest of the trace.
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["xeda_version"] = xeda.__version__
    (run_dir / "trace.json").write_text(json.dumps(trace))
    node = workspace.plan(spec).nodes["nextpnr"]
    assert node.state == "stale"
    assert node.reason


# ---------------------------------------------------------------------------------------------
# The layout of the package
# ---------------------------------------------------------------------------------------------


def test_only_the_public_modules_are_outside_the_private_package():
    found = sorted(p.name for p in SRC_XEDA.iterdir() if p.name != "__pycache__")
    # `data` holds the JSON Schemas, which are public files.
    assert found == [
        "__init__.py",
        "__main__.py",
        "_internal",
        "data",
        "errors.py",
        "introspect.py",
        "py.typed",
    ]
    assert sorted(p.name for p in (SRC_XEDA / "data").iterdir() if p.name != "__pycache__") == [
        "__init__.py",
        "schemas",
    ]


def test_an_old_internal_class_path_names_the_short_form(tmp_path):
    root = tmp_path / "design"
    root.mkdir()
    (root / "a.v").write_text("module a; endmodule\n")
    recipe = {"class": "xeda.design.ChiselGenerator", "executable": "sbt"}
    document = {"name": "d", "rtl": {"sources": ["a.v"], "top": "a", "generator": recipe}}
    (root / "d.yaml").write_text(yaml.safe_dump(document))
    with pytest.raises(DesignError) as raised:
        Design.load(root / "d.yaml")
    assert raised.value.code == "invalid_design"
    assert raised.value.field == "rtl.generator.class"
    assert "class: chisel" in raised.value.message


def test_an_old_optimizer_class_path_names_the_new_module():
    from xeda._internal.flow_runner.dse.dse_runner import Dse

    with pytest.raises(ValueError, match=re.escape(impl_module("flow_runner.dse"))):
        Dse("xeda.flow_runner.dse.fmax.fmax_optimizer")


def test_the_catalog_is_written_where_it_is_installed():
    assert not (SRC_XEDA / "flow_catalog.json").exists()
    assert (IMPL_DIR / "flow_catalog.json").is_file()
    done = subprocess.run(
        [sys.executable, str(TOOLS / "generate_flow_catalog.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_internal_helpers_are_not_reexported():
    import xeda.introspect

    assert not hasattr(xeda.introspect, "all_flow_classes")
    assert sorted(xeda.introspect.__all__) == sorted(
        name for name in dir(xeda.introspect) if not name.startswith("_")
    )


def test_the_api_has_no_difference_to_the_baseline():
    path = TOOLS / "api_baseline.py"
    tool = types.ModuleType("api_baseline")  # not imported: that would write bytecode
    tool.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), tool.__dict__)
    baseline = json.loads((tool.newest_baseline() / "public_api.json").read_text(encoding="utf-8"))
    additive, breaking = tool.classify(baseline, tool.public_api_snapshot(), kind="api")
    assert not additive and not breaking
