"""Every design source has a type: inferred from its suffix by one table, or given.

A suffix xeda cannot type -- unknown, ambiguous, or in another letter case -- is a load error
asking for `type`, never an untyped source that a template crashes on (`src.type.name` on None)
or a type-filtering flow silently drops. An explicit `type` that is no member is an error naming
the closest members, never a silent fall-back to the suffix.
"""

import pytest

from xeda.design import (
    AMBIGUOUS_SUFFIXES,
    SOURCE_SUFFIXES,
    TYPE_ONLY,
    Design,
    DesignSource,
    DesignValidationError,
    SourceType,
    source_type_named,
    source_type_of,
)

#: Every member, in declaration order. The first 13 are the ordinals old `settings.json` files
#: record (`SourceType.from_str`), so members are only ever appended.
# fmt: off
MEMBERS = [
    "Verilog", "VerilogHeader", "SystemVerilog", "SVHeader", "Vhdl", "Bluespec", "Xdc", "Sdc",
    "MemoryFile", "Tcl", "Chisel", "Cpp", "Cocotb",
    "Lpf", "Pcf", "Pdc", "JsonNetlist", "EcpConfig", "IceAsc", "Fasm", "Bitstream",
    "VerilogNetlist", "VhdlNetlist", "Blif", "Edif", "Ucf", "Xcf", "Qsf", "Ldc", "Fdc",
    "Sdf", "Spef", "Saif", "Vcd", "Fst", "Ghw", "Vpd", "Fsdb", "Checkpoint",
    "Liberty", "Def", "Odb", "Gds", "Cdl", "Chipdb", "C", "CHeader", "ObjectFile", "Vlt", "Data",
]
# fmt: on


def test_the_members_are_appended_never_reordered():
    assert [member.name for member in SourceType] == MEMBERS


def test_every_member_is_inferred_from_a_suffix_or_given_by_type_only():
    inferred = {member for member, _variant in SOURCE_SUFFIXES.values()}
    assert inferred | TYPE_ONLY == set(SourceType)
    assert not inferred & TYPE_ONLY


def test_no_suffix_is_both_inferred_and_ambiguous():
    assert not set(SOURCE_SUFFIXES) & set(AMBIGUOUS_SUFFIXES)


@pytest.mark.parametrize(("suffix", "expected"), sorted(SOURCE_SUFFIXES.items()))
def test_a_suffix_types_its_source(tmp_path, suffix, expected):
    path = tmp_path / f"f.{suffix}"
    path.write_text("x\n")
    source = DesignSource(path)
    assert (source.type, source.variant) == expected


def test_memory_images_and_quartus_bitstreams_are_typed():
    assert SOURCE_SUFFIXES["init"] == SOURCE_SUFFIXES["hex"] == (SourceType.MemoryFile, None)
    assert SOURCE_SUFFIXES["sof"] == SOURCE_SUFFIXES["bit"] == (SourceType.Bitstream, None)


@pytest.mark.parametrize("suffix", sorted(AMBIGUOUS_SUFFIXES))
def test_an_ambiguous_suffix_asks_for_a_type(tmp_path, suffix):
    path = tmp_path / f"f.{suffix}"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=rf"`\.{suffix}` names several kinds of file") as raised:
        DesignSource(path)
    assert "type = " in str(raised.value)


def test_an_unknown_suffix_asks_for_a_type_and_offers_data(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"infers no source type from `\.txt`") as raised:
        DesignSource(path)
    assert '"Data"' in str(raised.value)


def test_a_file_without_a_suffix_asks_for_a_type(tmp_path):
    path = tmp_path / "Makefile"
    path.write_text("x\n")
    with pytest.raises(ValueError, match="a file without a suffix"):
        DesignSource(path)


def test_a_suffix_in_another_case_names_the_type_it_would_have(tmp_path):
    path = tmp_path / "TOP.VHD"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"`\.vhd` is Vhdl"):
        DesignSource(path)


def test_an_explicit_type_types_any_file(tmp_path):
    for name, member in (
        ("vectors.txt", SourceType.Data),
        ("top.json", SourceType.JsonNetlist),
        ("chip.bin", SourceType.Chipdb),
    ):
        (tmp_path / name).write_text("x\n")
        source = DesignSource({"file": str(tmp_path / name), "type": member.name})
        assert source.type is member


def test_a_type_name_in_any_case_or_a_legacy_ordinal_is_its_member():
    assert source_type_named("jsonnetlist") is SourceType.JsonNetlist
    assert source_type_named("5") is SourceType.Vhdl  # as an old settings.json recorded it


def test_an_unknown_explicit_type_names_the_closest_members(tmp_path):
    path = tmp_path / "f.v"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"unknown source type 'Verlog'; did you mean `Verilog`"):
        DesignSource({"file": str(path), "type": "Verlog"})


def test_a_design_with_a_source_xeda_cannot_type_does_not_load(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "notes.txt").write_text("x\n")
    with pytest.raises(DesignValidationError, match="notes.txt"):
        Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": ["top.v", "notes.txt"], "top": "top"},
        )


def test_an_unchecked_source_a_generator_writes_later_is_typed_too(tmp_path):
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": [{"path": "gen/top.v"}], "top": "top"}
    )
    assert design.rtl.sources[0].type is SourceType.Verilog


@pytest.mark.parametrize("typ", ["", False, 0, "Verlog"])
@pytest.mark.parametrize("form", ["table", "argument"])
def test_an_invalid_explicit_type_never_falls_back_to_the_suffix(tmp_path, typ, form):
    path = tmp_path / "top.v"
    path.write_text("module top; endmodule\n")
    with pytest.raises(ValueError, match="source type|source's `type`"):
        if form == "table":
            DesignSource({"file": str(path), "type": typ})
        else:
            DesignSource(path, typ=typ)


@pytest.mark.parametrize("variant", ["custom", ""])
def test_a_stated_variant_overrides_suffix_inference(tmp_path, variant):
    path = tmp_path / "top.bsv"
    path.write_text("x\n")
    source = DesignSource({"file": str(path), "variant": variant})
    assert source.type is SourceType.Bluespec
    assert source.variant == variant
    assert DesignSource(source.as_json_value()).variant == variant


def test_constructor_arguments_take_none_only_precedence_over_table_metadata(tmp_path):
    path = tmp_path / "top.bsv"
    path.write_text("x\n")
    table = {"file": str(path), "type": "Vhdl", "standard": "2008", "variant": "bh"}
    source = DesignSource(table, typ=SourceType.Bluespec, standard="", variant="")
    assert (source.type, source.standard, source.variant) == (SourceType.Bluespec, "", "")
    assert table == {"file": str(path), "type": "Vhdl", "standard": "2008", "variant": "bh"}


@pytest.mark.parametrize("checked", [True, False])
def test_source_construction_and_round_trips_preserve_the_callers_table(tmp_path, checked):
    key = "file" if checked else "path"
    path = tmp_path / "top.v"
    if checked:
        path.write_text("module top; endmodule\n")
    table = {key: "top.v", "type": "SystemVerilog", "standard": "2012", "variant": "custom"}
    before = dict(table)
    source = DesignSource(table, _root_path=tmp_path)
    assert table == before
    again = DesignSource(source.as_json_value())
    assert (again.type, again.standard, again.variant) == (
        SourceType.SystemVerilog,
        "2012",
        "custom",
    )
    assert again.as_json_value() == source.as_json_value()


def test_the_original_ordinals_still_name_the_original_members():
    for ordinal, name in enumerate(MEMBERS[:13], 1):
        assert source_type_named(str(ordinal)) is SourceType[name]


def test_suffix_inference_needs_no_file_to_exist(tmp_path):
    assert source_type_of(tmp_path / "later.bs") == (SourceType.Bluespec, "bh")
