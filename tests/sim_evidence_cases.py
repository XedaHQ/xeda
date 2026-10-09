"""Behavioral oracle inventory and narrow build/runtime stand-ins.

The real flow's run method and launcher execute unchanged. Builds succeed; runtime invocations
execute a separate Python process which leaves a call marker independently of evidence. Each
simulator family extends these stand-ins with its native transcript/checkpoint contracts.
Power is not in this inventory: it reports power from successful declared activity,
and the postsynth simulator checks evidence before hand-over.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


from xeda import Design
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.proc_utils import run_process
from xeda.tool import Tool

from .settings_samples import minimal_settings
from .tool_utils import fake_calls, use_fake_tools


@dataclass(frozen=True)
class SimCase:
    flow: str
    backend: str | None = None

    @property
    def name(self) -> str:
        return self.flow + (f"/{self.backend}" if self.backend else "")


SIMULATORS = (
    "ghdl_sim",
    "nvc",
    "modelsim",
    "vcs",
    "vivado_sim",
    "vivado_postsynth_sim",
    "yosys_sim",
    "verilator",
    "bsc_sim",
)
BSC_BACKENDS = (
    "bluesim",
    "verilator",
    "iverilog",
    "cvc",
    "cver",
    "isim",
    "modelsim",
    "ncverilog",
    "questa",
    "vcs",
    "vcsi",
    "veriwell",
    "xsim",
)
REJECTED_BSC_BACKENDS = {"cvc", "cver", "isim", "ncverilog", "veriwell"}
CASES = [SimCase(name) for name in SIMULATORS if name != "bsc_sim"] + [
    SimCase("bsc_sim", backend) for backend in BSC_BACKENDS if backend not in REJECTED_BSC_BACKENDS
]


def launch_case(
    case: SimCase,
    work: Path,
    monkeypatch,
    *,
    positive: bool = False,
    settings_overrides=None,
    foreign_source: bool = False,
):
    """Launch a real flow against successful build and silent/positive runtime stand-ins."""
    work.mkdir(parents=True, exist_ok=True)
    source = work / ("Top.bsv" if case.backend else "tb.sv")
    source.write_text(
        "module mkTop(Empty); endmodule\n" if case.backend else "module tb; endmodule\n"
    )
    if case.flow in ("ghdl_sim", "nvc"):
        source = work / "tb.vhdl"
        source.write_text("entity tb is end; architecture rtl of tb is begin end;\n")
    top = "mkTop" if case.backend else "tb"
    design = Design(
        name="oracle",
        design_root=work,
        rtl={"sources": [source], "top": top, "clock": {"port": "clk"}},
        tb={"sources": [source], "top": top, "uut": "dut"},
        language={"vhdl": {"standard": "2008"}},
    )
    if foreign_source:
        foreign = work / "foreign.c"
        foreign.write_text("int foreign(void) { return 1; }\n")
        design.rtl.sources = [*design.rtl.sources, foreign]
    flow_class = get_flow_class(case.flow)
    settings = minimal_settings(flow_class)
    if case.backend:
        settings["simulator"] = case.backend
    settings.update(settings_overrides or {})
    # Clear invocation proof too: a build failure on a rerun must not inherit the old marker.
    for marker in (work / "runs").rglob("oracle.runtime"):
        marker.unlink()
    use_fake_tools(monkeypatch)
    monkeypatch.setattr(Tool, "version_gte", lambda self, *args: True)
    monkeypatch.setattr(
        Tool,
        "probe_stdout",
        lambda self, *args, **kwargs: "GHDL 6.0.0\nCompiled with GNAT\nmcode code generator\n"
        "Bluespec Compiler, version 2026.07.1\nVerilator 5.048\nVERILATOR_ROOT = /oracle/verilator\n",
    )
    if case.flow == "vcs" or case.backend in ("vcs", "vcsi"):
        use_fake_vcs(monkeypatch)
    original = Tool.execute
    calls = []
    runtime_options = []

    def execute(tool, executable, *args, **kwargs):
        words = [str(a) for a in args]
        name = Path(executable).name
        cwd = Path.cwd()
        calls.append((executable, words))
        if name == "vivado" and "-source" in words:
            script = Path(words[words.index("-source") + 1])
            if script.name != "vivado_sim.tcl":
                result = original(tool, executable, *args, **kwargs)
                # The existing Tcl fake records output commands but does not materialize
                # their netlists/checkpoints. Supply exactly what this successful build wrote.
                for command in fake_calls(cwd, elements=True):
                    if command[0] in ("write_verilog", "write_sdf", "write_checkpoint"):
                        target = Path(command[-1])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("module tb; endmodule\n")
                    if command[0] == "report_power":
                        target = Path(command[command.index("-file") + 1])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            '<report><section title="Summary"><table><tablerow>'
                            '<tablecell contents="Total On-Chip Power (W)"/>'
                            '<tablecell contents="0.5"/>'
                            "</tablerow></table></section></report>"
                        )
                return result
        runtime = (
            (name == "ghdl" and "run" in words)
            or (name == "nvc" and "-r" in words)
            or name == "vsim"
            or (name == "vcs" and "-R" in words)
            or name in ("simv", "top", "mkTop", "tb", "vvp", "xsim")
            or (name == "vivado" and "-source" in words)
        )
        if runtime:
            runtime_options.append(kwargs)
            (cwd / "oracle.runtime").unlink(missing_ok=True)
        if case.backend in ("verilator", "iverilog") and (name == "mkTop" or name == "vvp"):
            if name == "vvp":
                assert "-m" in words and Path(words[words.index("-m") + 1]).is_file()
                binary = next(w for w in words if Path(w).name == "mkTop")
                return run_process(
                    sys.executable,
                    [binary],
                    env=kwargs.get("env"),
                    tee=kwargs.get("tee"),
                    timeout=10,
                    merge_stderr=True,
                )
            return original(tool, executable, *args, **kwargs)
        if case.flow == "vcs" and name in ("vlogan", "vhdlan", "vcs", "simv"):
            monkeypatch.setenv("XEDA_FAKE_VCS_STATE", "finish5" if positive else "silent")
            result = original(tool, executable, *args, **kwargs)
            if name == "simv" or name == "vcs" and "-R" in words:
                assert (cwd / "fake_vcs.runtime").read_text() == "runtime executed"
                (cwd / "oracle.runtime").write_text("runtime executed")
            else:
                (cwd / "oracle.build").write_text("analysis/elaboration succeeded")
            return result
        if case.backend in ("vcs", "vcsi") and Path(executable).parent.name == "sim_build":
            Path(executable).unlink(missing_ok=True)
            Path(executable).symlink_to(FAKE.resolve())
            monkeypatch.setenv("XEDA_FAKE_VCS_STATE", "finish5" if positive else "silent")
            return use_fake_vcs_execute(original, tool, executable, args, kwargs)
        if (case.flow == "modelsim" or case.backend in ("modelsim", "questa")) and name == "vsim":
            monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", "finish0" if positive else "silent")
            result = original(tool, executable, *args, **kwargs)
            assert (cwd / "fake_vsim.runtime").read_text().strip() == "runtime executed"
            (cwd / "oracle.runtime").write_text("runtime executed")
            return result
        if case.backend == "xsim" and name == "xsim":
            script = Path(words[words.index("-tclbatch") + 1])
            (cwd / "fake_xsim.args.json").write_text(json.dumps(words))
            log_path = Path(words[words.index("-log") + 1])
            if "+proof" in (settings_overrides or {}).get("sim_args", []):
                assert "+proof" in words
            model = cwd / "fake_xsim_runner.tcl"
            finish = (
                'puts {$finish called at time : 5 ns : File "Top.v" Line 1}' if positive else ""
            )
            model.write_text(
                f"set xeda_channel [open {{{log_path}}} w]\n"
                "rename puts __xsim_puts\n"
                "proc puts {args} {__xsim_puts $::xeda_channel [lindex $args end]; flush $::xeda_channel}\n"
                "proc current_time {} {return {5 ns}}\n"
                "proc current_sim {} {return simulation}\n"
                "proc get_property {name sim} {return {1 ps}}\n"
                f"proc run {{args}} {{{finish}}}\n"
                f"source {{{script}}}\n"
                "close $xeda_channel\n"
            )
            if subprocess.run(["tclsh", str(model)], cwd=cwd, timeout=10).returncode:
                return 1
            (cwd / "fake_xsim.runtime").write_text("runtime executed")
            (cwd / "oracle.runtime").write_text("runtime executed")
            return ""
        if name == "vivado" and "-source" in words and case.flow.startswith("vivado"):
            monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", "finish0" if positive else "silent")
            result = original(tool, executable, *args, **kwargs)
            assert (cwd / "fake_xsim.runtime").is_file()
            (cwd / "oracle.runtime").write_text("runtime executed")
            return result
        if runtime:
            # Combined proprietary/NVC invocations also contain the successful build.
            build = cwd / "oracle.build"
            if name in ("nvc", "vsim", "vcs", "vivado"):
                build.write_text("analysis/elaboration succeeded\n")
            assert build.exists(), f"runtime reached without a successful build: {calls}"
            record = next(
                (
                    w.split("+xeda+end_record+", 1)[1]
                    for w in words
                    if w.startswith("+xeda+end_record+")
                ),
                None,
            )
            marker = cwd / "oracle.runtime"
            program = "from pathlib import Path\n"
            program += f"Path({str(marker)!r}).write_text('runtime executed')\n"
            if positive and record:
                program += f"Path({record!r}).write_text({json.dumps({'ended_by': 'finish', 'time': 0, 'time_unit': '1ps', 'events': []})!r})\n"
            if positive and case.backend == "bluesim":
                program += (
                    'Path(\'bluesim_events.jsonl\').write_text(\'{"kind":"finish","time":0}\\n\')\n'
                )
                program += 'Path(\'bluesim_end.json\').write_text(\'{"ended_by":"unknown","time":0,"time_unit":"1us","cycles":1,"events":[]}\')\n'
            if positive and case.flow == "ghdl_sim":
                program += "print('simulation finished @0ms', flush=True)\n"
            if positive and case.flow == "yosys_sim":
                program += "Path('cxxrtl_events.jsonl').write_text('')\n"
                program += (
                    'Path(\'cxxrtl_end.json\').write_text(\'{"ended_by":"exit","events":[]}\')\n'
                )
            if case.flow == "nvc":
                program += "print('XEDA_NVC_RUNTIME_START', flush=True)\n"
                if positive:
                    program += "print('lib/std.08/env-body.vhd:42:9: note: 0ms+0: FINISH called', flush=True)\n"
                    program += 'Path(\'nvc_end.json\').write_text(\'{"time": 0, "time_unit": "1fs", "next_time": null}\')\n'
            return run_process(
                sys.executable,
                ["-c", program],
                tee=kwargs.get("tee"),
                timeout=10,
                merge_stderr=True,
            )
        (cwd / "oracle.build").write_text("analysis/elaboration succeeded\n")
        if name == "yosys-config":
            return str(work / "include")
        if name == "bsc" and "-o" in words and case.backend in ("verilator", "iverilog"):
            target = Path(words[words.index("-o") + 1])
            if case.backend == "verilator":
                from xeda.flows.verilator import HOOK_MACROS

                assert all("-D" + macro in words for macro in HOOK_MACROS)
                assert "-Xl" in words
                assert Path(words[words.index("-Xl") + 1]).is_file()
            program = "#!" + sys.executable + "\nimport json, os\nfrom pathlib import Path\n"
            program += "Path('oracle.runtime').write_text('runtime executed')\n"
            if case.backend == "iverilog":
                program += "print('XEDA_ICARUS_RUNTIME_START', flush=True)\n"
            if positive:
                record = {
                    "ended_by": "exit" if case.backend == "verilator" else "finish",
                    "time": None if case.backend == "verilator" else 5,
                    "time_unit": "1ps",
                    "events": [{"kind": "finish", "time": 5}],
                }
                program += (
                    f"Path(os.environ['XEDA_END_RECORD']).write_text({json.dumps(record)!r})\n"
                )
            target.write_text(program)
            target.chmod(0o755)
            return ""
        if name in ("g++", "c++", "cc", "bsc") and "-o" in words:
            if case.flow == "yosys_sim":
                assert "cxxrtl_evidence.cpp" in words
                assert "-include" in words and "cxxrtl_evidence.h" in words
                assert Path("cxxrtl_evidence.cpp").is_file()
            target = Path(words[words.index("-o") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            # A preceding fake runtime linked this build output to its shared executable.
            # Rebuild the output itself, never overwrite the fixture through that link.
            target.unlink(missing_ok=True)
            target.write_text("fake build output\n")
            if name == "bsc" and case.backend in ("modelsim", "questa"):
                library = cwd / f"work_{top}"
                library.mkdir(exist_ok=True)
                (library / "library.dat").write_text("fake compiled library\n")
            if name == "bsc" and case.backend == "xsim":
                (target.parent / (target.name + ".xsim")).write_text("fake linked snapshot\n")
        return ""

    monkeypatch.setattr(Tool, "execute", execute)
    if case.flow == "vivado_postsynth_sim":
        design.flow = {"vivado_synth": {"fpga": "xc7a100tcsg324-1", "clock": {"period": 5.0}}}
    flow = DefaultRunner(work / "runs", display_results=False, rebuild_all=True).run_flow(
        flow_class, design, settings
    )
    markers = list((work / "runs").rglob("oracle.runtime"))
    assert markers, f"{case.name} never invoked a runtime; calls: {calls}"
    assert all(p.read_text() == "runtime executed" for p in markers)
    assert flow is not None
    assert runtime_options, f"{case.name} has no observed runtime invocation"
    assert all(options.get("timeout") == settings.get("timeout") for options in runtime_options)
    return flow


def use_fake_vcs_execute(original, tool, executable, args, kwargs):
    """Route bsc's linked VCS image through the same executable UCLI stand-in as VCSFlow."""
    fake = FAKE
    target = Path.cwd() / (Path(executable).name + ".py")
    if not target.exists():
        target.symlink_to(fake.resolve())
    result = run_process(
        sys.executable,
        [str(target), *args],
        env={**os.environ, **(kwargs.get("env") or {})},
        tee=kwargs.get("tee"),
        timeout=kwargs.get("timeout"),
        merge_stderr=kwargs.get("merge_stderr", False),
    )
    Path("oracle.runtime").write_text("runtime executed")
    return result


FAKE = Path(__file__).parent / "fake_tools" / "vcs_runtime.py"


def use_fake_vcs(monkeypatch):
    """Run real child processes, including Tcl scripts and both build/runtime phases."""
    original = Tool.execute

    def execute(tool, executable, *args, **kwargs):
        name = Path(executable).name
        if name not in ("vlogan", "vhdlan", "vcs", "simv"):
            return original(tool, executable, *args, **kwargs)
        # argv[0] selects the tool; the fake executable lives in this run directory.
        target = Path.cwd() / (name + ".py")
        if not target.exists():
            target.symlink_to(FAKE.resolve())
        return run_process(
            sys.executable,
            [str(target), *args],
            env={**os.environ, **(kwargs.get("env") or {})},
            tee=kwargs.get("tee"),
            timeout=kwargs.get("timeout"),
            merge_stderr=kwargs.get("merge_stderr", False),
        )

    monkeypatch.setattr(Tool, "execute", execute)
