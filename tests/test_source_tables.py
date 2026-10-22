"""A source table takes `file` or `path`, and `type`: nothing else.

A source once took a `standard` and a `variant` that no flow read. A design that wrote
`{file: tb.vhd, standard: "08"}` for a testbench using `std.env.finish` got a GHDL run with no
`--std`, which failed with `unit "env" not found in library "std"`: the design stated a standard
and nothing used it. The VHDL standard is one setting of the whole design
(`language.vhdl.standard`), because GHDL and NVC compile every VHDL source of a run under one
standard. A key of a source table that nothing reads is now refused where the design is loaded,
whichever way the design arrives, so a stated value can never be silently ignored.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import (
    REMOVED_SOURCE_KEYS,
    SOURCE_TABLE_KEYS,
    DesignSource,
    DesignValidationError,
    SourceType,
)
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Nvc
from xeda.xedaproject import XedaProject

from .tool_utils import require_ghdl, require_nvc

SRC = Path(__file__).parent.parent / "src" / "xeda"

#: a key a source table does not take -> what the refusal says
REFUSED = {
    "standard": "language.vhdl.standard",
    "variant": "type: SystemVerilog",
    "stadnard": "unknown key `stadnard`",
}

TESTBENCH = (
    "use std.env.all;\n"
    "entity tb is end;\n"
    "architecture a of tb is begin\n"
    "  process begin wait for 10 ns; finish; end process;\n"
    "end;\n"
)


@pytest.fixture
def design_dir(tmp_path: Path) -> Path:
    (tmp_path / "tb.vhd").write_text(TESTBENCH)
    return tmp_path


def design_data(source: dict, **extra) -> dict:
    return {"name": "d", "rtl": {"sources": [], "top": "tb"}, "tb": {"sources": [source]}, **extra}


def test_the_removed_keys_are_the_ones_a_source_table_does_not_take() -> None:
    assert not set(REMOVED_SOURCE_KEYS) & SOURCE_TABLE_KEYS
    assert set(REFUSED) >= set(REMOVED_SOURCE_KEYS)


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_a_source_table_with_another_key_is_refused(design_dir: Path, key: str) -> None:
    with pytest.raises(ValueError, match=re.escape(REFUSED[key])):
        DesignSource({"file": str(design_dir / "tb.vhd"), key: "08"})


def test_every_wrong_key_of_a_table_is_named(design_dir: Path) -> None:
    with pytest.raises(ValueError) as error:
        DesignSource({"file": str(design_dir / "tb.vhd"), "standard": "08", "bogus": 1})
    assert "`standard` was removed" in str(error.value)
    assert "unknown key `bogus`" in str(error.value)


@pytest.mark.parametrize("key", sorted(REFUSED))
@pytest.mark.parametrize("part", ["rtl", "tb"])
def test_the_api_refuses_it_in_either_part(design_dir: Path, key: str, part: str) -> None:
    data = design_data({"file": "tb.vhd"})
    data[part]["sources"] = [{"file": "tb.vhd", key: "08"}]
    with pytest.raises(Exception, match=re.escape(REFUSED[key])):
        Design(design_root=design_dir, **data)


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_a_design_file_is_refused_naming_the_source_list(design_dir: Path, key: str) -> None:
    path = design_dir / "d.yaml"
    path.write_text(yaml.safe_dump(design_data({"file": "tb.vhd", key: "08"})))
    with pytest.raises(DesignValidationError, match=re.escape(REFUSED[key])) as error:
        Design.from_file(path)
    assert "tb.sources" in str(error.value)


@pytest.mark.parametrize("key", sorted(REFUSED))
@pytest.mark.parametrize("selected", [True, False])
def test_a_target_is_refused_whether_or_not_it_is_selected(
    design_dir: Path, key: str, selected: bool
) -> None:
    """A target's own mistake is reported where it is written, as the other keys of a target are."""
    (design_dir / "board.vhd").write_text("entity board is end;\n")
    data = design_data(
        {"file": "tb.vhd"},
        targets={
            "bad": {"rtl": {"sources": [{"file": "board.vhd", key: "08"}]}},
            "good": {"rtl": {"sources": ["board.vhd"]}},
        },
    )
    path = design_dir / "d.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(DesignValidationError, match=re.escape(REFUSED[key])) as error:
        Design.from_file(path, target="bad" if selected else "good")
    assert "targets.bad.rtl.sources" in str(error.value)


def test_design_overrides_are_refused_too(design_dir: Path) -> None:
    path = design_dir / "d.yaml"
    path.write_text(yaml.safe_dump(design_data({"file": "tb.vhd"})))
    with pytest.raises(DesignValidationError, match="language.vhdl.standard"):
        Design.from_file(
            path,
            overrides={"tb": {"sources": [{"file": "tb.vhd", "standard": "08"}]}},
        )


def test_a_project_design_is_refused(design_dir: Path) -> None:
    project = design_dir / "xedaproject.yaml"
    project.write_text(
        yaml.safe_dump({"designs": [design_data({"file": "tb.vhd", "standard": "08"})]})
    )
    with pytest.raises(DesignValidationError, match="language.vhdl.standard"):
        XedaProject.from_file(project).get_design("d")


def test_the_command_line_reports_it_as_a_document(design_dir: Path, monkeypatch) -> None:
    """The case that started this: the testbench with its own standard never reaches GHDL."""
    monkeypatch.chdir(design_dir)
    path = design_dir / "d.yaml"
    path.write_text(yaml.safe_dump(design_data({"file": "tb.vhd", "standard": "08"})))
    result = CliRunner().invoke(cli, ["run", "ghdl_sim", str(path), "--json"])
    document = json.loads(result.stdout)
    assert result.exit_code == 2 and document["outcome"] is None
    assert document["error"]["code"] == "invalid_design"
    assert "language.vhdl.standard" in document["error"]["message"]
    assert not (design_dir / "xeda_run").exists()


def test_a_stated_type_is_still_written_back(design_dir: Path) -> None:
    source = DesignSource({"file": str(design_dir / "tb.vhd"), "type": "Vhdl"})
    assert source.as_json_value() == {"file": str(design_dir / "tb.vhd"), "type": "Vhdl"}
    assert DesignSource(source.as_json_value()).type is SourceType.Vhdl


def test_the_schema_offers_exactly_the_keys_a_table_takes() -> None:
    from xeda.introspect import design_schema

    schema = design_schema()
    text = json.dumps(schema)
    assert '"standard": {"type": "string"' not in text
    object_form = Design.model_json_schema()["$defs"]["RtlSettings"]["properties"]["sources"][
        "items"
    ]["anyOf"][1]
    assert set(object_form["properties"]) == SOURCE_TABLE_KEYS


# ---------------------------------------------------------------------------- the supported way


def test_ghdl_sim_runs_a_testbench_that_needs_vhdl_2008_with_the_design_standard(
    design_dir: Path,
) -> None:
    require_ghdl()
    design = Design(
        name="d",
        design_root=design_dir,
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    flow = DefaultRunner(design_dir / "xeda_run").run(GhdlSim, design)
    assert flow is not None and flow.succeeded


def test_nvc_runs_a_testbench_that_needs_vhdl_2008_with_the_design_standard(
    design_dir: Path,
) -> None:
    require_nvc()
    design = Design(
        name="d",
        design_root=design_dir,
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    flow = DefaultRunner(design_dir / "xeda_run").run(Nvc, design)
    assert flow is not None and flow.succeeded


# ---------------------------------------------------------------------------- the scan


READS_A_SOURCE_S_METADATA = re.compile(r"\b(?:src|source|s)\.(?:variant|standard)\b")


def test_no_code_or_template_reads_a_source_s_standard_or_variant() -> None:
    """The mechanism cannot return quietly: a flow reads the design's `language.vhdl.standard`,
    and nothing reads `.standard` or `.variant` of a source."""
    found = [
        f"{path.relative_to(SRC)}:{n}: {line.strip()}"
        for path in sorted(SRC.rglob("*"))
        if path.suffix in (".py", ".tcl", ".ys", ".xdc", ".sdc", ".fdc", ".ldc", ".j2")
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if READS_A_SOURCE_S_METADATA.search(line)
    ]
    assert not found, "\n".join(found)
    assert not hasattr(DesignSource, "standard") and not hasattr(DesignSource, "variant")
