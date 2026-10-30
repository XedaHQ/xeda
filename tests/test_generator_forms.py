"""A generator is written as a command line, a list of arguments or a table, and the design keeps
a table.

A dependency is merged into the design without a second look at what the first validation
derived (the cocotb settings of the testbench).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pydantic
import pytest

from xeda import Design
from xeda.design import POSIX_SHELL, DesignValidationError, Rtl, generator_of
from xeda.flow_runner.dependencies import merged_design
from xeda.flow_runner.launch_generation import generate
from xeda.generation import GenerationError


def _load(root: Path, generator: object) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    return Design(
        name="d",
        root=root,
        rtl={"sources": [{"path": "made.v"}], "top": "top", "generator": generator},
    )


def test_a_command_line_is_a_shell_recipe():
    rtl = Rtl(sources=(), generator="python soc.py --build")
    assert isinstance(rtl.generator, Mapping)
    assert dict(rtl.generator) == {"shell": "python soc.py --build"}


def test_a_list_is_an_executable_with_its_arguments():
    rtl = Rtl(sources=(), generator=["python3", "soc.py", "--build"])
    assert dict(rtl.generator or {}) == {"executable": "python3", "args": ("soc.py", "--build")}
    assert dict(Rtl(sources=(), generator=["make"]).generator or {}) == {
        "executable": "make",
        "args": (),
    }


def test_a_table_stays_as_it_is():
    rtl = Rtl(sources=(), generator={"command": "python3 soc.py", "always_runs": True})
    assert dict(rtl.generator or {}) == {"command": "python3 soc.py", "always_runs": True}


@pytest.mark.parametrize("nothing", ["", [], {}, None])
def test_an_empty_recipe_is_no_generator(nothing):
    assert Rtl(sources=(), generator=nothing).generator is None


def test_the_forms_in_a_design_file_are_the_tables(tmp_path):
    file = tmp_path / "d.yaml"
    for text, expected in [
        ('"echo a"', {"shell": "echo a"}),
        ("[echo, a]", {"executable": "echo", "args": ("a",)}),
        ("{command: echo}", {"command": "echo"}),
    ]:
        file.write_text(
            f"name: d\nrtl:\n  sources: [{{path: made.v}}]\n  top: top\n  generator: {text}\n"
        )
        (tmp_path / "made.v").write_text("module top; endmodule\n")
        design = Design.load(file)
        assert dict(design.rtl.generator or {}) == expected


@pytest.mark.parametrize(
    "table, keys",
    [
        ({"shell": "true", "command": "true"}, "`shell` and `command`"),
        ({"shell": "true", "executable": "true"}, "`shell` and `executable`"),
        ({"shell": "true", "command": "true", "executable": "true"}, "`command` and `executable`"),
        ({"shell": "true", "args": ["a"]}, "`shell` and `args`"),
    ],
)
def test_a_table_with_a_shell_and_another_command_is_refused(tmp_path, table, keys):
    with pytest.raises(DesignValidationError, match=keys):
        _load(tmp_path, table)
    with pytest.raises(pydantic.ValidationError, match=keys):
        Rtl(sources=(), generator=table)


def test_a_chisel_generator_takes_no_shell():
    with pytest.raises(pydantic.ValidationError, match="`shell` is not valid"):
        Rtl(sources=(), generator={"class": "chisel", "shell": "true"})


def _generate(design: Design, tmp_path: Path) -> Path:
    """Run the generator of `design` into a storage under a new run root, and return the storage."""
    run_root = tmp_path / "xeda_run"
    run_root.mkdir(exist_ok=True)
    generation = generate(design, run_root=run_root, environ=dict(os.environ))
    assert generation is not None
    return generation.storage


def test_the_model_of_a_shell_recipe_runs_the_text_in_a_shell(tmp_path):
    design = _load(
        tmp_path / "design",
        "printf 'module top; endmodule\\n' > \"$XEDA_GENERATED/made.v\""
        ' && printf \'%s\' "$DESIGN_ROOT" > "$XEDA_GENERATED/root.txt"',
    )
    root = design.root
    storage = _generate(design, tmp_path)
    assert (storage / "made.v").read_text() == "module top; endmodule\n"
    assert (storage / "root.txt").read_text() == str(root)
    assert not (root / "made.v").exists(), "the design tree is not written"
    generator = generator_of(design.rtl, root=root)
    assert generator is not None and generator.shell is not None
    assert generator.describe() == f"the generator ({generator.shell})"


def test_a_shell_recipe_runs_and_is_identified_by_one_shell(tmp_path, monkeypatch):
    """The freshness identity of a shell recipe names the shell that runs it, `/bin/sh`, and not
    an `sh` that comes first on `PATH`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    other_sh = bin_dir / "sh"
    other_sh.write_text('#!/bin/sh\ntouch "$0.ran"\n')
    other_sh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    design = _load(
        tmp_path / "design", "printf 'module top; endmodule\\n' > \"$XEDA_GENERATED/made.v\""
    )
    root = design.root
    storage = _generate(design, tmp_path)
    assert (storage / "made.v").read_text() == "module top; endmodule\n"
    assert not (bin_dir / "sh.ran").exists()
    generator = generator_of(design.rtl, root=root)
    assert generator is not None
    assert generator.execution_command(root)[:2] == [POSIX_SHELL, "-c"]
    assert generator.execution_executable_path(root, environ=os.environ) == Path(POSIX_SHELL)


def test_an_argument_list_runs_without_a_shell(tmp_path):
    code = "import pathlib, sys; (pathlib.Path(sys.argv[2]) / 'made.v').write_text(sys.argv[1])"
    design = _load(
        tmp_path / "design",
        [sys.executable, "-c", code, "module a; endmodule && echo x > no.txt", "$XEDA_GENERATED"],
    )
    storage = _generate(design, tmp_path)
    assert (storage / "made.v").read_text() == "module a; endmodule && echo x > no.txt"
    assert not (storage / "no.txt").exists()


TOOL = """\
#!/bin/sh
pwd -P > "$XEDA_GENERATED/started_in.txt"
printf 'module top; endmodule\\n' > "$XEDA_GENERATED/made.v"
"""


@pytest.mark.parametrize(
    "recipe",
    [
        "./tool",
        ["./tool"],
        {"command": "./tool"},
        {"executable": "./tool"},
        {"shell": "./tool", "sources": ["tool"]},
    ],
    ids=["shell", "list", "command", "executable", "table"],
)
def test_a_relative_tool_in_the_design_root_runs_in_the_design_root(tmp_path, recipe):
    """The directory that finds `./tool` is the directory the generator starts in."""
    root = tmp_path / "design"
    root.mkdir()
    (root / "tool").write_text(TOOL)
    (root / "tool").chmod(0o755)
    storage = _generate(_load(root, recipe), tmp_path)
    assert (storage / "made.v").read_text() == "module top; endmodule\n"
    assert Path((storage / "started_in.txt").read_text().strip()) == root.resolve()
    assert not (root / "made.v").exists(), "the design tree is not written"


@pytest.mark.parametrize("recipe", ["./tool", ["./tool"]], ids=["shell", "list"])
def test_a_cwd_decides_where_a_relative_tool_is_found_and_run(tmp_path, recipe):
    root = tmp_path / "design"
    (root / "build").mkdir(parents=True)
    (root / "build" / "tool").write_text(TOOL)
    (root / "build" / "tool").chmod(0o755)
    generator = {"cwd": "build", **({"shell": recipe} if isinstance(recipe, str) else {})}
    if not isinstance(recipe, str):
        generator.update(executable=recipe[0])
    storage = _generate(_load(root, generator), tmp_path)
    assert Path((storage / "started_in.txt").read_text().strip()) == (root / "build").resolve()


def test_a_failing_shell_recipe_fails_the_generation(tmp_path):
    design = _load(tmp_path / "design", "exit 3")
    with pytest.raises(GenerationError, match="exit"):
        _generate(design, tmp_path)
    left = [p for p in (tmp_path / "xeda_run" / ".generated").glob("*/*") if p.is_dir()]
    assert left == [], "no storage is left"


# ------------------------------------------------------------------ a dependency and cocotb


def _dependency(root: Path, tb: bool) -> None:
    (root / "dep").mkdir(parents=True, exist_ok=True)
    (root / "lib.v").write_text("module lib; endmodule\n")
    (root / "dep" / "cocotb_tests.py").write_text("# tests\n")
    text = "name: dep\nrtl:\n  sources: [../lib.v]\n  top: lib\n"
    if tb:
        text += "tb:\n  sources: [{file: cocotb_tests.py, type: Cocotb}]\n  top: lib\n"
    (root / "dep" / "dep.yaml").write_text(text)


def test_a_dependency_does_not_switch_cocotb_on_for_a_testbench_that_refused_it(tmp_path):
    root = tmp_path / "d"
    _dependency(root, tb=False)
    (root / "my_tests.py").write_text("# tests\n")
    design = merged_design(
        Design(
            name="d",
            root=root,
            rtl={"sources": ["lib.v"], "top": "lib"},
            tb={
                "sources": [{"file": "my_tests.py", "type": "Cocotb"}],
                "top": "lib",
                "cocotb": False,
            },
            dependencies=[{"uri": "dep/dep.yaml", "rtl": {"pos": -1}}],
        ),
        run_root=None,
    )
    assert design.tb.cocotb is None and design.dependencies == ()
    alone = Design(
        name="d",
        root=root,
        rtl={"sources": ["lib.v"], "top": "lib"},
        tb={"sources": [{"file": "my_tests.py", "type": "Cocotb"}], "top": "lib", "cocotb": False},
    )
    assert alone.tb.cocotb is None


def test_a_cocotb_source_from_a_dependency_does_not_switch_cocotb_on(tmp_path):
    root = tmp_path / "d"
    _dependency(root, tb=True)
    design = merged_design(
        Design(
            name="d",
            root=root,
            rtl={"sources": ["lib.v"], "top": "lib"},
            dependencies=[{"uri": "dep/dep.yaml", "rtl": {"pos": -1}}],
        ),
        run_root=None,
    )
    assert [source.file.name for source in design.tb.sources] == ["cocotb_tests.py"]
    assert design.tb.cocotb is None


def test_the_class_of_a_generator_is_case_sensitive():
    with pytest.raises(ValueError, match="unknown generator class: Chisel"):
        Rtl(sources=(), generator={"class": "Chisel", "project": "p"})
