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
from xeda.frozen import thaw

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


def test_a_design_cannot_be_built_without_a_root(tmp_path):
    """`model_construct` and `model_validate` skip the constructor, so they would give a design
    with no root: they are refused, and say what to call."""
    keys = {"name": "a", "rtl": {"sources": []}}
    for build in (Design.model_construct, Design.model_validate):
        with pytest.raises(TypeError, match=r"Design\(\.\.\.\) or Design\.load"):
            build(keys)
    with pytest.raises(TypeError, match=r"Design\(\.\.\.\) or Design\.load"):
        Design.model_validate_json('{"name": "a", "rtl": {"sources": []}}')
    design = Design(root=tmp_path, **keys)  # type: ignore[arg-type]
    assert design.model_copy(update={"description": "d"}).root == tmp_path.resolve()


def _cocotb_design(root: Path, **tb: object) -> Design:
    (root / "test_a.py").write_text("")
    return Design(
        name="a",
        root=root,
        rtl={"sources": [], "top": "a"},
        tb={"sources": [{"file": "test_a.py", "type": "Cocotb"}], "top": "a", **tb},
    )


def test_an_explicit_cocotb_refusal_survives_an_update_and_a_reload(tmp_path):
    from xeda.flow_runner.configuration import design_configuration

    refused = _cocotb_design(tmp_path, cocotb=False)
    derived = _cocotb_design(tmp_path)
    assert refused.tb.cocotb is None and derived.tb.cocotb is not None
    for design in (refused, derived):
        for same in (
            design.with_updates({}),
            design.with_updates({"rtl.top": "a"}),
            Design(root=design.root, **design.to_dict()),
        ):
            assert same == design
            assert same.tb.cocotb == design.tb.cocotb
            assert design_configuration(same) == design_configuration(design)
    assert refused.to_dict()["tb"]["cocotb"] is False
    assert isinstance(derived.to_dict()["tb"]["cocotb"], dict)


def test_a_new_cocotb_source_after_an_update_still_turns_cocotb_on(tmp_path):
    (tmp_path / "test_b.py").write_text("")
    plain = Design(name="a", root=tmp_path, rtl={"sources": []}, tb={"sources": [], "top": "a"})
    assert plain.tb.cocotb is None
    changed = plain.with_updates({"tb.sources": [{"file": "test_b.py", "type": "Cocotb"}]})
    assert changed.tb.cocotb is not None


PROPERTY = "STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM"


def _property_design(root: Path, **kwargs: object) -> Design:
    """A design whose base and target both set a property whose name contains dots."""
    return Design(
        name="a",
        root=root,
        rtl={"sources": [], "top": "a"},
        flows={"vivado_synth": {"set_synth_properties": {PROPERTY: 0, "OTHER": 1}}},
        targets={"t": {"flows": {"vivado_synth": {"set_synth_properties": {PROPERTY: 2}}}}},
        **kwargs,  # type: ignore[arg-type]
    )


def _properties(design: Design) -> dict[str, object]:
    """The property table as the flow reads it."""
    from xeda.flows import VivadoSynth

    settings = VivadoSynth.Settings.from_input(thaw(design.flows["vivado_synth"]))
    return dict(settings.set_synth_properties)


def test_a_merged_property_name_with_dots_survives_an_update(tmp_path):
    design = _property_design(tmp_path, target="t")
    assert _properties(design) == {PROPERTY: 2, "OTHER": 1}
    for changed in (
        design.with_updates({"rtl.top": "b"}),
        design.with_updates({}),
        design.with_updates({"flows": {"vivado_synth": {"set_synth_properties": {"NEW": 3}}}}),
    ):
        assert _properties(changed)[PROPERTY] == 2
        assert changed.target == "t"
    assert (
        _properties(
            design.with_updates({"flows.vivado_synth.set_synth_properties": {PROPERTY: 5}})
        )[PROPERTY]
        == 5
    )
    assert design.with_updates({}) == design


def test_to_dict_names_a_merged_property_as_a_design_file_does(tmp_path):
    design = _property_design(tmp_path, target="t")
    document = design.to_dict()
    table = document["flows"]["vivado_synth"]["set_synth_properties"]
    assert set(table) == {PROPERTY, "OTHER"}, "plain dotted names, no escaped dot"
    again = Design(root=design.root, **document)
    assert _properties(again) == _properties(design)
    assert again == Design(root=design.root, **design.to_dict())
    # the written document, read as a design file
    import json

    file = tmp_path / "written.json"
    file.write_text(json.dumps(document))
    assert _properties(Design.load(file)) == _properties(design)


def test_the_package_of_a_remote_run_keeps_a_name_and_a_name_inside_it_apart(tmp_path):
    """`A` and `A.B` can both be names of a merged table. A design file cannot write that pair,
    so the package writes the escaped text and the host reads it back as the merged names."""
    import json

    from xeda.dataclass import AtomicName

    design = Design(
        name="a",
        root=tmp_path,
        rtl={"sources": [], "top": "a"},
        flows={"vivado_synth": {"set_synth_properties": {"A": 1}}},
        targets={"t": {"flows": {"vivado_synth": {"set_synth_properties": {"A.B": 2}}}}},
        target="t",
    )
    file = tmp_path / "a.xeda.json"
    document = design._document("json", plain_names=False)
    file.write_text(json.dumps({**document, "target": "t"}))
    table = Design._load_file(file, packaged=True).flows["vivado_synth"]["set_synth_properties"]
    assert {(k.name if isinstance(k, AtomicName) else k): v for k, v in table.items()} == {
        "A": 1,
        "A.B": 2,
    }


def _pattern_design(root: Path) -> Design:
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "a.v").write_text("module a; endmodule\n")
    (root / "tb.sv").write_text("module tb; endmodule\n")
    return Design(
        name="a",
        root=root,
        rtl={"sources": ["src/*.v"], "top": "a", "parameters": {"W": 1}},
        tb={"sources": ["tb.sv"], "top": "tb"},
    )


def test_an_update_keeps_the_entries_that_named_the_sources(tmp_path):
    design = _pattern_design(tmp_path)
    entries = design.source_entries()
    assert [entry.text for entry in entries] == ["src/*.v", "tb.sv"]
    for changed in (
        design.with_updates({}),
        design.with_updates({"rtl.parameters.W": 2}),
        design.with_updates({"tb": {"top": "other"}}).with_updates({"rtl.top": "b"}),
    ):
        assert changed.source_entries() == entries
        assert changed.rtl.sources == design.rtl.sources
    replaced = design.with_updates({"rtl.sources": ["tb.sv"]})
    assert [entry.text for entry in replaced.source_entries()] == ["tb.sv", "tb.sv"]
    assert [s.path.name for s in replaced.rtl.sources] == ["tb.sv"]


def _sources_of(design: Design) -> list[str]:
    return [str(s.path.relative_to(design.root)) for s in design.rtl.sources]


def test_with_target_expands_nothing_again(tmp_path):
    for name in ("src/a.sv", "board/b.sv", "board/c.sv"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("module m; endmodule\n")
    file = tmp_path / "a.yaml"
    file.write_text(dedent("""\
            name: a
            rtl:
              sources: [src/*.sv]
              top: a
            targets:
              t:
                rtl:
                  sources: [board/*.sv]
              u:
                rtl:
                  sources: [board/b.sv]
            """))
    design = Design.load(file)
    # a file that matches is added, another is removed, after the design is loaded
    (tmp_path / "src" / "new.sv").write_text("module n; endmodule\n")
    (tmp_path / "board" / "new.sv").write_text("module n; endmodule\n")
    (tmp_path / "src" / "a.sv").unlink()
    (tmp_path / "board" / "c.sv").unlink()
    chosen = design.with_target("t")
    assert _sources_of(chosen) == ["src/a.sv", "board/b.sv", "board/c.sv"]
    assert _sources_of(chosen.with_target("u")) == ["src/a.sv", "board/b.sv"]
    assert _sources_of(chosen.with_target(None)) == ["src/a.sv"]
    assert [e.text for e in chosen.source_entries()] == ["src/*.sv", "board/*.sv"]
    # the same design loaded again sees the files as they are now
    assert _sources_of(Design.load(file, target="t")) == [
        "src/new.sv",
        "board/b.sv",
        "board/new.sv",
    ]
    # an update after the target keeps the sources of the target
    assert _sources_of(chosen.with_updates({"rtl.top": "b"})) == _sources_of(chosen)


def test_a_target_whose_files_are_missing_is_checked_when_it_is_selected(tmp_path):
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    design = Design(
        name="a",
        root=tmp_path,
        rtl={"sources": ["a.sv"], "top": "a"},
        targets={"t": {"rtl": {"sources": ["board/*.sv"]}}},
    )
    assert design.targets == ("t",)
    with pytest.raises(DesignValidationError, match=r"board/\*\.sv"):
        design.with_target("t")
    (tmp_path / "board").mkdir()
    (tmp_path / "board" / "b.sv").write_text("module b; endmodule\n")
    assert _sources_of(design.with_target("t")) == ["a.sv", "board/b.sv"]


def test_a_target_that_drops_the_generator_has_handwritten_sources(tmp_path):
    """A target that writes `generator: null` has no generator: its sources are files, though the
    design's generator would write every entry of its own list."""
    (tmp_path / "hand.v").write_text("module hand; endmodule\n")
    design = Design(
        name="a",
        root=tmp_path,
        rtl={"sources": ["gen/out.v"], "generator": {"shell": "true"}},
        targets={"t": {"rtl": {"generator": None, "sources": ["hand.v"]}}},
    )
    assert [s.generated for s in design.rtl.sources] == [True]
    chosen = design.with_target("t")
    hand = [s for s in chosen.rtl.sources if s.output_name != "gen/out.v"]
    assert [s.path.name for s in hand] == ["hand.v"] and not hand[0].generated
    assert hand[0].path == (tmp_path / "hand.v").resolve()
    # a target that says nothing of the generator still has the design's
    (tmp_path / "gen").mkdir()
    other = Design(
        name="a",
        root=tmp_path,
        rtl={"sources": ["gen/out.v"], "generator": {"shell": "true"}},
        targets={"t": {"rtl": {"sources": ["gen/more.v"]}}},
    ).with_target("t")
    assert [s.generated for s in other.rtl.sources] == [True, True]
