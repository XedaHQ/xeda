"""The ways to build and change a design: `Design.load`, `Design(...)`, `with_updates`,
`with_target` and `to_dict`."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import click
import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import DesignValidationError

DESIGN_FILE = """\
name: a
rtl:
  sources: [src/*.sv]
  top: a
  defines:
    DUMP_VCD: null
  parameters:
    W: 8
tb:
  top: tb
  parameters:
    G: 1
targets:
  ecp5:
    rtl:
      parameters:
        W: 4
  artix7:
    rtl:
      sources: [board/a7.sv]
      top: a7
"""


def _tree(tmp_path: Path, text: str = DESIGN_FILE) -> Path:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "a.sv").write_text("module a; endmodule\n")
    (tmp_path / "board").mkdir(exist_ok=True)
    (tmp_path / "board" / "a7.sv").write_text("module a7; endmodule\n")
    file = tmp_path / "a.yaml"
    file.write_text(text)
    return file


def test_the_constructor_is_a_file_in_root(tmp_path):
    _tree(tmp_path)
    built = Design(
        name="a",
        root=tmp_path,
        rtl={"sources": ["src/*.sv"], "top": "a", "defines": {"DUMP_VCD": None}},
    )
    (tmp_path / "plain.yaml").write_text(
        "name: a\nrtl:\n  sources: [src/*.sv]\n  top: a\n  defines:\n    DUMP_VCD: null\n"
    )
    assert built == Design.load(tmp_path / "plain.yaml")
    assert built.rtl.defines == {"DUMP_VCD": None}
    assert [s.path for s in built.rtl.sources] == [(tmp_path / "src" / "a.sv").resolve()]


def test_dotted_keys_expand_in_the_constructor(tmp_path):
    _tree(tmp_path)
    built = Design(name="a", root=tmp_path, rtl={"sources": [], "top": "a", "parameters.W": 3})
    assert built.rtl.parameters["W"] == 3


def test_the_signatures_are_the_contract():
    parameters = inspect.signature(Design.__init__).parameters
    assert list(parameters) == [
        "self",
        "name",
        "root",
        "rtl",
        "tb",
        "flows",
        "targets",
        "target",
        "language",
        "dependencies",
        "description",
        "authors",
        "license",
        "version",
        "url",
    ]
    assert all(p.kind is p.KEYWORD_ONLY for n, p in parameters.items() if n != "self")
    assert parameters["root"].default is inspect.Parameter.empty
    load = inspect.signature(Design.load).parameters
    assert list(load) == ["path", "target"] and load["target"].kind is load["target"].KEYWORD_ONLY
    assert list(inspect.signature(Design.with_updates).parameters) == ["self", "updates"]
    assert list(inspect.signature(Design.with_target).parameters) == ["self", "target"]
    assert list(inspect.signature(Design.to_dict).parameters) == ["self"]


def test_root_is_required_and_unknown_keywords_are_refused(tmp_path):
    with pytest.raises(TypeError):
        Design(name="a", rtl={"sources": [], "top": "a"})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Design(
            name="a", root=tmp_path, rtl={"sources": [], "top": "a"}, **{"design_root": tmp_path}
        )
    with pytest.raises(TypeError):
        Design(name="a", root=tmp_path, rtl={"sources": [], "top": "a"}, nonsense=1)  # type: ignore[call-arg]


def test_an_empty_root_is_refused_and_not_the_current_directory():
    with pytest.raises(DesignValidationError, match="not a path"):
        Design(name="a", root="", rtl={"sources": []})


def test_root_must_be_a_directory(tmp_path):
    with pytest.raises(DesignValidationError, match="directory does not exist"):
        Design(name="a", root=tmp_path / "missing", rtl={"sources": []})


def test_load_takes_the_directory_of_the_file_as_root(tmp_path):
    file = _tree(tmp_path)
    design = Design.load(file)
    assert design.root == tmp_path.resolve() and design.root.is_absolute()
    assert design.name == "a" and design.target is None
    assert design.targets == ("ecp5", "artix7")


def test_load_selects_a_target(tmp_path):
    design = Design.load(_tree(tmp_path), target="ecp5")
    assert design.target == "ecp5" and design.rtl.parameters["W"] == 4
    assert design.targets == ("ecp5", "artix7")
    with pytest.raises(DesignValidationError, match="not a target"):
        Design.load(tmp_path / "a.yaml", target="nope")


def test_a_design_file_cannot_give_the_root(tmp_path):
    file = _tree(tmp_path, DESIGN_FILE + "design_root: ../elsewhere\n")
    with pytest.raises(DesignValidationError, match="`design_root` was removed"):
        Design.load(file)


def test_the_constructor_selects_a_target_among_its_targets(tmp_path):
    _tree(tmp_path)
    kwargs = dict(
        name="a",
        root=tmp_path,
        rtl={"sources": ["src/*.sv"], "top": "a"},
        targets={"t1": {"rtl": {"top": "b"}}, "t2": {"rtl": {"top": "c"}}},
    )
    assert Design(**kwargs).target is None  # type: ignore[arg-type]
    assert Design(**kwargs).targets == ("t1", "t2")  # type: ignore[arg-type]
    assert Design(**kwargs, target="t2").rtl.top == "c"  # type: ignore[arg-type]
    with pytest.raises(DesignValidationError):
        Design(name="a", root=tmp_path, rtl={"sources": [], "top": "a"}, target="t1")


def test_resolution_happens_once_in_another_cwd(tmp_path):
    file = _tree(tmp_path / "d")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    script = dedent(f"""
        import os
        from pathlib import Path
        from xeda import Design
        design = Design.load("../d/a.yaml")
        assert design.root.is_absolute() and design.root == Path({str(file.parent.resolve())!r})
        os.chdir({str(tmp_path)!r})
        assert design.root == Path({str(file.parent.resolve())!r})
        assert design.with_updates({{"rtl.top": "x"}}).root == design.root
        built = Design(name="b", root="d", rtl={{"sources": ["src/*.sv"]}})
        assert built.root == Path({str(file.parent.resolve())!r})
        """)
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=elsewhere, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


def test_with_updates_merges_and_replaces(tmp_path):
    design = Design.load(_tree(tmp_path))
    changed = design.with_updates({"rtl.parameters.W": 16, "tb.top": "other"})
    assert changed is not design
    assert changed.rtl.parameters["W"] == 16 and changed.tb.top == ("other",)
    assert design.rtl.parameters["W"] == 8  # the first design is unchanged
    replaced = design.with_updates({"rtl.sources": ["board/a7.sv"]})
    assert [s.path.name for s in replaced.rtl.sources] == ["a7.sv"]
    assert design.with_updates({"rtl": {"parameters": {"X": 1}}}).rtl.parameters == {"W": 8, "X": 1}


def test_with_updates_keeps_the_order_of_updates_and_equality_ignores_them(tmp_path):
    design = Design.load(_tree(tmp_path))
    first, second = {"rtl.parameters.W": 1}, {"tb.parameters": {"G": 2}}
    changed = design.with_updates(first).with_updates(second)
    assert design.updates == ()
    assert changed.updates == (first, second)
    assert changed.updates[0] == first
    same = design.with_updates({"rtl.parameters.W": 1, "tb.parameters.G": 2})
    assert same.updates == ({"rtl.parameters.W": 1, "tb.parameters.G": 2},) and same == changed


def test_with_updates_rejects_what_cannot_be_updated(tmp_path):
    design = Design.load(_tree(tmp_path), target="ecp5")
    with pytest.raises(DesignValidationError, match="target"):
        design.with_updates({"target": "artix7"})
    with pytest.raises(DesignValidationError, match="nonsense"):
        design.with_updates({"nonsense": 1})
    with pytest.raises(DesignValidationError, match="design_root"):
        design.with_updates({"design_root": "x"})
    with pytest.raises(DesignValidationError):
        design.with_updates({"root": "x"})


def test_with_target_reapplies_the_updates_after_the_target(tmp_path):
    f = _tree(tmp_path)
    d = Design.load(f, target="ecp5").with_updates({"rtl.parameters.W": 16})
    assert d.rtl.parameters["W"] == 16
    e = d.with_target("artix7")
    assert e.target == "artix7" and e.rtl.parameters["W"] == 16
    assert e.rtl.top == "a7" and e.updates == d.updates
    assert d.target == "ecp5"  # the first design is unchanged


def test_with_target_none_selects_no_target_and_keeps_the_targets(tmp_path):
    d = Design.load(_tree(tmp_path), target="ecp5")
    e = d.with_target(None)
    assert e.target is None and e.targets == ("ecp5", "artix7") and e.rtl.parameters["W"] == 8
    with pytest.raises(DesignValidationError, match="not a target"):
        d.with_target("nope")


def test_with_updates_after_with_target_equals_load_with_updates(tmp_path):
    f = _tree(tmp_path)
    a = Design.load(f).with_target("artix7").with_updates({"rtl.parameters.W": 2})
    b = Design.load(f, target="artix7").with_updates({"rtl.parameters.W": 2})
    assert a == b and a.target == "artix7"


def test_with_updates_does_not_pick_up_files_added_later(tmp_path):
    d = Design.load(_tree(tmp_path))
    (tmp_path / "src" / "b.sv").write_text("module b; endmodule\n")
    changed = d.with_updates({"rtl.top": "a"})
    assert [s.path.name for s in changed.rtl.sources] == ["a.sv"]


def test_to_dict_has_no_root_target_targets_or_updates(tmp_path):
    d = Design.load(_tree(tmp_path), target="ecp5").with_updates({"rtl.parameters.W": 5})
    document = d.to_dict()
    assert isinstance(document, dict)
    assert not {"root", "target", "targets", "updates", "design_root"} & set(document)
    assert document["rtl"]["parameters"] == {"W": 5}
    assert document["name"] == "a"


def test_to_dict_rebuilds_the_configuration_with_no_target(tmp_path):
    d = Design.load(_tree(tmp_path), target="artix7").with_updates({"rtl.parameters.W": 5})
    again = Design(root=d.root, **d.to_dict())
    assert again.to_dict() == d.to_dict()
    assert again.target is None and again.targets == () and again.updates == ()
    assert again.root == d.root and again.rtl.sources == d.rtl.sources


def test_equality_is_to_dict_root_target_and_targets(tmp_path):
    f = _tree(tmp_path)
    assert Design.load(f) == Design.load(f)
    assert Design.load(f) != Design.load(f, target="ecp5")
    assert Design.load(f).with_target("ecp5") == Design.load(f, target="ecp5")
    other = tmp_path / "other"
    other.mkdir()
    copy = Design(root=other, **Design.load(f).to_dict())
    assert copy != Design.load(f)  # another root
    assert Design.load(f).with_updates({"rtl.top": "a"}) == Design.load(f)


def test_fields_of_the_contract(tmp_path):
    d = Design.load(
        _tree(
            tmp_path,
            DESIGN_FILE
            + "description: d\nauthors: ['A <a@x.org>']\nversion: '1'\nurl: u\nlicense: MIT\n"
            + "flows:\n  ghdl_sim:\n    werror: true\n",
        ),
        target="ecp5",
    )
    assert d.name == "a" and isinstance(d.root, Path) and d.target == "ecp5"
    assert d.targets == ("ecp5", "artix7") and d.updates == ()
    assert d.flows["ghdl_sim"]["werror"] is True
    assert d.language is not None and d.dependencies == ()
    assert (d.description, d.authors, d.version, d.url, d.license) == (
        "d",
        ("A <a@x.org>",),
        "1",
        "u",
        "MIT",
    )
    assert d.rtl.top == "a" and d.tb is not None and d.tb.top == ("tb",)
    for name in ("root", "targets", "updates", "name", "rtl"):
        with pytest.raises((AttributeError, TypeError, ValueError)):
            setattr(d, name, None)


def test_a_design_survives_copy_and_pickle(tmp_path):
    import copy
    import pickle

    d = Design.load(_tree(tmp_path), target="ecp5").with_updates({"rtl.parameters.W": 6})
    for other in (copy.deepcopy(d), pickle.loads(pickle.dumps(d)), d.model_copy(deep=True)):
        assert other == d and other.updates == d.updates and other.root == d.root
        assert other.with_target("artix7").target == "artix7"


def test_the_old_option_names_its_successor(tmp_path):
    file = _tree(tmp_path)
    result = CliRunner().invoke(
        cli, ["run", "--dry-run", "ghdl_sim", str(file), "--design-overrides", "rtl.top=x"]
    )
    assert result.exit_code != 0
    output = click.unstyle(result.output)
    assert "`--design-overrides` was removed" in output and "--design-updates" in output


def test_with_updates_equals_the_design_updates_option(tmp_path):
    from xeda.flow_runner import DefaultRunner

    file = _tree(tmp_path)
    updates = {"tb.top": "other", "rtl.parameters.W": 2}
    for target in (None, "ecp5"):
        request = DefaultRunner(tmp_path / "run")._request(
            "ghdl_sim", str(file), design_updates=updates, target=target
        )
        expected = Design.load(file, target=target).with_updates(updates)
        assert request.design == expected
        assert request.design.updates == (updates,)
        assert request.design.rtl.parameters["W"] == 2
        assert request.design.tb.top == ("other",)
