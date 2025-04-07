from __future__ import annotations

import logging
from functools import cached_property
from pathlib import Path
from typing import List, Literal, Optional, Union

from ...dataclass import Field
from ...design import DesignSource, SourceType, VhdlSettings
from ...flow import SimFlow
from ...tool import Tool
from ...utils import common_root

log = logging.getLogger(__name__)


class NvcTool(Tool):
    """NVC VHDL simulation, synthesis, and linting tool: https://github.com/nickg/nvc"""

    # docker: Optional[Docker] = None
    executable: str = "nvc"


class Nvc(SimFlow):
    """Simulate a VHDL design using NVC"""

    cocotb_sim_name = "nvc"

    class Settings(SimFlow.Settings):
        one_shot: bool = Field(
            True,
            description="Run the analysis, elaboration, and execution in a single command. Set to False to run each step separately, which might be helpful in.",
        )
        ignore_time: bool = Field(
            False,
            description="Do not check the timestamps of source files when the corresponding design unit is loaded from a library.",
        )
        heap_size: Optional[str] = Field(
            None,
            description="Set the maximum size in bytes of the simulation heap. This area of memory is used for temporary allocations during process execution and dynamic allocations by the VHDL ‘new’ operator. The size parameter takes an optional k, m, or g suffix to indicate kilobytes, megabytes, and gigabytes respectively. The default size is 16 megabytes.",
        )
        messages: Optional[Literal["full", "compact"]] = Field(
            None,
            description="Select the format used for printing error and informational messages. The default full message format is designed for readability whereas the compact messages can be easily parsed by tools.",
        )
        std_error: Optional[Literal["note", "warning", "error", "failure"]] = Field(
            None,
            description="Print error messages with the given severity or higher to ‘stderr’ instead of ‘stdout’. The default is to print all messages to ‘stderr’. Valid levels are note, warning, error, and failure.",
        )
        werror: bool = Field(
            False, alias="warn_error", description="warnings are always considered as errors"
        )
        work: Optional[str] = Field(None, description="Set the name of the WORK library")
        # clean: bool = Field(False, description="Run 'clean' before elaboration")
        ## analysis flags
        analysis_flags: List[str] = []
        check_synthesis: bool = Field(
            True,
            description="Issue warnings for common coding mistakes that may cause problems during synthesis such as missing signals from process sensitivity lists.",
        )
        psl_in_comments: Optional[bool] = Field(
            None, description="Enable parsing of PSL directives in comments during analysis."
        )
        relaxed: bool = Field(
            False,
            description="Disable certain pedantic LRM conformance checks or rules that were relaxed by later standards.",
        )
        ## elaboration flags
        elab_flags: List[str] = []
        cover: List[str] = Field([], description="Enable code coverage reporting ")
        cover_spec: Optional[Path] = Field(
            None, description="Specify the coverage specification file"
        )
        jit: bool = Field(
            False,
            description="""Normally nvc compiles all code ahead-of-time during elaboration. The --jit option defers native code generation until run-time where each function will be compiled separately on a background thread once it has been has been executed often enough in the interpreter to be deemed worthwhile. This dramatically reduces elaboration time at the cost of increased memory and CPU usage while the simulation is executing. This option is beneficial for short-running simulations where the performance gain from ahead-of-time compilation is not so significant.""",
        )
        no_collapse: bool = Field(
            False,
            description="Do not collapse ports into a single signal. Normally if a signal at one level in the hierarchy is directly connected to another signal in a lower level via a port map, the signals are “collapsed” and only the signal in the upper level is preserved. The --no-collapse option disables this optimization and preserves both signals. This improves debuggability at the cost of some performance.",
        )
        no_save: bool = Field(
            False,
            description="Do not save the elaborated design to a file. Normally nvc saves the elaborated design to a file in the work library. This file is used by the simulator to load the design quickly. The --no-save option disables this saving and the simulator will have to re-elaborate the design each time it is run. This option is useful for debugging the elaboration process.",
        )
        optimization_level: Optional[Literal[0, 1, 2, 3]] = Field(
            3,
            description="Set the optimization level. The default is 0. Higher levels may improve simulation performance but may also increase elaboration time. The maximum level is 3.",
        )
        print_verbose: bool = Field(
            False, description="Prints resource usage information after each elaboration step."
        )
        ## run flags
        run_flags: List[str] = []
        ieee_warnings: Optional[bool] = Field(
            None,
            description="Enable or disable warning messages from the standard IEEE packages. The default is warnings enabled.",
        )
        wave: Union[None, bool, str, Path] = Field(
            None,
            description="Write waveform data to a file. The default is to not write waveform data.",
        )
        wave_format: Optional[Literal["vcd", "fst"]] = Field(
            None,
            description="Generate waveform data in this format. The default is FST if this option is not provided and `wave` is not a filename. If this option is None `wave` is a filename, the format is selected automatically based on the file extension.",
        )
        wave_arrays: Union[int, bool, None] = Field(
            2048,
            description="Include memories and nested arrays in the waveform data. This is disabled by default as it can have significant performance, memory, and disk space overhead. With optional argument N only arrays with up to this many elements will be dumped.",
        )
        wave_include_glob: Optional[str] = Field(
            None,
            description="""Include signals matching this glob pattern in the waveform data.
            Examples: ‘:top:*:x’  ‘*:x’  ‘:top:sub:*’
            See https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS for more details.""",
        )
        wave_exclude_glob: Optional[str] = Field(
            None,
            description="""Exclude signals matching this glob pattern from the waveform data.
            Examples: ‘:top:*:x’  ‘*:x’  ‘:top:sub:*’
            See https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS for more details.""",
        )
        exit_severity: Optional[Literal["note", "warning", "error", "failure"]] = Field(
            None,
            description="Terminate the simulation after an assertion failures of severity greater than or equal to level.",
        )
        stop_delta: Optional[int] = Field(
            None,
            description="Stop the simulation after N delta cycles in the same current time.",
        )
        vhpi: Optional[str] = Field(
            None, description="Specify the VHPI library to load at startup."
        )
        shuffle: bool = Field(
            False,
            description="""Run processes in random order.
            The VHDL standard does not specify the execution order of processes and different simulators may exhibit subtly different orderings.
            This option can help to find and debug code that inadvertently depends on a particular process execution order.
            This option should only be used during debug as it incurs a significant performance overhead as well as introducing potentially non-deterministic behavior.""",
        )
        stats: bool = Field(
            False,
            description="Print a summary of the time taken and memory used at the end of the run.",
        )

    @cached_property
    def nvc(self):
        return NvcTool()  # pyright: ignore[reportCallIssue]

    def init(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.wave and isinstance(ss.wave, (str, Path)):
            ss.wave = self.process_path(ss.wave)

    def global_options(self) -> List[str]:
        cf: List[str] = []
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.ignore_time:
            cf.append("--ignore-time")
        if ss.heap_size:
            cf += ["-H", ss.heap_size]
        cf += [f"-L{p}" for p in ss.lib_paths]
        if ss.messages:
            cf.append(f"--messages={ss.messages}")
        if ss.std_error:
            cf.append(f"--std-error={ss.std_error}")

        # The default standard revision is VHDL-2008
        vhdl = self.design.language.vhdl
        if vhdl.standard:
            if vhdl.standard == "93":
                vhdl.standard = "1993"
            elif vhdl.standard == "00":
                vhdl.standard = "2000"
            elif vhdl.standard == "02":
                vhdl.standard = "2002"
            elif vhdl.standard == "08":
                vhdl.standard = "2008"
            elif vhdl.standard == "19":
                vhdl.standard = "2019"
            assert vhdl.standard in (
                "1993",
                "2000",
                "2002",
                "2008",
                "2019",
            ), f"Invalid VHDL standard: {vhdl.standard}"
            cf.append(f"--std={vhdl.standard}")
        if ss.work:
            cf.append(f"--work={ss.work}")
        return cf

    def init_lib(self) -> None:
        """Initialize the library
        Initialise the working library directory.
        This is not normally necessary as the library will be automatically created when using other commands such as `analyze`.
        """
        self.nvc.run("--init")

    def analyze_flags(self) -> list:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = ss.analysis_flags
        if ss.psl_in_comments:
            flags.append("--psl")
        if ss.relaxed:
            flags.append("--relaxed")
        for k, v in self.design.tb.defines.items():
            assert v is not None
            flags += ["-D", f"{k}={v}"]
        if ss.check_synthesis:
            flags.append("--check-synthesis")
        return flags

    def analyze(self, sources=None) -> None:
        """
        Analyse one or more files into the work library
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if sources is None:
            sources = self.design.sim_sources_of_type(SourceType.Vhdl)
        self.nvc.run(*self.global_options(), "-a", *sources, *self.analyze_flags())

    def elaborate_flags(self) -> List[str]:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = ss.elab_flags

        # Note: Generics in internal instances can be overridden by giving the full dotted path to the generic.
        for k, v in self.design.tb.parameters.items():
            assert v is not None
            flags += ["-g", f"{k}={v}"]

        if ss.optimization_level is not None:
            flags.append(f"-O{ss.optimization_level}")
        if ss.jit:
            flags.append("--jit")
        if ss.no_collapse:
            flags.append("--no-collapse")
        if ss.no_save:
            flags.append("--no-save")
        if ss.print_verbose:
            flags.append("--verbose")
        if ss.cover:
            flags += [f"--cover={','.join(ss.cover)}"]
        if ss.cover_spec:
            flags += ["--cover-spec", str(ss.cover_spec)]
        return flags

    def elaborate(self):
        """
        Elaborate a previously analysed top level design unit.
        """
        self.nvc.run(*self.global_options(), "-e", *self.design.sim_tops, *self.elaborate_flags())

    def execute(self, one_shot=False) -> None:
        """Run the simulation"""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        run_flags = ss.run_flags

        if ss.wave:
            if isinstance(ss.wave, bool):
                run_flags += ["--wave"]
            else:
                run_flags += [f"--wave={ss.wave}"]
                if not ss.wave_format and isinstance(ss.wave, (str, Path)):
                    ss.wave = Path(ss.wave)
                    if ss.wave.suffix == ".vcd":
                        ss.wave_format = "vcd"
                    elif ss.wave.suffix == ".fst":
                        ss.wave_format = "fst"
                if ss.wave_format:
                    run_flags.append(f"--format={ss.wave_format}")
            if ss.wave_arrays:
                if isinstance(ss.wave_arrays, bool):
                    run_flags.append("--dump-arrays")
                else:
                    assert isinstance(ss.wave_arrays, int)
                    if ss.wave_arrays > 0:
                        run_flags.append(f"--dump-arrays={ss.wave_arrays}")

        if ss.exit_severity:
            run_flags.append(f"--exit-severity={ss.exit_severity}")
        if ss.ieee_warnings is not None:
            run_flags.append("--ieee-warnings=" + ("on") if ss.ieee_warnings else "off")
        if ss.stop_delta is not None:
            run_flags.append(f"--stop-delta={ss.stop_delta}")
        if ss.stop_time is not None:
            run_flags.append(f"--stop-time={ss.stop_time}")
        if ss.stats:
            run_flags.append("--stats")
        if ss.shuffle:
            run_flags.append("--shuffle")

        vhpi = [] if ss.vhpi is None else ss.vhpi.split(",")
        # TODO factor out cocotb handling
        if self.design.tb.cocotb and self.cocotb:
            vpi_path = self.cocotb.lib_path(interface="vhpi")
            assert vpi_path, "cocotb VHPI library for NVC was not found"
            vhpi.append(vpi_path)
            self.design.tb.generics = self.design.rtl.generics
            if not self.design.tb.top and self.design.rtl.top:
                self.design.tb.top = (self.design.rtl.top,)

        run_flags += [f"--load={p}" for p in vhpi]

        if one_shot:
            sources = self.design.sim_sources_of_type(SourceType.Vhdl)
            self.nvc.run(
                *self.global_options(),
                "-a",
                *sources,
                *self.analyze_flags(),
                "-e",
                *self.design.sim_tops,
                *self.elaborate_flags(),
                "-r",
                *run_flags,
                env=self.cocotb.env(self.design) if self.cocotb else {},
            )
        else:
            self.nvc.run(
                *self.global_options(),
                "-r",
                *self.design.sim_tops,
                *run_flags,
                env=self.cocotb.env(self.design) if self.cocotb else {},
            )

    def gen_makefile(self, units: List[str]) -> None:
        self.nvc.run("--make", *units)

    def check_syntax(self, sources: List[DesignSource], vhdl: VhdlSettings) -> None:
        self.nvc.run("--syntax", *sources)

    def run(self) -> None:
        design = self.design
        assert design.tb
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if not ss.one_shot:
            self.analyze()
            self.elaborate()
        self.execute(one_shot=ss.one_shot)

    def parse_reports(self) -> bool:
        success = True
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if self.cocotb and self.design.tb and self.design.tb.cocotb:
            success &= self.cocotb.add_results(self.results)
        return success
