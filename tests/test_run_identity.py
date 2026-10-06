"""What identifies a flow run: `flowrun_hash` over the settings, beside the design's hash.

A run's identity depends on what its inputs mean, not on where anything is: moving a design,
copying it, or starting xeda from another directory keeps it. Design sources count by content,
metadata and path relative to the design root (`Design.rtl_hash`); settings count by value, with
a path counting as its text relative to the design root or start directory -- no file or
directory named in the settings is ever read.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

import xeda
from xeda import Design
from xeda.flow import flowrun_hash
from xeda.flow_runner.bindings import node_identity
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth
from xeda.utils import dump_json

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


def _design_copy(where: Path) -> Path:
    where.mkdir(parents=True)
    shutil.copy(SQRT / "sqrt.vhdl", where)
    (where / "c.xdc").write_text("create_clock -period 5 [get_ports clk]\n")
    return where


def _hash(design_root: Path, runner_cwd: Path, **settings) -> str:
    validated = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a100tftg256-2L", **settings}, design_root=design_root, runner_cwd=runner_cwd
    )
    return flowrun_hash("vivado_synth", validated)


def _design(root: Path) -> Design:
    return Design(
        name="sqrt",
        rtl={"sources": ["sqrt.vhdl"], "top": "sqrt", "clock_port": "clk"},
        design_root=root,
    )


def test_the_directory_xeda_is_started_from_does_not_change_the_hash(tmp_path):
    design = _design_copy(tmp_path / "d")
    first, second = tmp_path / "a", tmp_path / "b"
    assert _hash(design, first, tcl_files=[first / "hook.tcl"]) == _hash(
        design, second, tcl_files=[second / "hook.tcl"]
    )


@pytest.mark.parametrize(
    "xdc",
    ["c.xdc", "$DESIGN_ROOT/c.xdc", "{root}/c.xdc"],
    ids=["relative", "design-root-variable", "absolute-under-root"],
)
def test_an_identical_copy_of_the_design_elsewhere_hashes_the_same(tmp_path, xdc):
    one, other = _design_copy(tmp_path / "one"), _design_copy(tmp_path / "elsewhere" / "other")

    here = _hash(one, tmp_path, xdc_files=[xdc.format(root=one)])
    there = _hash(other, tmp_path, xdc_files=[xdc.format(root=other)])

    assert here == there
    assert _design(one).rtl_hash == _design(other).rtl_hash


def test_a_different_setting_is_a_different_run(tmp_path):
    design = _design_copy(tmp_path / "d")
    assert _hash(design, tmp_path, clock_period=5.0) != _hash(design, tmp_path, clock_period=4.0)
    assert _hash(design, tmp_path, xdc_files=["c.xdc"]) != _hash(design, tmp_path)


def test_design_sources_count_by_content_and_settings_paths_by_text(tmp_path):
    """Editing an RTL source is a different run; editing a file a setting names is not -- a
    file whose content should matter belongs in the design's sources (`SourceType.Xdc`, ...)."""
    root = _design_copy(tmp_path / "d")
    rtl, settings = _design(root).rtl_hash, _hash(root, tmp_path, xdc_files=["c.xdc"])

    (root / "c.xdc").write_text("create_clock -period 2 [get_ports clk]\n")
    assert _hash(root, tmp_path, xdc_files=["c.xdc"]) == settings

    with open(root / "sqrt.vhdl", "a") as f:
        f.write("\n-- edited\n")
    assert _design(root).rtl_hash != rtl


def test_design_source_order_and_compile_metadata_are_part_of_the_hash(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    for name in ("a.vhd", "b.vhd"):
        (root / name).write_text(f"-- {name}\n")

    def design(sources, **rtl):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": sources, "top": "top", **rtl},
        )

    original = design(["a.vhd", "b.vhd"])
    assert original.rtl_hash != design(["b.vhd", "a.vhd"]).rtl_hash
    assert original.rtl_hash != design([{"file": "a.vhd", "standard": "2008"}, "b.vhd"]).rtl_hash


def test_every_source_path_is_part_of_the_hash_relative_to_the_design_root(tmp_path):
    """A changed layout may change a tool's lookup, including files sourced by constraints.
    The same layout still has the same identity when the whole design moves."""
    one = tmp_path / "one"
    other = tmp_path / "other"
    for root in (one, other):
        (root / "rtl").mkdir(parents=True)
        (root / "vendor").mkdir()
        for sub in ("rtl", "vendor"):
            (root / sub / "top.vhd").write_text("entity top is end;\n")
            (root / sub / "top.v").write_text("module top; endmodule\n")
            (root / sub / "timing.xdc").write_text(
                "source [file dirname [info script]]/other.xdc\n"
            )

    def source_hash(root, source):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": [source], "top": "top"},
        ).rtl_hash

    for source in ("top.vhd", "top.v", "timing.xdc"):
        assert source_hash(one, f"rtl/{source}") != source_hash(one, f"vendor/{source}")
    for source in ("rtl/top.vhd", "rtl/top.v", "rtl/timing.xdc"):
        assert source_hash(one, source) == source_hash(other, source), "the design moved"

    # What the sources *are* still decides it.
    (other / "rtl" / "top.vhd").write_text("entity top is port (x : in bit); end;\n")
    assert source_hash(one, "rtl/top.vhd") != source_hash(other, "rtl/top.vhd")


def test_where_an_include_finds_a_header_is_part_of_the_hash(tmp_path):
    """A Verilog `include` is resolved by place -- the including file's directory first, then
    the header directories -- so two layouts of the same files can build different netlists:
    here `rtl/top.v` includes whichever `defs.vh` sits beside it. Counted by content alone,
    both were one design, and `--cached-dependencies` reused one's netlist for the other.
    Every source counts by its path relative to the design root."""

    def layout(root: Path, beside: str, elsewhere: str) -> Design:
        (root / "rtl").mkdir(parents=True)
        (root / "inc").mkdir()
        (root / "rtl" / "top.v").write_text(
            '`include "defs.vh"\nmodule top(output [7:0] y); assign y = `VAL; endmodule\n'
        )
        (root / "rtl" / "defs.vh").write_text(f"`define VAL 8'd{beside}\n")
        (root / "inc" / "defs.vh").write_text(f"`define VAL 8'd{elsewhere}\n")
        # listed so that the contents come in the same order in both layouts
        first, second = ("rtl", "inc") if beside == "1" else ("inc", "rtl")
        return Design(
            name="inc",
            design_root=root,
            rtl={
                "sources": ["rtl/top.v", f"{first}/defs.vh", f"{second}/defs.vh"],
                "top": "top",
            },
        )

    ones = layout(tmp_path / "d1", beside="1", elsewhere="2")
    twos = layout(tmp_path / "d2", beside="2", elsewhere="1")
    assert ones.rtl_hash != twos.rtl_hash

    # Still relative to the root: the same layout elsewhere is the same design.
    assert layout(tmp_path / "moved" / "d1", beside="1", elsewhere="2").rtl_hash == ones.rtl_hash


def test_a_tcl_script_counts_by_its_place(tmp_path):
    """A TCL script finds other files by its own place (`[file dirname [info script]]`), so the
    same script elsewhere in the design can read another file: it is another design."""
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "setup.tcl").write_text("source [file dirname [info script]]/more.tcl\n")

    def rtl_hash(script: str) -> str:
        return Design(name="d", design_root=tmp_path, rtl={"sources": [script]}).rtl_hash

    assert rtl_hash("a/setup.tcl") != rtl_hash("b/setup.tcl")


def test_a_header_outside_the_root_counts_by_its_place_relative_to_the_root(tmp_path):
    """A source outside the design root counts by its path relative to the root
    as well (`../lib/defs.vh`): an `include` depends on where files sit relative to each other.
    It counted by the path as the design file wrote it, so the same header written absolutely was
    another design, and a design reloaded from its own record -- which names the file absolutely
    -- could not reproduce its `design_hash`."""

    def tree(where: Path) -> Path:
        (where / "lib").mkdir(parents=True)
        (where / "lib" / "defs.vh").write_text("`define VAL 1\n")
        (where / "d").mkdir()
        (where / "d" / "top.v").write_text('`include "defs.vh"\nmodule top; endmodule\n')
        return (where / "d").resolve()

    def design(root: Path, header: str) -> Design:
        return Design(name="d", design_root=root, rtl={"sources": ["top.v", header], "top": "top"})

    here = tree(tmp_path / "here")
    written = design(here, "../lib/defs.vh")
    assert written.rtl_hash == design(here, str(here.parent / "lib" / "defs.vh")).rtl_hash

    recorded = tmp_path / "settings.json"
    dump_json({"design": written}, recorded)
    assert Design(**json.loads(recorded.read_text())["design"]).rtl_hash == written.rtl_hash

    # moved together with the directory beside it, it is the same design
    assert design(tree(tmp_path / "there"), "../lib/defs.vh").rtl_hash == written.rtl_hash


def test_behavior_affecting_design_metadata_is_part_of_the_hash(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk); endmodule\n")

    def design(**rtl):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": ["top.v"], "top": "top", **rtl},
        )

    original = design(clock_port="clk")
    assert original.rtl_hash != design(clock_port="other_clk").rtl_hash
    assert original.rtl_hash != design(attributes={"keep": {"top": True}}).rtl_hash
    assert (
        original.rtl_hash
        != Design(
            name="d",
            design_root=root,
            rtl={"sources": ["top.v"], "top": "top", "clock_port": "clk"},
            hdl={"verilog": "2005"},
        ).rtl_hash
    )


def test_where_settings_were_given_is_not_part_of_them(tmp_path):
    design = _design_copy(tmp_path / "d")
    settings = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a100tftg256-2L"}, design_root=design, runner_cwd=tmp_path
    )

    dumped = str(settings.model_dump())
    assert str(design) not in dumped and str(tmp_path) not in dumped
    assert settings.context == {"design_root": design, "runner_cwd": tmp_path}


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setenv("PATH", str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"])


def test_an_unchanged_rerun_reuses_the_previous_run_from_any_directory(
    tmp_path, monkeypatch, fake_tools
):
    """A run whose inputs did not change is reused, not repeated -- also when xeda is started
    from another directory, which used to change the settings' hash."""
    design = Design.from_file(SQRT / "sqrt.yaml")
    settings = {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}
    runner = DefaultRunner(tmp_path / "xeda_run")

    monkeypatch.chdir(_design_copy(tmp_path / "start_one"))
    first = runner.run_flow(VivadoSynth, design, dict(settings))
    monkeypatch.chdir(_design_copy(tmp_path / "start_two"))
    second = runner.run_flow(VivadoSynth, design, dict(settings))

    assert first is not None and second is not None and first.succeeded
    assert second.results.timestamp == first.results.timestamp, "the second run was repeated"
    changed = runner.run_flow(VivadoSynth, design, {**settings, "clock_period": 4.5})
    assert changed is not None and changed.results.timestamp != first.results.timestamp


def test_a_run_never_modifies_its_input_settings(tmp_path, fake_tools):
    """The flow completes a copy of its own; the input it was launched with -- what identifies
    and records the run -- is never modified, whatever the flow does to its copy."""
    design = Design.from_file(SQRT / "sqrt.yaml")
    given = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5, "bitstream": "sqrt.bit"},
        design_root=design.root_path,
    )
    before = given.model_dump()

    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(VivadoSynth, design, given)

    assert flow is not None and flow.succeeded
    assert flow.settings.bitstream is not None and flow.settings.bitstream.is_absolute()
    assert given.model_dump() == before, "the flow's work reached the settings it was given"
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["flow_settings"]["bitstream"] == "sqrt.bit"
    assert "effective_flow_settings" in recorded  # the flow's final settings, after `run()`


def test_a_recorded_settings_json_is_a_rerunnable_input(tmp_path, fake_tools):
    design = Design.from_file(SQRT / "sqrt.yaml")
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        VivadoSynth, design, {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}
    )
    assert flow is not None
    recorded = json.loads((flow.run_path / "settings.json").read_text())

    again = VivadoSynth.Settings.from_input(recorded["flow_settings"], design_root=design.root_path)

    # the settings alone hash as recorded; the run's identity adds its input origins (none
    # for a flow that declares no inputs)
    assert flowrun_hash("vivado_synth", again) == recorded["settings_hash"]
    assert node_identity(recorded["settings_hash"]) == recorded["flowrun_hash"]


def test_a_source_hashes_the_same_however_its_path_is_written(tmp_path):
    """One file is one source, named relatively, through `$DESIGN_ROOT`, absolutely or by a
    glob, and wherever the design tree sits.

    A glob reaching outside the design root expands to absolute matches, and the fingerprint
    used to record a source's path as it was given: that one spelling therefore gave the design
    a different identity on every machine and after every move -- exactly what `rtl_fingerprint`
    promises it does not do.
    """

    def tree(where: Path) -> Path:
        (where / "shared").mkdir(parents=True)
        (where / "shared" / "pkg.vhd").write_text("-- pkg\n")
        (where / "design").mkdir()
        (where / "design" / "top.vhd").write_text("-- top\n")
        return (where / "design").resolve()

    roots = [tree(tmp_path / where) for where in ("here", "there")]
    spellings = [
        "../shared/pkg.vhd",
        "$DESIGN_ROOT/../shared/pkg.vhd",
        "../shared/*.vhd",
        "$DESIGN_ROOT/../shared/*.vhd",
    ]
    hashes = {
        Design(
            name="d", design_root=root, rtl={"sources": [spelling, "top.vhd"], "top": "top"}
        ).rtl_hash
        for root in roots
        for spelling in spellings + [str(root.parent / "shared" / "pkg.vhd")]
    }
    assert len(hashes) == 1, "one source hashes differently depending on how its path is written"


def test_a_file_valued_parameter_hashes_by_its_path_under_the_design_root(tmp_path):
    """A parameter given as a file resolves to that file's absolute path, because that is what
    reaches the tool -- so counting the design by the parameter's value as is tied its identity
    to where it lived. A path under the design root counts relative to it instead, the rule
    `flowrun_hash` applies to settings.

    `{ path = ... }` names a file that need not exist yet (an output a testbench writes, a file
    a generator makes), which is also why such a parameter cannot be counted by its content the
    way a source is.
    """

    def tree(where: Path) -> Path:
        where.mkdir(parents=True)
        (where / "top.vhd").write_text("-- top\n")
        (where / "rom.mem").write_text("00\n")
        return where.resolve()

    def design(root: Path, **parameters):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": ["top.vhd"], "top": "top", "parameters": parameters},
        )

    parameters = dict(ROM={"file": "rom.mem"}, DUMP={"path": "out/dump.txt"}, WIDTH=8)
    designs = [design(tree(tmp_path / where), **parameters) for where in ("here", "there")]

    assert designs[0].rtl.parameters["ROM"] == str(designs[0].root_path / "rom.mem")
    assert designs[0].rtl_fingerprint["parameters"] == {
        "ROM": "$DESIGN_ROOT/rom.mem",
        "DUMP": "$DESIGN_ROOT/out/dump.txt",
        "WIDTH": 8,
    }
    assert designs[0].rtl_hash == designs[1].rtl_hash

    # `file` and `path` differ only in whether the file must exist when the design is loaded.
    # The tool is handed the same text either way, so it is the same design.
    as_input = design(designs[0].root_path, ROM={"file": "rom.mem"}, DUMP={"file": "rom.mem"})
    as_output = design(designs[0].root_path, ROM={"file": "rom.mem"}, DUMP={"path": "rom.mem"})
    assert as_input.rtl_hash == as_output.rtl_hash

    # Which file a parameter names still decides it.
    moved = design(designs[0].root_path, ROM={"file": "rom.mem"}, DUMP={"path": "dump.txt"})
    assert moved.rtl_hash != designs[0].rtl_hash

    # The identity survives the design's own round trip: a reloaded design is counted exactly
    # as the one recorded, which is what `settings.json` is compared against. Written exactly
    # as `settings.json` writes a design, through `dump_json`.
    recorded = tmp_path / "settings.json"
    dump_json({"design": designs[0]}, recorded, backup=False)
    reloaded = Design(**json.loads(recorded.read_text())["design"])
    assert reloaded.rtl_fingerprint["parameters"] == designs[0].rtl_fingerprint["parameters"]
    assert reloaded.rtl_hash == designs[0].rtl_hash


def test_a_parameter_inherited_from_a_dependency_keeps_how_it_was_written(tmp_path):
    """A design without parameters of its own takes its dependency's. Those arrive already
    validated -- a file-valued one as the absolute path it resolved to -- and a path under the
    design root still counts relative to it, so the parent is not counted by its dependency's
    absolute location: that would be a different hash on every machine."""

    def tree(where: Path) -> Path:
        dep = where / "dep"
        dep.mkdir(parents=True)
        (dep / "core.vhd").write_text("-- core\n")
        (dep / "rom.mem").write_text("00\n")
        (dep / "dep.toml").write_text(
            'name = "dep"\n[rtl]\nsources = ["core.vhd"]\ntop = "core"\n'
            '[rtl.parameters]\nROM = { file = "rom.mem" }\nWIDTH = 8\n'
        )
        (where / "top.vhd").write_text("-- top\n")
        return where.resolve()

    designs = []
    for where in ("here", "there"):
        root = tree(tmp_path / where)
        designs.append(
            Design(
                name="d",
                design_root=root,
                rtl={"sources": ["top.vhd"], "top": "top"},
                # appended (`pos = -1`): the form that also inherits the dependency's parameters
                dependencies=[{"uri": str(root / "dep" / "dep.toml"), "rtl": {"pos": -1}}],
            )
        )

    assert designs[0].rtl.parameters["ROM"] == str(designs[0].root_path / "dep" / "rom.mem")
    assert designs[0].rtl_hash == designs[1].rtl_hash


@pytest.mark.parametrize("own_tb", [None, {"top": "own_tb"}], ids=["no_tb", "tb_top_only"])
@pytest.mark.parametrize("pos", [-1, 0])
def test_a_recorded_design_with_a_dependency_reloads_as_itself(tmp_path, pos, own_tb):
    """A dependency's sources are merged into the design when it is built, and a design is
    recorded as it was built. Recording the dependency *as well* merged its sources again on
    every reload -- `['top.vhd', 'core.vhd', 'core.vhd']` -- so the `design_hash` in a
    `settings.json` could not be reproduced from the design beside it, and a remote run was
    handed the duplicate (and a dependency to fetch all over again).

    The record has to hold the whole merge, testbench included: a design that gave no `tb` of
    its own was recorded without the testbench its dependency supplied, and reloaded as another
    design."""
    root = tmp_path / "d"
    (root / "dep").mkdir(parents=True)
    (root / "top.vhd").write_text("-- top\n")
    (root / "dep" / "core.vhd").write_text("-- core\n")
    (root / "dep" / "tb.vhd").write_text("-- tb\n")
    (root / "dep" / "dep.toml").write_text(
        'name = "dep"\n[rtl]\nsources = ["core.vhd"]\ntop = "core"\n'
        '[tb]\nsources = ["tb.vhd"]\ntop = "tb"\n'
    )
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.vhd"], "top": "top"},
        **({"tb": own_tb} if own_tb else {}),
        dependencies=[{"uri": str(root / "dep" / "dep.toml"), "rtl": {"pos": pos}}],
    )
    recorded = tmp_path / "settings.json"
    dump_json({"design": design}, recorded)
    reloaded = Design(**json.loads(recorded.read_text())["design"])

    names = [src.file.name for src in reloaded.rtl.sources]
    assert names == [src.file.name for src in design.rtl.sources]
    assert sorted(names) == ["core.vhd", "top.vhd"]
    assert [src.file.name for src in reloaded.tb.sources] == ["tb.vhd"]
    assert reloaded.tb.top == design.tb.top
    assert reloaded.rtl_hash == design.rtl_hash
    assert reloaded.tb_hash == design.tb_hash


@pytest.mark.parametrize("pos", [-1, 0])
def test_a_file_the_design_and_its_dependency_both_list_is_compiled_once(tmp_path, pos):
    """A design and its dependency may both list a shared file (a package). The merge appended
    the dependency's sources as they were, so the design compiled the file twice -- and reloaded
    from its record, where the sources validator drops the repeat, it was another design."""
    root = tmp_path / "d"
    (root / "dep").mkdir(parents=True)
    (root / "top.vhd").write_text("-- top\n")
    (root / "dep" / "pkg.vhd").write_text("-- pkg\n")
    (root / "dep" / "core.vhd").write_text("-- core\n")
    (root / "dep" / "dep.toml").write_text(
        'name = "dep"\n[rtl]\nsources = ["pkg.vhd", "core.vhd"]\n'
    )
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["dep/pkg.vhd", "top.vhd"], "top": "top"},
        dependencies=[{"uri": str(root / "dep" / "dep.toml"), "rtl": {"pos": pos}}],
    )
    names = [src.file.name for src in design.rtl.sources]
    assert sorted(names) == ["core.vhd", "pkg.vhd", "top.vhd"]

    recorded = tmp_path / "settings.json"
    dump_json({"design": design}, recorded)
    reloaded = Design(**json.loads(recorded.read_text())["design"])
    assert [src.file.name for src in reloaded.rtl.sources] == names
    assert reloaded.rtl_hash == design.rtl_hash


@pytest.mark.parametrize(
    "path, value",
    [
        ("tb.top", ("tb",)),
        ("tb.sources", ["tb.vhd"]),
        ("language.vhdl.standard", "08"),
        ("language.vhdl.synopsys", True),
    ],
)
def test_an_assignment_inside_a_nested_model_is_recorded(tmp_path, monkeypatch, path, value):
    """`design.tb.top = ...` is ordinary use of a design, and whatever it assigns is the design's
    own: a record of it has to hold it. pydantic marks a field set only when it is assigned on its
    own model, so on a design that gave no `tb` (or no `language`) the assignment set a field of
    the default testbench, the design's `tb` stayed unset, and the record -- which leaves out what
    is unset -- dropped it."""
    (tmp_path / "top.vhd").write_text("-- top\n")
    (tmp_path / "tb.vhd").write_text("-- tb\n")
    monkeypatch.chdir(tmp_path)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["top.vhd"], "top": "top"})
    *parents, name = path.split(".")
    owner = design
    for parent in parents:
        owner = getattr(owner, parent)
    setattr(owner, name, value)
    assigned = design.model_dump(mode="json")

    recorded = tmp_path / "settings.json"
    dump_json({"design": design}, recorded)
    reloaded = Design(**json.loads(recorded.read_text())["design"])

    assert reloaded.model_dump(mode="json") == assigned
    assert (reloaded.rtl_hash, reloaded.tb_hash) == (design.rtl_hash, design.tb_hash)
    assert design.model_fields_set == {"name", "design_root", "rtl"}, "a dump changes nothing"


def _design_with_a_rom(root: Path, **parameters) -> Design:
    """Create a design containing a ROM source."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.vhd").write_text("-- top\n")
    (root / "rom.mem").write_text("00\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.vhd"], "top": "top", "parameters": parameters},
    )


def test_a_parameter_is_counted_by_its_one_value(tmp_path):
    """A parameter's value is the one thing a tool sees, so it is the one thing the design is
    counted by. A second record of "how it was written" kept beside it drifted: assigning the
    parameters to themselves rebuilt the record from absolute paths and made the hash depend on
    location, and a record reloaded with the design outranked a parameter overridden since."""
    design = _design_with_a_rom(tmp_path / "d", ROM={"file": "rom.mem"}, W=8)
    before = design.rtl_hash

    design.rtl.parameters = design.rtl.parameters
    assert design.rtl_hash == before, "assigning a value to itself changes nothing"

    design.rtl.generics = {**design.rtl.generics, "W": 16}
    assert design.rtl_fingerprint["parameters"]["ROM"] == "$DESIGN_ROOT/rom.mem"

    recorded = tmp_path / "settings.json"
    dump_json({"design": _design_with_a_rom(tmp_path / "d", ROM={"file": "rom.mem"})}, recorded)
    reloaded = json.loads(recorded.read_text())["design"]
    reloaded["rtl"]["parameters"]["ROM"] = 7
    assert Design(**reloaded).rtl_hash == _design_with_a_rom(tmp_path / "d", ROM=7).rtl_hash


def test_naming_an_artifact_after_a_source_keeps_two_sources_apart(tmp_path):
    """A flow that writes one file per source names it with `Design.source_artifact_name`.

    Sources are distinct files but their stems are not, so GHDL's `--out=verilog`, which writes
    one Verilog file per VHDL source, used to name both `rtl/a/fifo.vhd` and `rtl/b/fifo.vhd`
    `fifo.v` and write one over the other. This is naming only: it must not change the hash.
    """
    root = tmp_path / "d"
    for sub in ("rtl/a", "rtl/b"):
        (root / sub).mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    (root / "top.vhd").write_text("-- top\n")
    (root / "rtl" / "a" / "fifo.vhd").write_text("-- a\n")
    (root / "rtl" / "b" / "fifo.vhd").write_text("-- b\n")
    (tmp_path / "shared" / "pkg.vhd").write_text("-- pkg\n")

    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["top.vhd", "rtl/a/fifo.vhd", "rtl/b/fifo.vhd", "../shared/pkg.vhd"],
            "top": "top",
        },
    )
    names = [design.source_artifact_name(src, ".v") for src in design.rtl.sources]
    assert names == ["top.v", "rtl_a_fifo.v", "rtl_b_fifo.v", "shared_pkg.v"]
    assert len(set(names)) == len(names)


def test_artifact_names_are_distinct_even_where_folding_is_not(tmp_path):
    """Folding a path into one filename cannot be both readable and injective -- `my-fifo` and
    `my_fifo`, `fifo.vhd` and `fifo.vhdl`, and a dependency's `rtl/fifo.vhd` named relative to
    *its* root all fold alike -- so names that would collide carry a digest of their path, and
    every other name stays as readable as before."""
    root, lib = tmp_path / "main", tmp_path / "lib"
    for where in (root / "rtl", lib / "rtl"):
        where.mkdir(parents=True)
    for name in ("my-fifo.vhd", "my_fifo.vhd", "fifo.vhd", "fifo.vhdl", "alone.vhd"):
        (root / "rtl" / name).write_text(f"-- {name}\n")
    (lib / "rtl" / "fifo.vhd").write_text("-- the library's own fifo\n")
    (lib / "lib.toml").write_text('name = "lib"\n[rtl]\nsources = ["rtl/fifo.vhd"]\ntop = "fifo"\n')

    sources = ["rtl/my-fifo.vhd", "rtl/my_fifo.vhd", "rtl/fifo.vhd", "rtl/fifo.vhdl"]
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": [*sources, "rtl/alone.vhd"], "top": "top"},
        dependencies=[{"uri": str(lib / "lib.toml"), "rtl": {"pos": -1}}],
    )
    assert len(design.rtl.sources) == 6

    names = [design.source_artifact_name(src, ".v") for src in design.rtl.sources]
    assert len(set(names)) == len(names), names
    assert names[4] == "rtl_alone.v", "a name nothing collides with is left alone"
    # The same source always gets the same name, whichever list it is found through.
    assert design.source_artifact_name(design.rtl.sources[0], ".v") == names[0]


# ------------------------------------------- O-RI1: a bundled platform names no installation

# The imported package: under tox it is the installed one, not the checkout's `src/xeda`.
XEDA_PACKAGE = Path(xeda.__file__).parent


def _install_elsewhere(prefix: Path) -> Path:
    """Another installation's package directory, holding the bundled `nangate45` platform: what
    `-s platform=nangate45` resolves to under that installation."""
    package = prefix / "site-packages" / "xeda"
    shutil.copytree(XEDA_PACKAGE / "platforms" / "nangate45", package / "platforms" / "nangate45")
    return package


def _as_installed_at(monkeypatch, package: Path) -> None:
    """Make this process resolve bundled platforms, and its own installation, as `package`."""
    import xeda.platforms.platform
    import xeda.utils

    monkeypatch.setattr(xeda.utils, "XEDA_PACKAGE_ROOT", package, raising=False)
    monkeypatch.setattr(xeda.platforms.platform, "files", lambda _package: package / "platforms")


def _platform_settings(tmp_path: Path, platform: str, flow_name="yosys"):
    from xeda.flow_runner import get_flow_class

    root, start = tmp_path / "d", tmp_path / "start"
    root.mkdir(exist_ok=True)
    start.mkdir(exist_ok=True)
    return get_flow_class(flow_name).Settings.from_input(
        {"platform": platform, "clock": {"period": 2.0}}, design_root=root, runner_cwd=start
    )


@pytest.mark.parametrize("flow_name", ["yosys", "openroad"])
def test_a_bundled_platform_hashes_the_same_wherever_xeda_is_installed(
    tmp_path, monkeypatch, flow_name
):
    """O-RI1 (a), PCD23: `-s platform=nangate45` is one request under any installation. A
    validated platform holds absolute paths (`with_absolute_paths`), so its identity named the
    installation, and two machines never agreed on it."""
    here = _platform_settings(tmp_path, "nangate45", flow_name)
    expected = flowrun_hash(flow_name, here, "d")
    _as_installed_at(monkeypatch, _install_elsewhere(tmp_path / "elsewhere"))
    there = _platform_settings(tmp_path, "nangate45", flow_name)
    assert there.platform is not None
    assert there.platform.root_dir.is_relative_to(tmp_path / "elsewhere")  # really elsewhere
    assert flowrun_hash(flow_name, there, "d") == expected


@pytest.mark.parametrize("flow_name", ["yosys", "openroad"])
def test_the_hashed_settings_name_no_path_under_the_installation(tmp_path, flow_name):
    from xeda.flow.flow import identity_values

    values = json.dumps(
        identity_values(flow_name, _platform_settings(tmp_path, "nangate45", flow_name), "d"),
        default=str,
    )
    assert str(XEDA_PACKAGE) not in values and os.path.realpath(XEDA_PACKAGE) not in values
    assert "$XEDA/platforms/nangate45/" in values


@pytest.mark.parametrize("where", ["design", "outside"])
def test_a_user_s_platform_counts_by_its_own_place(tmp_path, where):
    """Only the installation became a root: a `config.toml` under the design root still counts
    relative to it, and one elsewhere as the absolute location it names."""
    from xeda.flow.flow import identity_values

    source = XEDA_PACKAGE / "platforms" / "nangate45"
    place = (tmp_path / "d" / "pdk") if where == "design" else (tmp_path / "pdks" / "pdk")
    shutil.copytree(source, place)
    values = json.dumps(
        identity_values("yosys", _platform_settings(tmp_path, str(place / "config.toml")), "d"),
        default=str,
    )
    if where == "design":
        assert "$DESIGN_ROOT/pdk/" in values and str(place) not in values
    else:
        assert str(place) in values and "$XEDA" not in values


@pytest.mark.parametrize(
    "linked,flow_name",
    [("installation", "yosys"), ("installation", "openroad"), ("design root", None)],
)
def test_a_root_counts_however_its_path_is_spelled(tmp_path, monkeypatch, linked, flow_name):
    """A root reached through a symbolic link is one root: a path under it counts relative to it
    whether it is spelled through the link or by the physical path. xeda imported through a
    linked prefix gave `platform=nangate45` `$XEDA/...`, while the resolved path of that very
    `config.toml` kept the absolute prefix -- another hash for one platform."""
    import xeda.utils
    from xeda.flow.flow import identity_values

    physical = tmp_path / "physical"
    link = tmp_path / "link"
    if linked == "installation":
        package = _install_elsewhere(physical)
        link.symlink_to(physical, target_is_directory=True)
        linked_package = link / package.relative_to(physical)
        _as_installed_at(monkeypatch, linked_package)
        assert xeda.utils.XEDA_PACKAGE_ROOT == linked_package
        by_name = _platform_settings(tmp_path, "nangate45", flow_name)
        by_path = _platform_settings(
            tmp_path, str(package / "platforms/nangate45/config.toml"), flow_name
        )
        assert flowrun_hash(flow_name, by_path, "d") == flowrun_hash(flow_name, by_name, "d")
        values = json.dumps(identity_values(flow_name, by_path, "d"), default=str)
        assert str(physical) not in values and "$XEDA/platforms/nangate45/" in values
    else:
        physical.mkdir()
        link.symlink_to(physical, target_is_directory=True)
        (physical / "c.xdc").write_text("\n")
        from xeda.flows import VivadoSynth

        def hashed(root: Path, xdc: Path, start: Path | None = None):
            settings = VivadoSynth.Settings.from_input(
                {"fpga": "xc7a100tftg256-2L", "xdc_files": [str(xdc)]},
                design_root=root,
                runner_cwd=start or tmp_path / "start",
            )
            return json.dumps(identity_values("vivado_synth", settings), default=str)

        for root, xdc in ((link, physical / "c.xdc"), (physical, link / "c.xdc")):
            values = hashed(root, xdc)
            assert "$DESIGN_ROOT/c.xdc" in values and str(tmp_path) not in values

        # Two roots, one of them reached through a link: the design root is the more specific
        # place whichever way the file is spelled. Trying each spelling of the path against
        # every root in turn made `$PWD/design/c.xdc` of one spelling and `$DESIGN_ROOT/c.xdc`
        # of the other: one file, two identities.
        (physical / "design").mkdir()
        (physical / "design" / "c.xdc").write_text("\n")
        for xdc in (link / "design" / "c.xdc", physical / "design" / "c.xdc"):
            values = hashed(physical / "design", xdc, start=link)
            assert "$DESIGN_ROOT/c.xdc" in values and "$PWD" not in values, values

        # Specificity is the place's, not its spelling's: a design root written as a short link
        # to a deep directory is still the more specific root there.
        deep = physical / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "c.xdc").write_text("\n")
        short = tmp_path / "short"
        short.symlink_to(deep, target_is_directory=True)
        for xdc in (short / "c.xdc", deep / "c.xdc"):
            values = hashed(short, xdc, start=physical / "a" / "b")
            assert "$DESIGN_ROOT/c.xdc" in values and "$PWD" not in values, values
