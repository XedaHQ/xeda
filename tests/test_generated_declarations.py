"""A design lists the sources its generator writes as declarations, and loading checks them.

An entry of `rtl.sources` is wholly the generator's or wholly written by hand. A generated entry is
a name or a pattern in the output of the generator: it is not expanded, it need not exist, and it
takes its type from its extension, or from the `type` that it states.
"""

import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError, SourceType


def _design(root: Path, sources, generated_sources=None, **recipe) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gen.py").write_text("")
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "lib").mkdir(exist_ok=True)
    (root / "lib" / "a.v").write_text("module a; endmodule\n")
    (root / "lib" / "b.v").write_text("module b; endmodule\n")
    generator = {"executable": sys.executable, "args": ["gen.py"], "sources": ["gen.py"], **recipe}
    if generated_sources is not None:
        generator["generated_sources"] = generated_sources
    return Design(
        name="d", design_root=root, rtl={"sources": sources, "top": "top", "generator": generator}
    )


def _described(design: Design) -> list[tuple[str, str, bool]]:
    return [(str(s.path), s.type.name, s.generated) for s in design.rtl.sources]


def test_every_entry_is_the_generators_when_generated_sources_is_empty(tmp_path):
    design = _design(tmp_path, ["gen/top.v", "gen/*.sv", {"path": "x/y", "type": "Data"}])
    assert _described(design) == [
        ("gen/top.v", "Verilog", True),
        ("gen/*.sv", "SystemVerilog", True),
        ("x/y", "Data", True),
    ]
    assert not any(source.located for source in design.rtl.sources)


def test_a_declaration_need_not_exist_and_is_not_expanded(tmp_path):
    design = _design(tmp_path, ["gen/*.v"])
    assert [source.output_name for source in design.rtl.sources] == ["gen/*.v"]
    assert not (tmp_path / "gen").exists()


def test_generated_sources_names_the_entries_the_generator_writes(tmp_path):
    design = _design(tmp_path, ["top.v", "gen/a.v", "gen/b.v", "lib/*.v"], ["gen"])
    assert [s.generated for s in design.rtl.sources] == [False, True, True, False, False]
    written = [s for s in design.rtl.sources if not s.generated]
    assert [s.path.name for s in written] == ["top.v", "a.v", "b.v"], "written entries expand"
    assert all(s.located for s in written)


def test_generated_sources_may_name_a_file_or_a_pattern(tmp_path):
    design = _design(tmp_path, ["top.v", "lib/a.v", "gen/b.v"], ["gen/b.v"])
    assert [s.generated for s in design.rtl.sources] == [False, False, True]
    design = _design(tmp_path / "other", ["top.v", "lib/a.v", "gen/b.v", "gen/c.v"], ["gen/*.v"])
    assert [s.generated for s in design.rtl.sources] == [False, False, True, True]


@pytest.mark.parametrize(
    "pattern, suffix",
    [
        ("gen/*.v", "Verilog"),
        ("gen/*.sv", "SystemVerilog"),
        ("gen/*.vhd", "Vhdl"),
        ("*.edf", "Edif"),
    ],
)
def test_a_pattern_takes_its_type_from_its_extension(tmp_path, pattern, suffix):
    assert _described(_design(tmp_path, [pattern])) == [(pattern, suffix, True)]


def test_a_stated_type_wins_and_a_pattern_with_no_definite_type_needs_one(tmp_path):
    design = _design(
        tmp_path,
        [{"path": "gen/*", "type": "Verilog"}, {"file": "gen/x.v", "type": "SystemVerilog"}],
    )
    assert [(str(s.path), s.type.name) for s in design.rtl.sources] == [
        ("gen/*", "Verilog"),
        ("gen/x.v", "SystemVerilog"),
    ]
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path / "bare", ["gen/*"])
    text = str(error.value)
    assert "`gen/*`" in text and "no definite type" in text
    assert "{path: gen/*, type: Verilog}" in text


def test_an_ambiguous_suffix_needs_a_type_too(tmp_path):
    with pytest.raises(DesignValidationError, match=r"`gen/\*\.json`.*type"):
        _design(tmp_path, ["gen/*.json"])


def test_a_pattern_that_matches_both_kinds_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match=r"`lib/\*\.v` is partly generated"):
        _design(tmp_path, ["lib/*.v"], ["lib/b.v"])


def test_a_generated_sources_entry_that_no_source_relates_to_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match="not among `rtl.sources`: other"):
        _design(tmp_path, ["gen/top.v", "top.v"], ["gen", "other"])


@pytest.mark.parametrize("entry", ["../gen/top.v", "/abs/gen/top.v"])
def test_a_generated_name_stays_inside_the_output(tmp_path, entry):
    with pytest.raises(DesignValidationError, match="name in the output of the generator"):
        _design(tmp_path, [entry])


def test_the_design_root_spelling_of_a_generated_name_is_the_name(tmp_path):
    design = _design(tmp_path, ["$DESIGN_ROOT/gen/top.v"])
    assert _described(design) == [("gen/top.v", "Verilog", True)]


def test_a_dump_of_the_design_loads_again_to_the_same_declarations(tmp_path):
    design = _design(tmp_path, ["top.v", "gen/*.v", {"path": "gen/odd", "type": "Data"}], ["gen"])
    dumped = design.model_dump(mode="json")
    assert dumped["rtl"]["sources"][1:] == ["gen/*.v", {"path": "gen/odd", "type": "Data"}]
    again = Design(design_root=tmp_path, **{k: v for k, v in dumped.items() if k != "design_root"})
    assert _described(again) == _described(design)
    assert again.rtl.sources == design.rtl.sources


def test_the_environment_of_a_generator_may_not_set_the_variables_xeda_sets(tmp_path):
    with pytest.raises(DesignValidationError, match="XEDA_GENERATED"):
        _design(tmp_path, ["gen/top.v"], env={"XEDA_GENERATED": "/x"})


def test_a_dependency_with_a_generator_is_refused(tmp_path):
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / "gen.py").write_text("")
    (dep / "dep.yaml").write_text(
        f"name: dep\nrtl:\n  sources: [gen/a.v]\n  top: a\n  generator:\n"
        f"    executable: {sys.executable!r}\n    args: [gen.py]\n    sources: [gen.py]\n"
    )
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    with pytest.raises(DesignValidationError, match="has a generator"):
        Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": ["top.v"], "top": "top"},
            dependencies=["dep/dep.yaml"],
        )


def test_generated_sources_keep_their_place_in_the_order(tmp_path):
    design = _design(tmp_path, ["gen/a.v", "gen/*.sv", "gen/b.v"])
    assert [s.output_name for s in design.rtl.sources] == ["gen/a.v", "gen/*.sv", "gen/b.v"]
    assert design.rtl.sources[1].type is SourceType.SystemVerilog


def test_a_flow_that_takes_other_types_hashes_a_design_with_a_declaration(tmp_path):
    """A declaration has no content, so the whole design has no content hash. A flow that takes
    only some types is judged on those sources alone, and a generated source of another type is
    no obstacle."""
    root = tmp_path / "d"
    root.mkdir()
    (root / "gen.py").write_text("")
    (root / "a.vhd").write_text("entity a is end;\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["a.vhd", "gen/top.v"],
            "top": "a",
            "generator": {
                "executable": sys.executable,
                "sources": ["gen.py"],
                "generated_sources": ["gen"],
            },
        },
    )
    assert design.parts_hash({"rtl"}, {SourceType.Vhdl})
    with pytest.raises(FileNotFoundError, match="generator has not run"):
        design.parts_hash({"rtl"})
