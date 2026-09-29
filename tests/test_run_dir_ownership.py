"""Every run directory is xeda's (D21): one the launcher chose under the run root it created,
whose every file is the run's. Every deletion -- the launcher's, a flow's, one a flow's tool
script would make -- goes through `RunDirectory.remove`/`clear`, or is one of the sites reviewed
here; a flow built directly holds an `unlaunched` directory, in which xeda deletes nothing.

Everything here runs in scratch copies under `tmp_path`, never in a directory holding real files.
"""

import ast
import os
import re
import shutil
from collections import Counter
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli

from .settings_samples import flow_classes
from .tool_utils import FAKE_TOOLS_DIR, use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"

INV_TOML = """name = "inv"
[rtl]
sources = ["inv.v"]
top = "inv"
clock.port = "clk"
"""
INV_V = "module inv(input clk, input a, output reg y); always @(posedge clk) y <= ~a; endmodule\n"


def _files(root: Path) -> dict:
    """Every file under `root` and its content, by relative path."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


# --- The ise_synth probe (P10), through the command line --------------------------------------


@pytest.mark.parametrize("tools", ["fake xtclsh", "no xtclsh"])
def test_ise_synth_from_the_design_directory_deletes_none_of_its_files(
    tmp_path, monkeypatch, tools
):
    """`xeda run ise_synth inv.toml --cwd`, started in the design's directory, deleted inv.toml,
    inv.v and an unrelated notes.txt. Without `--cwd` the run goes to ./xeda_run, and the
    directory keeps every file."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "inv.toml").write_text(INV_TOML)
    (work / "inv.v").write_text(INV_V)
    (work / "notes.txt").write_text("my notes\n")
    before = _files(work)
    if tools == "fake xtclsh":
        use_fake_tools(monkeypatch)
    else:
        monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.chdir(work)
    CliRunner().invoke(
        cli,
        [
            "run",
            "ise_synth",
            "inv.toml",
            "--clean",
            "-s",
            "fpga=xc6slx9-2-tqg144",
            "clock.period=10",
        ],
    )
    after = {k: v for k, v in _files(work).items() if not k.startswith("xeda_run/")}
    assert after == before, "the design directory changed"


# --- The sweep's tables: every flow, launched from the design's directory (Task 9) ---------------

#: The flows whose tools have a fake in tests/fake_tools (run under tclsh, `file delete` too).
FAKED = {
    "vivado_synth",
    "vivado_alt_synth",
    "vivado_project",
    "vivado_sim",
    "vivado_postsynth_sim",
    "vivado_power",
    "quartus",
    "ise_synth",
    "dc",
    "diamond_synth",
    "modelsim",
}

#: What each flow deletes by name in its run directory when it is xeda's, placed in the user's
#: directory where the flow would look: their files, which it must not touch.
CANARIES = [
    "notes.txt",  # anything of the user's
    "xsim.dir/canary.txt",  # vivado_sim: `file delete -force xsim.dir`
    "diamond_impl/canary.txt",  # diamond_synth: `file delete -force ${impl_dir}`
    "sim_build/canary.d",  # verilator: `*.d` under `sim_dir`
    "bobjs/canary.bo",  # bsc, bsc_sim: `*.bo` and `*.ba` under `bobj_dir`
    "bobjs/canary.ba",
    "gen_rtl/canary.use",  # bsc: a module an earlier run generated, and its `.use` file
    "gen_rtl/canary.v",
    "sim_build/canary.use",  # bsc_sim: the same, in `sim_dir`
    "sim_build/canary.v",
    "dump.vcd",  # bsc_sim: the waveform bsc's Verilator driver writes
    "wave.opt",  # ghdl_sim: `write_wave_opt`
    "reports/utilization.json",  # yosys: the reports it writes
    "reports/timing.rpt",
    "sqrt.totCap",  # openroad: what `extract_parasitics` leaves
    "work-obj08.cf",  # the GHDL flows: `ghdl remove` deletes the work library
]

#: Tool commands that delete files in the directory they run in: `ghdl remove` deletes the work
#: library and what GHDL built from it.
DELETING_COMMANDS = {"ghdl": {"remove", "--remove", "clean", "--clean"}}

#: settings that make a flow reach the deletions it has
EXTRA_SETTINGS = {
    "ghdl_sim": {"vcd": "dump.vcd"},
    "vivado_sim": {"saif": "sim.saif"},
    "bsc_sim": {"simulator": "verilator", "vcd": "bsc_sim.vcd"},
}


#: the design's sources for a flow whose tool does not read VHDL
SOURCES = {"bsc": (["Top.bsv"], "mkTop"), "bsc_sim": (["Top.bsv"], "mkTop")}
#: the design's testbench for a flow that does not run cocotb
TESTBENCHES = {"bsc_sim": {"sources": ["Tb.bsv"], "top": "mkTb"}}

#: The flows the sweep does not bring to their `run()` -- a dependency that fails with its tool
#: doing nothing, or platform files that are not shipped -- and why nothing is lost: none of
#: them deletes anything but through its run directory, as the sweep of the code checks.
UNREACHED = {
    "nextpnr": "its yosys_fpga dependency writes no netlist",
    "open_xc7": "its yosys_fpga dependency writes no netlist",
    "openfpgaloader": "its nextpnr dependency does not run",
    "openroad": "the asap7 platform's liberty files are not shipped",
    "vivado_power": "its vivado_postsynth_sim dependency finds no netlist",
}


def _design_directory(work: Path) -> Path:
    """The design's own directory: the sqrt design plus the bsc/bsc_sim sources."""
    work.mkdir(parents=True)
    for name in ("sqrt.vhdl", "sqrt.toml", "tb_sqrt.py"):
        shutil.copy(SQRT / name, work / name)
    (work / "Top.bsv").write_text("module mkTop(Empty); endmodule\n")
    (work / "Tb.bsv").write_text("module mkTb(Empty); endmodule\n")
    return work


def test_fake_tools_are_where_the_sweep_expects_them():
    for name in ("vivado", "xtclsh", "quartus_sh", "dc_shell", "diamondc", "vsim"):
        assert (FAKE_TOOLS_DIR / name).exists()
    assert FAKED <= {cls.name for cls, _ in flow_classes()}


#: What deletes, moves over or overwrites a file or a tree, called by its name: a method
#: (`path.unlink()`), a module's function (`os.remove`, `shutil.rmtree`), or one imported by
#: name (`from os import remove`). `replace` with one argument is `Path.replace`; with two,
#: `str.replace` -- but `os.replace` always.
DELETING_CALLS = {
    "unlink",
    "remove",
    "rmdir",
    "removedirs",
    "rmtree",
    "rename",
    "renames",
    "move",
    "replace",
    "truncate",
}


def _python_deletions(package: Path) -> Counter:
    """Every call in xeda's Python code, but `xeda.run_dir` (the checked operation itself)
    and calls on a `run_directory`, that deletes, moves over or overwrites a file by name --
    by (module, source line), counted: a second copy of a reviewed line is a new site."""
    found: Counter = Counter()
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if "__pycache__" in path.parts or relative == "run_dir.py":
            continue
        text = path.read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        imported = {  # names imported from os, shutil or pathlib that delete: `remove as rm`
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil", "pathlib")
            for alias in node.names
            if alias.name in DELETING_CALLS
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute):
                receiver = ast.unparse(function.value)
                if function.attr not in DELETING_CALLS or receiver.endswith("run_directory"):
                    continue
                one_argument = len(node.args) == 1 and not node.keywords
                if function.attr == "replace" and receiver != "os" and not one_argument:
                    continue  # str.replace(old, new)
            elif not (isinstance(function, ast.Name) and function.id in imported):
                continue
            found[(relative, lines[node.lineno - 1].strip())] += 1
    return found


#: Every deletion in xeda's Python code outside `RunDirectory`, reviewed: what it removes, and
#: why that is only ever xeda's own. A new one, or a second copy of one, fails the oracle.
REVIEWED_PY_DELETIONS = {
    ("digest.py", "marker.unlink(missing_ok=True)"): (1, "the clock marker it has just created"),
    ("flow_runner/trace.py", "(run_dir / TRACE_FILE).unlink(missing_ok=True)"): (1, "a trace"),
    ("flow_runner/trace.py", "temporary.unlink(missing_ok=True)"): (
        1,
        "a link or leftover at trace.json.tmp, xeda's reserved name, as itself",
    ),
    ("flow_runner/trace.py", "os.replace(temporary, path)"): (1, "trace.json.tmp over trace.json"),
    ("flow_runner/default_runner.py", "lock_file(p).unlink(missing_ok=True)"): (
        1,
        "the lock beside a run directory xeda has just removed",
    ),
    ("utils.py", "return path.rename(backup_path)"): (1, "a backup, to a name nothing has yet"),
    ("flows/openfpgaloader.py", "packed.replace(bitstream)"): (
        1,
        "the packed bitstream, inside the run directory (`run_directory.writable`)",
    ),
    ("flows/yosys/common.py", "flags.remove(flag)"): (1, "a list of flags"),
    ("proc_utils.py", "readable.remove(fd)"): (1, "a list of file descriptors"),
    ("deliver.py", "os.replace(temporary, destination)"): (
        1,
        "the delivery's own temporary file, renamed over the destination the rules allowed",
    ),
    (
        "deliver.py",
        "temporary.unlink(missing_ok=True)  # its own temporary file, never anything else",
    ): (
        2,
        "its own temporary file, after a failed copy or a failed re-check",
    ),
    ("deliver.py", "os.replace(temporary, self.record_path)"): (
        1,
        "the delivery record's temporary file, beside the run directory",
    ),
}


def test_nothing_deletes_but_through_the_run_directory():
    """A mechanical oracle over all of xeda's Python code, by its syntax tree: nothing deletes,
    moves over or overwrites a file or a tree but through `RunDirectory` (`xeda.run_dir`), or
    one of the sites reviewed in `REVIEWED_PY_DELETIONS` -- each counted, so a copy of one is a
    new site."""
    import xeda

    found = _python_deletions(Path(xeda.__file__).parent)
    expected = Counter({site: count for site, (count, _why) in REVIEWED_PY_DELETIONS.items()})
    assert found == expected, "not reviewed, reviewed but gone, or not as often"


def test_the_oracle_sees_what_deletes():
    """The oracle's teeth: each way of deleting by name is seen, the second copy of a reviewed
    line too; `str.replace` and a list's `remove` of a reviewed line are not new."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        package = Path(scratch)
        (package / "a.py").write_text(
            "import os, shutil\n"
            "from os import remove as rm\n"
            "from pathlib import Path\n"
            "p = Path('x')\n"
            "p.unlink()\n"
            "p.unlink()\n"
            "os.removedirs(p)\n"
            "os.replace(p, p)\n"
            "shutil.move(p, p)\n"
            "rm(p)\n"
            "p.replace(p)\n"
            "'text'.replace('t', 'x')\n"
            "self.run_directory.remove(p)\n"
        )
        found = _python_deletions(package)
    assert found == Counter(
        {
            ("a.py", "p.unlink()"): 2,
            ("a.py", "os.removedirs(p)"): 1,
            ("a.py", "os.replace(p, p)"): 1,
            ("a.py", "shutil.move(p, p)"): 1,
            ("a.py", "rm(p)"): 1,
            ("a.py", "p.replace(p)"): 1,
        }
    )


#: In a tool script (every file of xeda's that is not Python: templates, the OpenROAD scripts,
#: the platforms' scripts): a deletion (`file delete`, `file rename`, `rm`), or a tool command
#: told to replace or overwrite what is there (`-force`, `-overwrite`).
SCRIPT_DELETION = re.compile(r"\brm\b|\bfile\s+(delete|rename)\b|(?<![\w-])-(force|overwrite)\b")
#: a tool command a flow runs that deletes where it runs (`ghdl remove`)
TOOL_DELETION = re.compile(r"\.run\(\s*[\"'](--)?(remove|clean)[\"']")

_REPORT = "a report, output or checkpoint the tool writes under its own name in the flow's "
_REPORT += "reports/outputs/checkpoints directory"
_BITSTREAM = "the bitstream named by the flow's `bitstream` setting"
#: a command that replaces by name, or deletes where it runs: only ever in the flow's run directory
IN_THE_RUN_DIRECTORY = "inside the flow's run directory, which is xeda's (D21)"

#: Every such line, reviewed, with how many times it occurs: (file, line) -> (count, why it
#: removes or replaces only xeda's own, and -- for a command that replaces by name or deletes
#: where it runs -- the flow module and the text of the guard in it, which must be there).
REVIEWED_SCRIPT_DELETIONS: dict = {
    (
        "flows/ghdl/__init__.py",
        'self.ghdl.run("remove", *ss.get_flags(vhdl, "remove", backend=backend))',
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/vivado/templates/vivado_synth.tcl",
        "create_project -part $fpga_part -force -verbose ${project_name}",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/vivado/templates/vivado_project.tcl",
        "create_project {% if settings.fpga and settings.fpga.part -%} -part "
        '{{settings.fpga.part}} {%- endif %} -force -verbose "$project_name"',
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/quartus/templates/create_project.tcl",
        "project_new ${design_name} -overwrite",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/dc/templates/dc_script.tcl",
        "write_icc2_files -force -output $OUTPUTS_DIR/icc2_files",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    ("flows/dc/templates/dc_script.tcl", "if { [catch {uniquify -force} -errorinfo err] } {"): (
        1,
        "uniquifies the design in memory: no file",
        None,
    ),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_synth",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_place",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_route",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_synth/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_place/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_verilog -mode funcsim -force ${settings.outputs_dir}/impl_funcsim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_sdf -mode timesim -process_corner slow -force -file ${settings.outputs_dir}/impl_timesim.sdf",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_verilog -mode timesim -sdf_anno false -force -file ${settings.outputs_dir}/impl_timesim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file "
        "${settings.outputs_dir}/impl_funcsim_xlib.vhd",
    ): (1, "a comment", None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_xdc -no_fixed_only -force ${settings.outputs_dir}/impl.xdc",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_bitstream -force { {{-settings.bitstream-}} }",
    ): (1, _BITSTREAM, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${reports_dir} utilization.xml] -format xml",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${reports_dir} hierarchical_utilization.xml] "
        "-format xml -hierarchical",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${reports_dir} utilization.rpt]",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${reports_dir} hierarchical_utilization.rpt] "
        "-hierarchical_percentages -hierarchical",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_qor_suggestions -quiet -strategy_dir  ./strategy_suggestions -force ./qor_suggestions.rqs",
    ): (
        1,
        "in the implementation run's own directory, <design>.runs/impl_1: part of the project "
        "`create_project` replaces and xeda owns",
        None,
    ),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_verilog -mode timesim -sdf_anno false -force -file ${outputs_dir}/timesim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_sdf -mode timesim -process_corner slow -force -file ${outputs_dir}/timesim.min.sdf",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_sdf -mode timesim -process_corner fast -force -file ${outputs_dir}/timesim.max.sdf",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_vhdl -mode funcsim -include_xilinx_libs -write_all_overrides -force -file "
        "${outputs_dir}/funcsim.vhdl",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_xdc -no_fixed_only -force ${outputs_dir}/impl.xdc",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_bitstream -force { {{-settings.bitstream-}} }",
    ): (1, _BITSTREAM, None),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lib"): (
        1,
        "an upstream " "PDK helper script xeda never runs",
        None,
    ),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lef"): (
        1,
        "an upstream " "PDK helper script xeda never runs",
        None,
    ),
    (
        "platforms/nangate45/fakeram.tcl",
        "file copy -force $results_dir/fakeram45_$size/fakeram45_$size.lib $flow_dir/lib/fakeram45_$size.lib",
    ): (1, "an upstream PDK helper script xeda never runs", None),
    (
        "platforms/nangate45/fakeram.tcl",
        "file copy -force $results_dir/fakeram45_$size/fakeram45_$size.lef $flow_dir/lef/fakeram45_$size.lef",
    ): (1, "an upstream PDK helper script xeda never runs", None),
    (
        "flows/diamond/templates/synth.tcl",
        "prj_project new -name {{design.name}} -dev {{settings.fpga.part|tcl_word}} -impl "
        "${implementation_name} -impl_dir ${impl_dir}",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    ("flows/ise/templates/ise_synth.tcl", "if { [catch  { project new {{design.name}} }] } {"): (
        1,
        IN_THE_RUN_DIRECTORY,
        None,
    ),
}

#: Commands that replace a project or a directory by its name, whatever their options: each is
#: reviewed above.
SCRIPT_REPLACEMENT = re.compile(
    r"\bcreate_project\b|\bproject_new\b|\bprj_project\s+new\b|\bproject\s+new\b|"
    r"\bwrite_icc2_files\b"
)


def test_every_tool_command_that_deletes_or_replaces_is_reviewed_and_guarded():
    """A mechanical oracle over every file of xeda's that is not Python -- tool scripts, the
    OpenROAD and platform scripts -- and every tool command in a flow's code: each line that
    deletes (`file delete`, `file rename`, `rm`), tells a tool to replace or overwrite
    (`-force`, `-overwrite`), replaces by name (`create_project`, `project_new`, ...) or runs a
    deleting tool command (`ghdl remove`) is reviewed in `REVIEWED_SCRIPT_DELETIONS`, as often as
    it occurs; one that replaces by name or deletes where it runs has its guard in its flow, if
    it needs one."""
    import xeda

    package = Path(xeda.__file__).parent
    found: Counter = Counter()
    for path in sorted(package.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".gz"):
            continue
        pattern = TOOL_DELETION if path.suffix == ".py" else None
        for line in path.read_text(errors="ignore").splitlines():
            text = line.strip()
            if pattern is not None:
                hit = pattern.search(text) and not text.startswith("#")
            else:
                hit = SCRIPT_DELETION.search(text) or SCRIPT_REPLACEMENT.search(text)
            if hit:
                found[(path.relative_to(package).as_posix(), text)] += 1
    expected = Counter(
        {site: count for site, (count, _why, _guard) in REVIEWED_SCRIPT_DELETIONS.items()}
    )
    assert found == expected, "not reviewed, reviewed but gone, or not as often"
    for _count, _why, guard in REVIEWED_SCRIPT_DELETIONS.values():
        if guard is not None:
            module, text = guard
            assert text in (package / module).read_text(), f"{module} lost its guard {text!r}"
