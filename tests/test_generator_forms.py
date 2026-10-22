"""A generator is written as a command line, a list of arguments or a table, and the design keeps
a table.

A dependency is merged into the design without a second look at what the first validation
derived (the cocotb settings of the testbench).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pydantic
import pytest

from xeda import Design
from xeda.design import DesignValidationError, Rtl, generator_of
from xeda.utils import NonZeroExitCode


def _load(root: Path, generator: object) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    return Design(
        name="d",
        design_root=root,
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
        design = Design.from_file(file)
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


def test_the_model_of_a_shell_recipe_runs_the_text_in_a_shell(tmp_path):
    design = _load(
        tmp_path,
        "printf 'module top; endmodule\\n' > made.v && printf '%s' \"$DESIGN_ROOT\" > root.txt",
    )
    root = design.root_path
    assert (root / "made.v").read_text() == "module top; endmodule\n"
    assert (root / "root.txt").read_text() == str(root)
    generator = generator_of(design.rtl, root=root)
    assert generator is not None and generator.shell is not None
    assert generator.describe() == f"the generator ({generator.shell})"


def test_an_argument_list_runs_without_a_shell(tmp_path):
    code = "import pathlib, sys; pathlib.Path('made.v').write_text(sys.argv[1])"
    design = _load(tmp_path, [sys.executable, "-c", code, "module a; endmodule && echo x > no.txt"])
    root = design.root_path
    assert (root / "made.v").read_text() == "module a; endmodule && echo x > no.txt"
    assert not (root / "no.txt").exists()


def test_a_failing_shell_recipe_fails_the_load(tmp_path):
    with pytest.raises(NonZeroExitCode):
        _load(tmp_path, "exit 3")


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
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["lib.v"], "top": "lib"},
        tb={"sources": [{"file": "my_tests.py", "type": "Cocotb"}], "top": "lib", "cocotb": False},
        dependencies=[{"uri": "dep/dep.yaml", "rtl": {"pos": -1}}],
    )
    assert design.tb.cocotb is None and design.dependencies == ()
    alone = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["lib.v"], "top": "lib"},
        tb={"sources": [{"file": "my_tests.py", "type": "Cocotb"}], "top": "lib", "cocotb": False},
    )
    assert alone.tb.cocotb is None


def test_a_cocotb_source_from_a_dependency_does_not_switch_cocotb_on(tmp_path):
    root = tmp_path / "d"
    _dependency(root, tb=True)
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["lib.v"], "top": "lib"},
        dependencies=[{"uri": "dep/dep.yaml", "rtl": {"pos": -1}}],
    )
    assert [source.file.name for source in design.tb.sources] == ["cocotb_tests.py"]
    assert design.tb.cocotb is None
