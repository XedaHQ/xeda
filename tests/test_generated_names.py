"""The place of a generated file in the identity of a derived design.

A converter writes files into its run directory. The identity of the design it derives counts each
of them by its content, its type and its place in that output, never by a path that leads from the
design root into the run directory: that path depends on where the run root is. A link in the
output stands for the file it points to, so that a link that now points to another file with the
same bytes (and other neighbors) is another design.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import Source, SourceType
from xeda.flow.derived import derive_design, generated_name

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the tests make symbolic links, which need privileges on Windows"
)


def _design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Top.bsv").write_text("package Top; endpackage\n")
    (root / "ext.v").write_text("module ext(); endmodule\n")
    return Design(name="d", root=root, rtl={"sources": ["Top.bsv", "ext.v"], "top": "mkTop"})


def _record(*generated: Path) -> dict:
    return {
        "kind": "design",
        "top": "mkTop",
        "input_design": "0" * 64,
        "sources": [
            *({"generated": str(path), "type": "Verilog", "sha": "0" * 32} for path in generated),
            {"source": 1, "file": "ext.v", "type": "Verilog"},
        ],
    }


def _output(base: Path) -> Path:
    """An output directory with two files of the same bytes in two directories."""
    for directory in ("a", "b"):
        (base / directory).mkdir(parents=True, exist_ok=True)
        (base / directory / "top.v").write_text("module top(); endmodule\n")
    return base


def test_the_name_is_the_path_within_the_output(tmp_path) -> None:
    base = _output(tmp_path / "out")
    assert generated_name(base / "a" / "top.v", base) == "a/top.v"
    assert generated_name(base / "b" / "top.v", base) == "b/top.v"


def test_the_name_does_not_depend_on_where_the_output_is(tmp_path) -> None:
    near = _output(tmp_path / "out")
    far = _output(tmp_path / "deeper" / "elsewhere" / "out")
    assert generated_name(near / "a" / "top.v", near) == generated_name(far / "a" / "top.v", far)


def test_the_name_through_an_alias_of_the_output(tmp_path) -> None:
    base = _output(tmp_path / "out")
    alias = tmp_path / "alias"
    alias.symlink_to(base, target_is_directory=True)
    assert generated_name(alias / "a" / "top.v", base) == "a/top.v"
    assert generated_name(base / "a" / "top.v", alias) == "a/top.v"


def test_a_link_in_the_output_stands_for_its_target(tmp_path) -> None:
    base = _output(tmp_path / "out")
    (base / "top.v").symlink_to("a/top.v")
    assert generated_name(base / "top.v", base) == "a/top.v"
    (base / "top.v").unlink()
    (base / "top.v").symlink_to("b/top.v")
    assert generated_name(base / "top.v", base) == "b/top.v"


def test_a_link_out_of_the_output_keeps_its_own_name(tmp_path) -> None:
    base = _output(tmp_path / "out")
    elsewhere = tmp_path / "elsewhere.v"
    elsewhere.write_text("module top(); endmodule\n")
    (base / "top.v").symlink_to(elsewhere)
    assert generated_name(base / "top.v", base) == "top.v"


def test_a_file_outside_the_output_has_no_name(tmp_path) -> None:
    base = _output(tmp_path / "out")
    other = tmp_path / "other.v"
    other.write_text("module other(); endmodule\n")
    with pytest.raises(ValueError, match="is not inside"):
        generated_name(other, base)
    with pytest.raises(ValueError, match="is not inside"):
        derive_design(_design(tmp_path / "d"), _record(other), base)


def test_the_hash_follows_the_target_of_a_link(tmp_path) -> None:
    """The same bytes, reached through the same name, from another directory: another design,
    since a tool looks for the neighbors of a file where the file is."""
    design = _design(tmp_path / "d")
    base = _output(tmp_path / "out")
    link = base / "top.v"
    link.symlink_to("a/top.v")
    first = derive_design(design, _record(link), base)
    link.unlink()
    link.symlink_to("b/top.v")
    second = derive_design(design, _record(link), base)
    assert first.rtl_hash != second.rtl_hash


def test_the_hash_does_not_depend_on_where_the_output_is(tmp_path) -> None:
    design = _design(tmp_path / "d")
    hashes = []
    for place in ("out", "deeper/elsewhere/out"):
        base = _output(tmp_path / place)
        derived = derive_design(design, _record(base / "a" / "top.v"), base)
        assert derived.rtl.sources[0].output_name == "a/top.v"
        hashes.append(derived.rtl_hash)
    assert hashes[0] == hashes[1]
    assert str(tmp_path) not in str(design.rtl_fingerprint)


def test_a_copy_of_a_derived_design_keeps_its_identity(tmp_path) -> None:
    """The launch builds the flow on a deep copy of the derived design."""
    design = _design(tmp_path / "d")
    base = _output(tmp_path / "out")
    derived = derive_design(design, _record(base / "a" / "top.v"), base)
    copied = derived.model_copy(deep=True)
    assert copied.rtl.sources[0].output_name == "a/top.v"
    assert copied.rtl_hash == derived.rtl_hash
    assert copied.rtl.sources[0].type is SourceType.Verilog


# --------------------------------------------- one meaning of `generated`, for both producers


def test_a_declaration_is_a_name_in_the_output_and_names_no_file() -> None:
    import copy

    source = Source.declaration("gen/*.v", SourceType.Verilog)
    assert source.generated and not source.located
    assert source.output_name == "gen/*.v" and source.path == Path("gen/*.v")
    assert source.file is None and str(source) == "gen/*.v"
    with pytest.raises(FileNotFoundError, match="generator has not run"):
        source.content_hash
    with pytest.raises(AttributeError):
        source.output_name = "x"  # type: ignore[misc]
    again = copy.deepcopy(source)
    assert again == source and hash(again) == hash(source) and not again.located


def test_a_declaration_is_equal_to_the_same_name_and_type_only() -> None:
    verilog = Source.declaration("gen/*.v", SourceType.Verilog)
    assert verilog == Source.declaration("gen/*.v", SourceType.Verilog)
    assert verilog != Source.declaration("gen/*.v", SourceType.SystemVerilog)
    assert verilog != Source.declaration("gen/*.sv", SourceType.Verilog)
    assert verilog != Source({"path": "/x/gen/*.v"}, SourceType.Verilog)


def test_a_declaration_is_written_as_a_name_or_a_table_with_its_stated_type() -> None:
    assert Source.declaration("gen/top.v", SourceType.Verilog).as_json_value() == "gen/top.v"
    stated = Source.declaration("gen/top", SourceType.Verilog, stated=True)
    assert stated.as_json_value() == {"path": "gen/top", "type": "Verilog"}


def test_a_converter_output_and_a_declaration_are_generated_in_one_sense(tmp_path) -> None:
    base = _output(tmp_path / "out")
    derived = derive_design(_design(tmp_path / "d"), _record(base / "a" / "top.v"), base)
    written, converted = derived.rtl.sources[1], derived.rtl.sources[0]
    assert converted.generated and converted.output_name == "a/top.v" and converted.located
    assert not written.generated and written.output_name is None
    assert derived.source_place(converted) == "a/top.v"
    assert derived.source_place(Source.declaration("gen/*.v", SourceType.Verilog)) == "gen/*.v"


def test_a_generated_source_needs_its_name_and_a_written_source_has_none(tmp_path) -> None:
    with pytest.raises(ValueError, match="output_name"):
        Source({"path": str(tmp_path / "top.v")}, SourceType.Verilog, generated=True)
    with pytest.raises(ValueError, match="output_name"):
        Source({"path": str(tmp_path / "top.v")}, SourceType.Verilog, output_name="top.v")


def test_a_declaration_has_an_owner_and_the_same_name_of_two_owners_is_two_declarations() -> None:
    mine = Source.declaration("gen/top.v", SourceType.Verilog)
    theirs = Source.declaration("gen/top.v", SourceType.Verilog, owner="/dep")
    assert mine.owner is None and theirs.owner == "/dep"
    assert mine != theirs and hash(mine) != hash(theirs)
    assert theirs == Source.declaration("gen/top.v", SourceType.Verilog, owner="/dep")
    assert theirs.with_owner("/other").owner == "/other" and theirs.owner == "/dep"
    assert theirs.as_json_value() == "gen/top.v", "a dump holds no owner"
