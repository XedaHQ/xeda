"""A design lists the sources its generator writes as declarations, and loading checks them.

An entry of `rtl.sources` is wholly the generator's or wholly written by hand. A generated entry is
a name or a pattern in the output of the generator: it is not expanded, it need not exist, and it
takes its type from its extension, or from the `type` that it states.
"""

import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import (
    STORAGE_PREFIX,
    TREE_DIRECTORY,
    DesignValidationError,
    SourceEntry,
    SourceType,
    generator_of,
    storage_name,
    tree_declarations,
)
from xeda.flow_runner.configuration import design_configuration_hash
from xeda.flow_runner.dependencies import merged_design
from xeda.flow_runner.launch_generation import generation_key


def _design(root: Path, sources, generated_sources=None, *, inputs=None, **recipe) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gen.py").write_text("")
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "lib").mkdir(exist_ok=True)
    (root / "lib" / "a.v").write_text("module a; endmodule\n")
    (root / "lib" / "b.v").write_text("module b; endmodule\n")
    generator = {
        "executable": sys.executable,
        "args": ["gen.py"],
        "sources": inputs or ["gen.py"],
        **recipe,
    }
    if generated_sources is not None:
        generator["generated_sources"] = generated_sources
    return Design(
        name="d", root=root, rtl={"sources": sources, "top": "top", "generator": generator}
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
        [{"path": "gen/*", "type": "Verilog"}, {"path": "gen/x.v", "type": "SystemVerilog"}],
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
def test_a_generated_entry_stays_below_the_design_root(tmp_path, entry):
    with pytest.raises(DesignValidationError, match="path below the design root"):
        _design(tmp_path, [entry])


def test_the_design_root_spelling_of_a_generated_name_is_the_name(tmp_path):
    design = _design(tmp_path, ["$DESIGN_ROOT/gen/top.v", str(tmp_path / "gen" / "b.v")])
    assert _described(design) == [("gen/top.v", "Verilog", True), ("gen/b.v", "Verilog", True)]


def test_a_dump_of_the_design_loads_again_to_the_same_declarations(tmp_path):
    design = _design(tmp_path, ["top.v", "gen/*.v", {"path": "gen/odd", "type": "Data"}], ["gen"])
    dumped = design.to_dict()
    assert dumped["rtl"]["sources"][1:] == ["gen/*.v", {"path": "gen/odd", "type": "Data"}]
    again = Design(root=tmp_path, **dumped)
    assert _described(again) == _described(design)
    assert again.rtl.sources == design.rtl.sources


def test_the_environment_of_a_generator_may_not_set_the_variables_xeda_sets(tmp_path):
    with pytest.raises(DesignValidationError, match="XEDA_GENERATED"):
        _design(tmp_path, ["gen/top.v"], env={"XEDA_GENERATED": "/x"})


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
        root=root,
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


def test_a_file_table_is_a_written_source_even_when_every_entry_is_generated(tmp_path):
    """A `file` must exist when the design loads, so it can never be a declaration. With an empty
    `generated_sources` the other entries are the generator's, and this one stays written."""
    design = _design(tmp_path, [{"file": "top.v"}, "gen/a.v", {"path": "gen/b.v"}])
    assert [(s.generated, s.located) for s in design.rtl.sources] == [
        (False, True),
        (True, False),
        (True, False),
    ]


def test_a_file_table_that_generated_sources_owns_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match=r"`lib/a.v`.*must exist.*path"):
        _design(tmp_path, [{"file": "lib/a.v"}, "gen/a.v"], ["lib", "gen"])


def test_a_written_entry_of_a_generator_design_gets_a_message_that_names_generated_sources(
    tmp_path,
):
    from xeda.generation import MissingGenerated

    message = str(MissingGenerated("lib/a.v", "the generator of design 'd' (gen.py)"))
    assert "`generated_sources`" in message and "lib/a.v" in message
    with pytest.raises(DesignValidationError, match="generated_sources"):
        _design(tmp_path, ["/abs/outside.v"])


@pytest.mark.parametrize("cwd", ["/tmp", "/", "..", "../elsewhere", "build/../..", "a/../../b"])
def test_a_generator_cwd_outside_the_design_root_is_refused_when_the_design_loads(tmp_path, cwd):
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path, ["gen/top.v"], cwd=cwd)
    message = str(error.value)
    assert "cwd" in message and "below the design root" in message and cwd in message


@pytest.mark.parametrize("cwd", [".", "build", "build/work", "a/../b", "./build"])
def test_a_generator_cwd_below_the_design_root_is_accepted(tmp_path, cwd):
    design = _design(tmp_path, ["gen/top.v"], cwd=cwd)
    assert design.rtl.generator is not None


def test_a_generator_starts_in_its_cwd_as_the_check_reads_it(tmp_path):
    """`build/../work` is `work`: the generator starts there, also when `build` does not exist
    (on the remote host, only `work` is made)."""
    design = _design(tmp_path, ["gen/top.v"], cwd="build/../work")
    generator = generator_of(design.rtl, root=design.root)
    assert generator is not None
    assert generator.lookup_directory(tmp_path) == tmp_path / "work"


def test_a_dependency_generator_cwd_is_judged_against_the_root_of_the_dependency(tmp_path):
    dep = tmp_path / "app" / "dep"
    dep.mkdir(parents=True)
    (dep / "gen.py").write_text("")
    (dep / "dep.yaml").write_text(
        "name: dep\nrtl:\n  sources: [gen/a.v]\n  top: a\n  generator:\n"
        f"    executable: {sys.executable!r}\n    args: [gen.py]\n    sources: [gen.py]\n"
        "    cwd: ..\n"
    )
    (tmp_path / "app" / "top.v").write_text("module top; endmodule\n")
    design = Design(
        name="app",
        root=tmp_path / "app",
        rtl={"sources": ["top.v"], "top": "top"},
        dependencies=["dep/dep.yaml"],
    )
    # planning reads the dependency, and the design of the dependency is refused as it loads
    with pytest.raises(DesignValidationError, match="cwd.*below the design root"):
        merged_design(design, run_root=None)


# ---------------------------------------------------------------- the two kinds of entry
# A plain generated entry is a path below the design root that the generator writes (a tree
# entry). `$XEDA_GENERATED/<name>` is a file in the storage of the generator (a storage entry).


def _names(design: Design) -> list[str]:
    return [str(s.output_name) for s in design.rtl.sources]


def test_the_prefix_and_the_reserved_directory_have_one_spelling():
    assert STORAGE_PREFIX == "$XEDA_GENERATED/" and TREE_DIRECTORY == ".xeda-tree"
    assert storage_name("$XEDA_GENERATED/gen/top.v") == "gen/top.v"
    assert storage_name("$XEDA_GENERATED/gen/*.v") == "gen/*.v"
    assert storage_name("gen/top.v") is None
    assert storage_name("$XEDA_GENERATED") is None


def test_the_two_kinds_differ_in_equality_hash_json_and_identity(tmp_path):
    tree = _design(tmp_path / "tree", ["gen/top.v"])
    storage = _design(tmp_path / "storage", ["$XEDA_GENERATED/gen/top.v"])
    (a,), (b,) = tree.rtl.sources, storage.rtl.sources
    assert a != b and hash(a) != hash(b)
    assert (a.output_name, b.output_name) == ("gen/top.v", "$XEDA_GENERATED/gen/top.v")
    assert (a.as_json_value(), b.as_json_value()) == ("gen/top.v", "$XEDA_GENERATED/gen/top.v")
    assert design_configuration_hash(tree) != design_configuration_hash(storage)
    assert generation_key(tree)[0] != generation_key(storage)[0]
    for design in (tree, storage):
        again = Design(root=design.root, **design.to_dict())
        assert again.rtl.sources == design.rtl.sources
        assert generation_key(again)[0] == generation_key(design)[0]


def test_a_stated_type_of_a_storage_entry_survives_a_dump(tmp_path):
    design = _design(tmp_path, [{"path": "$XEDA_GENERATED/gen/odd", "type": "Data"}])
    (source,) = design.rtl.sources
    assert (source.output_name, source.type) == ("$XEDA_GENERATED/gen/odd", SourceType.Data)
    assert design.to_dict()["rtl"]["sources"] == [
        {"path": "$XEDA_GENERATED/gen/odd", "type": "Data"}
    ]
    assert Design(root=tmp_path, **design.to_dict()).rtl.sources == design.rtl.sources


def test_a_storage_entry_is_normalized_and_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_GENERATED", "/elsewhere")
    design = _design(tmp_path, ["$XEDA_GENERATED/gen/./sub/../top.v", "$XEDA_GENERATED/gen/*.sv"])
    assert _names(design) == ["$XEDA_GENERATED/gen/top.v", "$XEDA_GENERATED/gen/*.sv"]
    assert [s.type for s in design.rtl.sources] == [SourceType.Verilog, SourceType.SystemVerilog]
    assert not any(s.located for s in design.rtl.sources)


@pytest.mark.parametrize(
    "entry",
    [
        "$XEDA_GENERATED/",
        "$XEDA_GENERATED/.",
        "$XEDA_GENERATED/../top.v",
        "$XEDA_GENERATED/gen/../../top.v",
        "$XEDA_GENERATED//abs/top.v",
    ],
)
def test_a_storage_name_stays_below_the_storage(tmp_path, entry):
    with pytest.raises(DesignValidationError, match=r"not a name below the storage.*<name>"):
        _design(tmp_path, [entry])


@pytest.mark.parametrize(
    "entry",
    [
        "$XEDA_GENERATED/.xeda-tree/top.v",
        "$XEDA_GENERATED/.xeda-tree",
        "$XEDA_GENERATED/gen/../.xeda-tree/top.v",
        "$XEDA_GENERATED/.xeda*/top.v",
        "$XEDA_GENERATED/.*/top.v",
    ],
)
def test_the_directory_xeda_keeps_in_the_storage_is_not_a_name(tmp_path, entry):
    with pytest.raises(DesignValidationError, match=r"\.xeda-tree.*keeps for itself"):
        _design(tmp_path, [entry])


@pytest.mark.parametrize("entry", ["$XEDA_GENERATED/*/top.v", "$XEDA_GENERATED/gen/.xeda-tree/x.v"])
def test_only_a_first_part_that_matches_the_reserved_name_is_refused(tmp_path, entry):
    assert _names(_design(tmp_path, [entry])) == [entry]


@pytest.mark.parametrize(
    "entry",
    [
        "${XEDA_GENERATED}/gen/top.v",
        "$XEDA_GENERATED",
        "gen/$XEDA_GENERATED/top.v",
        "gen/${XEDA_GENERATED}",
    ],
)
def test_another_spelling_of_the_variable_is_refused(tmp_path, entry):
    with pytest.raises(DesignValidationError, match=r"write `\$XEDA_GENERATED/<name>`"):
        _design(tmp_path, [entry])


@pytest.mark.parametrize("entry", ["../top.v", "/abs/top.v", ".", "a/../.."])
def test_a_tree_entry_names_a_file_below_the_design_root(tmp_path, entry):
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path, [entry])
    assert "path below the design root that the generator writes" in str(error.value)
    assert "`$XEDA_GENERATED/<name>`" in str(error.value)


# ---------------------------------------------------------------- selection of either kind


def test_generated_sources_selects_each_kind_by_its_own_items(tmp_path):
    sources = ["top.v", "gen/a.v", "$XEDA_GENERATED/gen/b.v", "$XEDA_GENERATED/c.v"]
    design = _design(tmp_path, sources, ["gen", "$XEDA_GENERATED/gen", "$XEDA_GENERATED/c.v"])
    assert [s.generated for s in design.rtl.sources] == [False, True, True, True]
    assert _names(design)[1:] == ["gen/a.v", "$XEDA_GENERATED/gen/b.v", "$XEDA_GENERATED/c.v"]


def test_the_whole_storage_selects_every_storage_entry_and_no_tree_entry(tmp_path):
    design = _design(
        tmp_path,
        ["top.v", "$XEDA_GENERATED/a.v", "$XEDA_GENERATED/gen/b.v", "gen/c.v"],
        ["$XEDA_GENERATED", "gen/c.v"],
    )
    assert [s.generated for s in design.rtl.sources] == [False, True, True, True]
    assert tree_declarations(design) == ("gen/c.v",)


def test_an_item_of_one_kind_never_selects_an_entry_of_the_other(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "gen.v").write_text("module gen; endmodule\n")
    # a `*` part of a written pattern does not match the literal storage prefix
    design = _design(tmp_path, ["$XEDA_GENERATED/a.v", "*/gen.v"], ["$XEDA_GENERATED"])
    assert [s.generated for s in design.rtl.sources] == [True, False]
    # a storage item selects no tree entry, which is then written and must exist
    with pytest.raises(DesignValidationError, match="does not exist"):
        _design(tmp_path / "other", ["gen/a.v", "$XEDA_GENERATED/gen/b.v"], ["$XEDA_GENERATED/gen"])


def test_a_storage_entry_that_generated_sources_does_not_select_is_refused(tmp_path):
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path, ["gen/a.v", "$XEDA_GENERATED/gen/b.v"], ["gen"])
    assert "`$XEDA_GENERATED/gen/b.v`" in str(error.value)
    assert "list it (or `$XEDA_GENERATED`) in `generated_sources`" in str(error.value)


def test_a_generated_sources_item_of_the_storage_that_no_entry_relates_to_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match=r"not among `rtl.sources`: \$XEDA_GENERATED/x"):
        _design(
            tmp_path,
            ["$XEDA_GENERATED/gen/b.v", "top.v"],
            ["$XEDA_GENERATED/gen", "$XEDA_GENERATED/x"],
        )


def test_a_pattern_of_the_storage_that_generated_sources_names_in_part_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match=r"`\$XEDA_GENERATED/gen/\*.v` is partly"):
        _design(tmp_path, ["$XEDA_GENERATED/gen/*.v"], ["$XEDA_GENERATED/gen/b.v"])


# ---------------------------------------------------------------- written entries


def test_a_written_entry_may_not_name_the_storage(tmp_path):
    with pytest.raises(DesignValidationError, match=r"names `\$XEDA_GENERATED`"):
        Design(name="d", root=tmp_path, rtl={"sources": ["$XEDA_GENERATED/top.v"]})
    with pytest.raises(DesignValidationError, match=r"names `\$XEDA_GENERATED`"):
        Design(name="d", root=tmp_path, rtl={"sources": [{"file": "${XEDA_GENERATED}/top.v"}]})
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    with pytest.raises(DesignValidationError, match=r"names `\$XEDA_GENERATED`"):
        Design(
            name="d",
            root=tmp_path,
            rtl={"sources": ["top.v"]},
            tb={"sources": ["$XEDA_GENERATED/tb.v"]},
        )
    with pytest.raises(DesignValidationError, match=r"names `\$XEDA_GENERATED`"):
        _design(tmp_path / "gen", [{"file": "$XEDA_GENERATED/x.v"}, "a.v"])


# ---------------------------------------------------------------- the generator's own inputs


def _make_inputs(root: Path) -> None:
    """Files for the patterns of the generator's `sources`, which must match something."""
    for name in ("gen/ab.v", "gen/a.v", "gen/b.v", "gen/xy.v", "gen/x.sv", "src/s.v"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")


@pytest.mark.parametrize(
    "inputs, entry",
    [
        (["gen.py", "lib"], "lib/new.v"),
        (["gen.py", "lib/a.v"], "lib/*.v"),
        (["gen.py", "lib/*.v"], "lib/new.v"),
        (["."], "gen/top.v"),
        (["gen.py", "$DESIGN_ROOT/lib"], "lib/sub/new.v"),
        (["gen.py", "gen/ab*.v"], "gen/a*b.v"),
        (["gen.py", "gen/*b.v"], "gen/a*.v"),
        (["gen.py", "gen/ab*"], "gen/*.v"),
    ],
)
def test_a_tree_entry_among_the_inputs_of_the_generator_is_refused(tmp_path, inputs, entry):
    _make_inputs(tmp_path)
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path, [entry], inputs=inputs)
    assert "the generator would change its own input" in str(error.value)
    assert f"`{entry}`" in str(error.value)


@pytest.mark.parametrize(
    "inputs, entry",
    [
        (["gen.py", "lib"], "gen/top.v"),
        (["gen.py", "lib/a.v"], "lib/b.sv"),
        (["gen.py"], "lib/*.v"),
        (["lib/*.v"], "lib/sub/new.v"),
        (["gen.py", "src/*.v"], "gen/*.v"),
        (["gen.py", "gen/a*.v"], "gen/b*.v"),
        (["gen.py", "gen/*.v"], "gen/*.sv"),
        (["gen.py", "gen/x*y.v"], "gen/x*z.v"),
    ],
)
def test_a_tree_entry_apart_from_the_inputs_of_the_generator_is_accepted(tmp_path, inputs, entry):
    _make_inputs(tmp_path)
    assert _names(_design(tmp_path, [entry], inputs=inputs)) == [entry]


def test_a_storage_entry_is_never_among_the_inputs_of_the_generator(tmp_path):
    design = _design(tmp_path, ["$XEDA_GENERATED/lib/new.v"], inputs=["."])
    assert _names(design) == ["$XEDA_GENERATED/lib/new.v"]


# ---------------------------------------------------------------- the tree entries of a design


def test_tree_declarations_are_the_own_tree_entries_in_order(tmp_path):
    design = _design(
        tmp_path,
        ["gen/b.v", "$XEDA_GENERATED/a.v", "gen/a.v", "top.v", "gen/*.sv"],
        ["$XEDA_GENERATED", "gen"],
    )
    assert tree_declarations(design) == ("gen/b.v", "gen/a.v", "gen/*.sv")
    assert tree_declarations(_design(tmp_path / "s", ["$XEDA_GENERATED/a.v"])) == ()


def test_tree_entries_are_source_entries_and_storage_entries_are_not(tmp_path):
    design = _design(
        tmp_path,
        ["gen/b.v", "$XEDA_GENERATED/a.v", "gen/*.sv", "top.v"],
        ["gen", "$XEDA_GENERATED"],
    )
    assert design.tree_entries() == (
        SourceEntry("rtl.sources", "gen/b.v", tmp_path),
        SourceEntry("rtl.sources", "gen/*.sv", tmp_path),
    )
    texts = [(e.part, e.text) for e in design.source_entries()]
    assert texts == [
        ("rtl.sources", "top.v"),
        ("rtl.sources", "gen/b.v"),
        ("rtl.sources", "gen/*.sv"),
        ("rtl.generator.sources", "gen.py"),
    ]
    assert design.tree_entries()[0].matches(tmp_path / "gen" / "b.v")
    assert design.tree_entries()[1].matches(tmp_path / "gen" / "x.sv")
    assert not any(e.matches(tmp_path / "a.v") for e in design.source_entries())


def _app_with_dependency(tmp_path: Path, app_sources, dep_sources=("gen/a.v",)) -> Design:
    dep = tmp_path / "app" / "dep"
    dep.mkdir(parents=True)
    (dep / "gen.py").write_text("")
    (dep / "dep.yaml").write_text(
        f"name: dep\nrtl:\n  sources: {list(dep_sources)!r}\n  top: a\n  generator:\n"
        f"    executable: {sys.executable!r}\n    args: [gen.py]\n    sources: [gen.py]\n"
    )
    (tmp_path / "app" / "app.py").write_text("")
    return Design(
        name="app",
        root=tmp_path / "app",
        rtl={
            "sources": app_sources,
            "top": "top",
            "generator": {
                "executable": sys.executable,
                "args": ["app.py"],
                "sources": ["app.py"],
            },
        },
        dependencies=["dep/dep.yaml"],
    )


def test_the_tree_entries_of_a_merged_design_belong_to_the_unit_that_writes_them(tmp_path):
    merged = merged_design(_app_with_dependency(tmp_path, ["out/top.v"]), run_root=None)
    assert tree_declarations(merged) == ("out/top.v",), "the dependency's entry has an owner"
    assert merged.tree_entries() == (
        SourceEntry("rtl.sources", "gen/a.v", tmp_path / "app" / "dep"),
        SourceEntry("rtl.sources", "out/top.v", tmp_path / "app"),
    )
    assert [e.root for e in merged.source_entries() if e.part == "rtl.sources"] == [
        tmp_path / "app" / "dep",
        tmp_path / "app",
    ]


@pytest.mark.parametrize(
    "entry", ["dep/out.v", "dep/gen/new.v", "dep/*/new.v", "$DESIGN_ROOT/dep/x.v"]
)
def test_a_tree_entry_below_the_root_of_another_unit_is_refused_at_merge(tmp_path, entry):
    design = _app_with_dependency(tmp_path, [entry])
    with pytest.raises(DesignValidationError, match=r"below the root of .*dep") as error:
        merged_design(design, run_root=None)
    assert entry.replace("$DESIGN_ROOT/", "") in str(error.value)


@pytest.mark.parametrize("entry", ["out/top.v", "out/*.v", "depend/x.v"])
def test_a_tree_entry_apart_from_the_roots_of_the_other_units_is_accepted(tmp_path, entry):
    merged = merged_design(_app_with_dependency(tmp_path, [entry]), run_root=None)
    assert entry in tree_declarations(merged)
