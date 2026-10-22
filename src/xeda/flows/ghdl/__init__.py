from __future__ import annotations

import inspect
import logging
import platform
import re
from abc import ABCMeta
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from ...dataclass import Field, deliverable, field_validator, waveform
from ...design import Design, DesignSource, SourceType, Tuple012, VhdlSettings
from ...flow import (
    Flow,
    FlowException,
    FlowSettingsError,
    FlowSettingsException,
    SimFlow,
    SynthFlow,
    describe_results,
)
from ...flow.sim import SimEvidence
from ...flow.sim_evidence import parse_ghdl_log
from ...tool import Docker, Tool
from ...units import convert_unit
from ...utils import LOCATION_FORMS, SDF, common_root, replacing_file, setting_flag

log = logging.getLogger(__name__)


def _get_wave_opt_signals(wave_opt_file, extra_top=None):
    signals = []
    with open(wave_opt_file) as f:
        for line in f.read().splitlines():
            line = line.strip()
            if line.startswith("/"):
                sig = line[1:].split("/")
                if extra_top:
                    sig.insert(0, extra_top)
                signals.append(sig)
    root_group = common_root(signals)
    return signals, root_group


class GhdlTool(Tool):
    """GHDL, the VHDL analyzer, simulator and synthesizer: https://ghdl.readthedocs.io"""

    docker: Optional[Docker] = Docker(image="hdlc/sim:osvb")  # type: ignore
    executable: str = "ghdl"
    version_regexps: List[Union[str, re.Pattern[str]]] = [
        re.compile(r"GHDL\s+(?P<version>\d+\.\d+\.\d+).*", re.IGNORECASE)
    ]

    @cached_property
    def info(self) -> Dict[str, Optional[str]]:
        out = self.version_output
        lines = [ln for ln in (line.strip() for line in out.splitlines()) if ln] if out else []
        compiler = None
        backend = None
        for line in lines:
            m = re.match(r"\s*Compiled with\s+(.*)\s*$", line, re.IGNORECASE)
            if m:
                compiler = m.group(1).strip()
                break
        for line in lines:
            m = re.match(r"\s*(.*)\s+code generator\s*$", line, re.IGNORECASE)
            if m:
                backend = m.group(1).strip()
                break
        info = super().info
        if compiler is None and len(lines) >= 2:
            compiler = lines[1]
        if backend is None and compiler and len(lines) >= 3:
            backend = lines[2]
        if compiler:
            info["compiler"] = compiler
        if backend:
            info["backend"] = backend
        return info


class Ghdl(Flow, metaclass=ABCMeta):
    """Common settings and steps of the GHDL flows."""

    executables = ("ghdl",)

    #: GHDL analyzes VHDL.
    reads_sources = frozenset({SourceType.Vhdl})

    class Settings(Flow.Settings):
        analysis_flags: List[str] = Field(
            [],
            description="Additional flags for the analysis phase.",
        )
        elab_flags: List[str] = Field(
            [],
            description="Additional flags for the elaboration phase.",
        )
        syn_bindings: bool = Field(
            True,
            description="Use synthesizer rules for component binding. During elaboration, if the "
            "VHDL LRM rules bind a component to no entity, look in every known library for an "
            "entity with the name of the component.",
        )
        warn_flags: List[str] = Field(
            [
                "--warn-binding",
                "--warn-default-binding",
                "--warn-reserved",
                "--warn-library",
                "--warn-vital-generic",
                "--warn-shared",
                "--warn-runtime-error",
                "--warn-body",
                "--warn-specs",
                "--warn-no-hide",
                "--warn-parenthesis",
                "--warn-port",  # warn on an unconnected input port without a default (relaxed mode)
                "--warn-static",
                "--warn-port-bounds",  # warn on a bounds mismatch in a scalar port association
                "--warn-universal",  # warn on incorrect use of universal values
            ],
            description="Default warning flags.",
        )
        additional_warn_flags: List[str] = Field(
            [],
            description="Additional warning flags.",
        )
        werror: bool = Field(
            False, alias="warn_error", description="Treat every warning as an error."
        )
        elab_werror: bool = Field(
            False,
            alias="elab_warn_error",
            description="Treat warnings as errors during elaboration.",
        )
        relaxed: bool = Field(
            True,
            description="Relax some rules slightly, for compatibility with other simulators and "
            "synthesizers.",
        )
        clean_before_analyze: bool = Field(
            True,
            description="Run `ghdl remove` before analysis. It removes the work library and "
            "every file GHDL generated from it in the run directory.",
        )
        diagnostics: bool = Field(
            True, description="Enable both color and source line caret diagnostics."
        )
        work: Optional[str] = Field(None, description="Name of the WORK library.")
        expect_failure: bool = Field(
            False,
            description="Expect elaboration to fail (`--expect-failure`).",
        )
        synopsys: bool = Field(
            False,
            description="Allow the Synopsys packages in the `ieee` library.",
        )
        psl_in_comments: bool = Field(
            False, description="Parse PSL assertions in comments (for VHDL-2002 and earlier)."
        )

        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "clean": "clean_before_analyze (whether `ghdl remove` runs before analysis) or the "
            "--clean option (to empty the run directory)",
        }

        def common_flags(self, vhdl: VhdlSettings) -> List[str]:
            """Build GHDL flags shared by analysis and execution."""
            cf: List[str] = []
            if vhdl.standard:
                # GHDL spells the standard with two digits ("2008" is --std=08). The design's
                # own setting stays as written, because the design is shared and already hashed.
                standard = vhdl.standard
                if len(standard) == 4 and standard[:2] in ("20", "19"):
                    standard = standard[2:]
                cf.append(f"--std={standard}")
            if self.synopsys:
                cf += ["-fsynopsys", "-fexplicit"]
            if self.work:
                cf.append(f"--work={self.work}")
            # Each entry is a (library name, path) pair. `-P<dir>` adds the path to GHDL's
            # library search path.
            cf += [f"-P{path}" for _, path in self.lib_paths if path is not None]
            return cf

        @staticmethod
        def generics_flags(generics: Optional[Dict[str, Any]]) -> List[str]:
            if not generics:
                return []

            def conv(v):
                if isinstance(v, bool):
                    return str(v).lower()
                return v

            return [f"-g{k}={conv(v)}" for k, v in generics.items()]

        def get_flags(self, vhdl: VhdlSettings, stage: str, backend=None) -> List[str]:
            common = self.common_flags(vhdl)
            warn_flags: List[str] = self.warn_flags + self.additional_warn_flags
            analysis_flags: List[str] = common + self.analysis_flags
            elab_flags: List[str] = common + self.elab_flags
            find_top_flags = common
            if stage == "common":
                return common
            if stage == "remove":
                return common
            if self.werror:
                warn_flags += ["--warn-error"]
            elif self.elab_werror:
                elab_flags += ["--warn-error"]
            if backend in {"llvm", "gcc"} and self.syn_bindings:
                elab_flags += ["--syn-binding"]
            if self.relaxed:
                analysis_flags.extend(["-frelaxed-rules", "-frelaxed", "--mb-comments"])
                elab_flags.extend(["-frelaxed"])
            if self.psl_in_comments:
                analysis_flags.append("--psl")
            if self.verbose:
                analysis_flags.append("-v")
                elab_flags.append("-v")
            if self.diagnostics:
                elab_flags.extend(
                    [
                        "-fcaret-diagnostics",
                        "-fcolor-diagnostics",
                        "-fdiagnostics-show-option",
                    ]
                )
            if self.work:
                elab_flags.append("-v")
            if self.expect_failure:
                elab_flags.append("--expect-failure")
            if vhdl.synopsys:
                analysis_flags += ["-fsynopsys", "-fexplicit"]
            if stage in ("import", "analyze"):
                return analysis_flags + warn_flags
            if stage in ("make", "elaborate"):
                return elab_flags + warn_flags
            if stage in ("find-top"):
                return find_top_flags
            raise ValueError("unknown stage!")

    @cached_property
    def ghdl(self):
        return GhdlTool()  # pyright: ignore[reportCallIssue]

    def find_top(self, *args, sources=None):
        out = self.ghdl.run_get_stdout("find-top", *args, raise_on_error=False)
        tops = out.strip().split() if out else []
        if not tops and sources:
            sources = sources or []
            find_out = self.ghdl.run_get_stdout(
                "-f", *args, *[str(s) for s in sources], raise_on_error=True
            )
            entities: List[str] = []
            if find_out:
                for line in find_out.split("\n"):
                    sp = line.split()
                    if len(sp) >= 2 and sp[0] == "entity":
                        entities.append(sp[1])
                log.info("discovered entities: %s", ", ".join(entities))
                if entities:
                    tops = (entities[-1],)
        return tops

    def _work_library(self, vhdl: VhdlSettings) -> str:
        """Return the file name of the work library that GHDL analyzes into.

        The file lies in GHDL's working directory and is named `<work>-obj<standard>.cf`. GHDL's
        default standard, 93c, gives `obj93`.
        """
        assert isinstance(self.settings, self.Settings)
        standard = vhdl.standard or "93"
        if len(standard) == 4 and standard[:2] in ("20", "19"):
            standard = standard[2:]
        return f"{self.settings.work or 'work'}-obj{standard[:2]}.cf"

    def analyze(
        self,
        sources: List[DesignSource],
        top: Union[str, Tuple012, None],
        vhdl: VhdlSettings,
        find_top: bool = True,
    ) -> Tuple012:
        """Analyze the VHDL `sources` into the work library.

        Run `ghdl remove` first if `clean_before_analyze` is set. Return the top units: `top` as
        given or, when it is empty and `find_top` is set, the units that `ghdl find-top` (else
        the last entity of the sources) finds. Return an empty tuple if neither gives one.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        sources = [src for src in sources if src.type == SourceType.Vhdl]
        if isinstance(top, str):
            top = (top,)
        backend = self.ghdl.info.get("backend", None)
        # `ghdl remove` deletes the work library in the working directory (the run directory)
        # and the files that GHDL built from it.
        library = self._work_library(vhdl)
        if ss.clean_before_analyze and (self.run_path / library).exists():
            self.ghdl.run("remove", *ss.get_flags(vhdl, "remove", backend=backend))
        analysis_flags = ss.get_flags(vhdl, "analyze", backend=backend)
        self.ghdl.run("analyze", *analysis_flags, *(str(s) for s in sources))
        if not top and find_top:
            log.info("No top units were specified. Trying to discover them with `find-top`.")
            tops = self.find_top(*ss.get_flags(vhdl, "find-top"), sources=sources)
            if tops:
                # Build the tuple by hand: mypy accepts this form.
                top = (tops[0],) if len(tops) == 1 else (tops[0], tops[1])
                log.info(
                    "[ghdl:find-top] discovered top unit: %s. "
                    "Set `top` explicitly if this is not the intended top unit.",
                    ", ".join(top),
                )
            else:
                log.error(
                    inspect.cleandoc(
                        """[ghdl:find-top] Unable to determine the top unit.
                        Set `tb.top` (for simulation) or `rtl.top` (for synthesis) in the design file."""
                    )
                )
        return top or ()


class GhdlSynth(Ghdl, SynthFlow):
    """Convert a VHDL design to Verilog with `ghdl --synth`."""

    # The flow reports no result keys beyond the common ones. The empty table says so.
    results_description: dict = {}

    requires_physical_clocks = False

    class Settings(Ghdl.Settings, SynthFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **Ghdl.Settings.removed_settings,
            **{
                name: f"`flows.ghdl_sim.{name}`, the simulation flow's: `ghdl --synth` compiles and "
                "links nothing"
                for name in ("compiler_flags", "assembler_flags", "linker_flags")
            },
        }
        vendor_library: Optional[str] = Field(
            None, description="Treat every unit of this library as a black box."
        )
        no_formal: bool = Field(
            True,
            description="Synthesize neither `assert` nor PSL. Required for the yosys+nextpnr flow.",
        )
        no_assert_cover: bool = Field(
            False,
            description='Pass `--no-assert-cover` to GHDL. Its help text reads: "Cover PSL '
            'assertion activation".',
        )
        assert_assumes: bool = Field(False, description="Treat all PSL asserts like PSL assumes.")
        assume_asserts: bool = Field(False, description="Treat all PSL assumes like PSL asserts.")
        verilog_output: Optional[Path] = Field(
            None,
            description="Output file for the generated Verilog; by default `outputs/<design>.v`. If a "
            "directory is provided, each VHDL source file is converted to a Verilog file in that "
            "directory.",
            json_schema_extra=deliverable("outputs/{design}.v", always=True),
        )

    def init(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.resolve_lib_paths()
        if ss.verilog_output is not None:
            ss.verilog_output = self.process_path(
                ss.verilog_output, subs_vars=True, resolve_to=None
            )

    def run(self) -> None:
        """Synthesize VHDL sources into Verilog artifacts."""
        design = self.design
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        flags = self.synth_args(ss, design, one_shot_elab=False)
        flags += ["--out=verilog", "--warn-nowrite"]
        if ss.verilog_output is None:
            conventional = ss.conventional_output("verilog_output", design.name)
            assert conventional is not None  # the field is a deliverable with a conventional name
            ss.verilog_output = Path(conventional)
        output = self.run_file(ss.verilog_output)
        per_source = output.is_dir() or (not output.exists() and output.suffix not in {".v", ".sv"})
        # `ghdl synth` elaborates by itself, so the sources are only analyzed here: `ghdl make`
        # would also build a simulation executable, and on the LLVM/GCC backends it rejects two
        # sources sharing a base name (`rtl/a/fifo.vhd`, `rtl/b/fifo.vhd` are "both compiled to
        # 'fifo.o'").
        top = self.analyze(
            self.sources_read(), design.rtl.top, design.language.vhdl, find_top=not per_source
        )
        if per_source:
            # Decide every output name before synthesizing anything. A collision then fails the
            # run before any output is written.
            outputs = self._per_source_outputs(ss.verilog_output)
            output.mkdir(parents=True, exist_ok=True)
            fixed_params = "\n".join(
                [f"  parameter {k} = {v};" for k, v in design.rtl.generics.items()]
            )
            for out_file, src in outputs.items():
                verilog = self.ghdl.run_get_stdout("synth", *flags, str(src.path), "-e")
                if not verilog:
                    raise FlowException(f"ghdl synthesis of {src.path} failed!")
                verilog = verilog.replace(");", f");\n{fixed_params}", 1)
                out_path = self.run_directory.writable(out_file)
                if out_path.exists():
                    log.warning("File %s will be overwritten!", out_file)
                else:
                    log.info("Generating verilog: %s", out_file)
                with replacing_file(out_path) as f:
                    f.write(verilog)
            self.artifacts.generated_verilog = list(outputs)
        else:
            if not top:
                raise FlowException("Unable to determine the top unit")
            output.parent.mkdir(parents=True, exist_ok=True)
            self.run_directory.writable(ss.verilog_output)
            log.info("Generating verilog: %s", ss.verilog_output)
            self.ghdl.run("synth", *flags, *top, stdout=ss.verilog_output)
            self.artifacts.generated_verilog = [ss.verilog_output]

    def _per_source_outputs(self, output_dir: Path) -> dict[Path, DesignSource]:
        """Return the Verilog file that each VHDL source is converted to, in source order.

        Each file is named after the path of its source (`Design.source_artifact_name`), because
        two sources in different directories can share a stem (`rtl/a/fifo.vhd`,
        `rtl/b/fifo.vhd`). Two sources whose names still coincide are an error that names both,
        since one output would silently replace the other.
        """
        design = self.design
        outputs: dict[Path, DesignSource] = {}
        for src in self.sources_read():
            out_file = output_dir / design.source_artifact_name(src, ".v")
            other = outputs.get(out_file)
            if other is not None:
                raise FlowException(
                    f"VHDL sources {other.path} and {src.path} would both be converted to "
                    f"{out_file}; rename one of them."
                )
            outputs[out_file] = src
        return outputs

    @staticmethod
    def synth_args(ss: Optional[Settings], design: Design, one_shot_elab: bool = True) -> List[str]:
        assert ss is not None and isinstance(
            ss, GhdlSynth.Settings
        ), "GHDL settings must be set at this point"
        flags = ss.get_flags(design.language.vhdl, "elaborate")
        flags += setting_flag(ss.vendor_library, name="vendor_library")
        flags += setting_flag(ss.no_formal, name="no_formal")
        flags += setting_flag(ss.no_assert_cover, name="no_assert_cover")
        flags += setting_flag(ss.assert_assumes, name="assert_assumes")
        flags += setting_flag(ss.assume_asserts, name="assume_asserts")
        flags.extend(ss.generics_flags(design.rtl.generics))
        if one_shot_elab:
            flags += map(str, design.sources_of_type(SourceType.Vhdl, rtl=True, tb=False))
        return flags


# class GhdlLint(Ghdl, Flow):
#     """Lint VHDL sources using GHDL"""

#     class Settings(Ghdl.Settings):
#         pass


class GhdlSim(Ghdl, SimFlow):
    """Simulate a VHDL design with GHDL."""

    cocotb_sim_name = "ghdl"
    aliases = ["ghdl"]
    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )
    _sim_log: Path | None = None

    @classmethod
    def runs_without_testbench_top(
        cls, design: Design, settings: Flow.Settings | None = None
    ) -> bool:
        """Return whether the testbench has a VHDL source, whose top unit `ghdl find-top` finds."""
        return any(src.type is SourceType.Vhdl for src in design.tb.sources)

    class Settings(Ghdl.Settings, SimFlow.Settings):
        run_flags: List[str] = Field([], description="Extra flags passed to `ghdl run`.")
        optimization_flags: List[str] = Field(
            ["-O3"], description="Optimization flags passed to `ghdl make`."
        )
        compiler_flags: List[str] = Field(
            [],
            description="Flags for the C compiler. Used only with the LLVM and GCC backends.",
        )
        assembler_flags: List[str] = Field(
            [],
            description="Flags for the assembler. Used only with the LLVM and GCC backends.",
        )
        linker_flags: List[str] = Field(
            [],
            description="Flags for the linker. Used only with the LLVM and GCC backends.",
        )
        asserts: Optional[Literal["disable", "disable-at-0"]] = Field(
            None,
            description="Disable user assertion failures entirely (`disable`) or only those firing "
            "at time 0 (`disable-at-0`).",
        )
        ieee_asserts: Optional[Literal["disable", "disable-at-0"]] = Field(
            "disable-at-0",
            description="Same as `asserts`, but for assertions inside the IEEE standard "
            "libraries. The default suppresses the metavalue warnings they emit at time 0.",
        )
        sdf: SDF = Field(
            SDF(),
            description="SDF files for VITAL annotation: the optional files `min`, `max` and "
            "`typ`, and `root`, the region they apply to (`tb.uut` by default).",
        )
        wave: Optional[Union[str, Path]] = Field(
            None,
            description="Write the waveforms (GHW): a name in the run directory, or a location it is "
            f"delivered to ({LOCATION_FORMS}). `true` writes `dump.ghw`; a name without an "
            "extension gets `.ghw`. A name that ends in `.vcd` or `.fst` is refused: use `vcd` or "
            "`fst`.",
            json_schema_extra=waveform("ghw"),
        )
        read_wave_opt: Optional[Path] = Field(
            None,
            description="Wave option file that filters the signals written to the waveform. "
            "Needs a waveform (`vcd`, `vcdgz`, `fst` or `wave`).",
        )
        write_wave_opt: Optional[Path] = Field(
            None,
            description="Write a wave option file that lists all signals of the design. An "
            "existing file is replaced.",
            json_schema_extra=deliverable("wave.opt"),
        )
        vcdgz: Optional[Union[str, Path]] = Field(
            None,
            description="Write the waveforms as a gzip-compressed VCD file (`--vcdgz`). `true` "
            "writes `dump.vcd.gz`; a name without an extension gets `.vcdgz`. `vcd` writes plain "
            "VCD, and a gzip name given to it is refused.",
            json_schema_extra=waveform("vcdgz", "dump.vcd.gz"),
        )
        fst: Optional[Union[str, Path]] = Field(
            None,
            description="Write the waveforms to an FST file. `true` writes `dump.fst`; a name "
            "without an extension gets `.fst`.",
            json_schema_extra=waveform("fst"),
        )
        stop_delta: Optional[str] = Field(
            None,
            description="Stop the simulation after this many delta cycles at the same time.",
        )
        disp_tree: Optional[Literal["inst", "proc", "port"]] = Field(
            None,
            description="Display the design hierarchy as a tree of instantiated design "
            "entities. The GHDL documentation describes the values.",
        )
        vpi: Union[str, List[str], None] = Field(
            None,
            description="Load one or more VPI libraries.",
        )
        # TODO workdir?

        @field_validator("stop_time", mode="before")
        @classmethod
        def validate_stop_time(cls, value):
            if value is not None:
                if convert_unit(value, "fs", from_unit="ns") < 0:
                    raise ValueError("stop_time must be nonnegative")
            return value

        def runtime_flags(self) -> list[str]:
            """Return `run_flags` with the `--assert-level` that `fail_severity` selects."""
            threshold = "failure" if self.fail_severity == "fatal" else self.fail_severity
            canonical = f"--assert-level={threshold}"
            for flag in self.run_flags:
                if flag.startswith("--assert-level") and flag != canonical:
                    raise FlowSettingsException(
                        f"run_flags {flag!r} conflicts with fail_severity={self.fail_severity}; "
                        "set fail_severity instead"
                    )
            return [flag for flag in self.run_flags if flag != canonical] + [canonical]

    @classmethod
    def check_waveforms(cls, settings: Flow.Settings) -> None:
        """Refuse more than one waveform, and `read_wave_opt` without one.

        GHDL writes one waveform. `vcd`, `vcdgz`, `fst` and `wave` each select its format.
        """
        chosen = [
            name for name in ("vcd", "vcdgz", "fst", "wave") if getattr(settings, name) is not None
        ]
        if len(chosen) > 1:
            raise FlowSettingsException(
                f"ghdl_sim writes one waveform, and {', '.join(chosen)} are set: keep one of them"
            )
        if not chosen and getattr(settings, "read_wave_opt", None) is not None:
            raise FlowSettingsException(
                "`read_wave_opt` filters the waveform, and none is set: set `vcd`, `vcdgz`, `fst` or "
                "`wave`"
            )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse conflicting waveform settings and run flags that compete with `fail_severity`."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        cls.check_waveforms(settings)
        settings.runtime_flags()

    def elaborate(
        self,
        sources: List[DesignSource],
        top: Union[str, Tuple012, None],
        vhdl: VhdlSettings,
    ) -> Tuple012:
        """Analyze `sources` and build the simulation executable of the top unit (`ghdl make`).

        Return the top units as a `Tuple012`.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        top = self.analyze(sources, top, vhdl)
        if not top:
            raise FlowException("Unable to determine the top unit")
        backend = self.ghdl.info.get("backend", None)
        args = ss.get_flags(vhdl, "make", backend=backend)
        args += ss.optimization_flags
        if backend:
            log.info("GHDL backend: %s", backend)
            backend_split = backend.split()
            compiler = backend_split[0].lower() if backend_split else None
            backend_version = backend_split[1].split(".") if len(backend_split) > 1 else []
            # Workaround for linker warnings on macOS/arm64 with older toolchains. The latest
            # Xcode, Command Line Tools, LLVM, GNAT and GHDL do not need it.
            # The flag belongs to Apple's linker. Add it only for a GHDL that runs on this macOS
            # host. In a (Linux) container, GNU ld misreads it and the link fails (`-lgcc_s`).
            runs_on_host = not (self.ghdl.dockerized and self.ghdl.docker)
            if compiler == "llvm" and backend_version and backend_version[0].isdigit():
                llvm_major = int(backend_version[0])
                link_flag = "-Wl,-no_compact_unwind"
                if (
                    runs_on_host
                    and platform.system() == "Darwin"
                    and platform.machine() == "arm64"
                    and llvm_major
                    < 19  # TODO: check the GNAT version too? The LLVM limit is a guess.
                    and ss.linker_flags.count(link_flag) == 0
                ):
                    log.info("Adding no_compact_unwind linker flags for macOS/arm64 %s" % backend)
                    ss.linker_flags.append(link_flag)
            if compiler in ("llvm", "gcc"):
                args += (f"-Wc,{x}" for x in ss.compiler_flags)
                args += (f"-Wa,{x}" for x in ss.assembler_flags)
                args += (f"-Wl,{x}" for x in ss.linker_flags)
        args += list(top)
        self.ghdl.run("make", *args)
        return top

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)
        # a flow built directly skips planning, so check here
        self.check_settings_supported(ss)
        self.resolve_lib_paths()

    def has_evidence_adapter(self) -> bool:
        return not self.cocotb

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_ghdl_log(self, self._sim_log) if self._sim_log is not None else None

    def run(self) -> None:
        """Compile and run the GHDL testbench."""
        design = self.design
        assert design.tb
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        cf = ss.common_flags(design.language.vhdl)
        run_flags = ss.runtime_flags()
        sdf_root = ss.sdf.root if ss.sdf.root else design.tb.uut
        for delay_type, sdf_file in ss.sdf.delay_items():
            if sdf_file:
                run_flags.append(f"--sdf={delay_type}={sdf_root}={sdf_file}")

        if ss.wave:
            ss.wave = self.process_path(ss.wave, subs_vars=True, resolve_to=None)

        if ss.vcd:
            # FIXME: process_path might be run twice
            ss.vcd = self.process_path(ss.vcd, subs_vars=True, resolve_to=None)
            run_flags.append(f"--vcd={ss.vcd}")
            log.warning("Dumping VCD to %s", ss.vcd)
        elif ss.vcdgz:
            ss.vcdgz = self.process_path(ss.vcdgz, subs_vars=True, resolve_to=None)
            run_flags.append(f"--vcdgz={ss.vcdgz}")
            log.warning("Dumping compressed VCD to %s", ss.vcdgz)
        elif ss.fst:
            # FIXME: process_path might be run twice
            ss.fst = self.process_path(ss.fst, subs_vars=True, resolve_to=None)
            run_flags += setting_flag(ss.fst, name="fst")
            log.warning("Dumping fst to %s", ss.fst)
        elif ss.wave:
            run_flags += setting_flag(ss.wave, name="wave")
            log.warning("Dumping GHW to %s", ss.wave)

        if ss.wave or ss.vcd or ss.vcdgz or ss.fst:
            if not ss.read_wave_opt and not ss.write_wave_opt:
                ss.write_wave_opt = Path("wave.opt")

        if ss.write_wave_opt:
            # GHDL does not replace a wave option file: the previous one goes first
            wave_opt = self.run_directory.writable(ss.write_wave_opt)
            for removed in self.run_directory.remove(wave_opt):
                log.warning("Deleted the existing wave option file %s", removed)
        run_flags += setting_flag(ss.write_wave_opt, name="write_wave_opt")
        if ss.read_wave_opt:
            if not self.run_file(ss.read_wave_opt).exists():  # TODO move to validation
                raise FlowSettingsError(
                    [
                        (
                            "read_wave_opt",
                            f"File {ss.read_wave_opt} does not exist",
                            None,
                            None,
                        )
                    ],
                    self.Settings,
                )
        run_flags += setting_flag(ss.read_wave_opt, name="read_wave_opt")
        run_flags += setting_flag(ss.disp_tree, name="disp_tree")
        run_flags += setting_flag(ss.asserts, name="asserts")
        run_flags += setting_flag(ss.ieee_asserts, name="ieee_asserts")

        vpi = (
            []
            if ss.vpi is None
            else [ss.vpi] if not isinstance(ss.vpi, (list, tuple)) else list(ss.vpi)
        )
        # The testbench's parameters and top module as this run uses them. The design itself
        # stays unchanged: every flow of a run shares it, and it is already hashed.
        tb_generics = design.tb.generics
        tb_top = design.tb.top
        # TODO factor out cocotb handling
        if design.tb.cocotb and self.cocotb:
            vpi_path = self.cocotb.lib_path()
            assert vpi_path, "cocotb VPI library for GHDL was not found"
            vpi.append(vpi_path)
            # tb_generics = list(design.tb.generics)  # TODO pass to cocotb?
            tb_generics = design.rtl.generics
            if not tb_top and design.rtl.top:
                tb_top = (design.rtl.top,)
        run_flags += setting_flag(vpi, name="vpi")

        if ss.debug:
            run_flags.extend(
                [
                    "--trace-processes",
                    "--trace-signals",
                    "--checks",
                    "--disp-sig-types",
                    "--stats",
                    "--dump-rti",
                ]
            )

        if ss.stop_time is not None:
            stop_fs = round(convert_unit(ss.stop_time, "fs", from_unit="ns"))
            run_flags.append(f"--stop-time={stop_fs}fs")
        run_flags += setting_flag(ss.stop_delta, name="stop_delta")

        run_flags.extend(ss.generics_flags(tb_generics))

        tb_top = self.elaborate(self.sources_read(tb=True), tb_top, design.language.vhdl)
        # like `design.sim_tops`, but with the top unit that `elaborate` found
        sim_tops = (design.rtl.top,) if design.tb.cocotb and design.rtl.top else tb_top
        runtime = self.ghdl
        if not self.cocotb:
            self._sim_log = self.run_directory.writable("sim.log")
            self.run_directory.remove(self._sim_log)
            run_flags.append("--unbuffered")
            # The evidence tee shows native output and copies it into this invocation's log.
            # A flow's general stdout redirection still applies to analysis/elaboration.
            runtime = runtime.derive(runtime.executable, redirect_stdout=None)
        runtime.run(
            "run",
            *cf,
            *sim_tops,
            *run_flags,
            env=self.cocotb.env(design) if self.cocotb else {},
            timeout=ss.timeout,
            tee=self._sim_log,
            merge_stderr=not self.cocotb,
        )
