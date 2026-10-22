import logging
import os
from pathlib import Path
from typing import ClassVar, Dict, List, Literal, Optional, Union

from colorama import Fore as fg
from colorama import Style as style

from ..dataclass import WORKING, Field, field_validator, waveform
from ..design import SourceType
from ..flow import Flow, FlowSettingsException, SimFlow, NoReplacement
from ..flow.flow import describe_results
from ..flow.sim import SimEvidence
from ..tool import Tool
from ..units import convert_unit, data_size
from ..utils import replacing_file, tcl_word
from .vcs_evidence import RUNTIME_LOG, parse_vcs_evidence, prepare_vcs_runtime

log = logging.getLogger(__name__)


class Vcs(SimFlow):
    """Simulate a design with Synopsys VCS and judge it by runtime evidence.

    The flow runs `vcs` and `simv` as two steps, or as one step with `one_shot_run`.
    This adapter is documentation-only and is not verified against a licensed VCS release.
    A quiet `$finish(0)` and VHDL completion fail unless a native finish diagnostic is observable.
    UCLI time checkpoints alone do not establish HDL completion.
    """

    executables = ("vcs", "vlogan", "vhdlan", "fsdb2vcd", "vpd2vcd")

    #: `vlogan` analyzes Verilog and SystemVerilog sources and finds a header through the directory
    #: it lies in (`+incdir+`). `vhdlan` analyzes VHDL.
    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
        }
    )

    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )

    unsupported_waveforms = {
        "vcd": "VCS writes VPD, FSDB or EVCD waveforms: set `vpd`, `fsdb` or `evcd` (`to_vcd` "
        "converts an FSDB or VPD file to VCD)"
    }

    @classmethod
    def check_waveforms(cls, settings: Flow.Settings) -> None:
        """Refuse more than one waveform, and `to_vcd` or a size limit without its format."""
        assert isinstance(settings, cls.Settings)
        chosen = [name for name in ("fsdb", "vpd", "evcd") if getattr(settings, name) is not None]
        if len(chosen) > 1:
            raise FlowSettingsException(
                f"VCS writes one waveform, and {', '.join(chosen)} are set: keep one of them"
            )
        if settings.to_vcd and not (settings.fsdb or settings.vpd):
            raise FlowSettingsException(
                "`to_vcd` converts an FSDB or VPD waveform: set `fsdb` or `vpd`"
            )
        if settings.vpd_size_limit is not None and settings.vpd is None:
            raise FlowSettingsException("`vpd_size_limit` limits the VPD waveform: set `vpd`")
        default_limit = cls.Settings.model_fields["fsdb_size_limit"].default
        if settings.fsdb_size_limit != default_limit and settings.fsdb is None:
            raise FlowSettingsException("`fsdb_size_limit` limits the FSDB waveform: set `fsdb`")

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_vcs_evidence(self)

    highlight_rules = {
        r"^(Error:)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\*+ERROR\*+)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(Error-\s*\[\s*\w+\s*\])(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\s*SDF Error:)(.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(\s*Total errors:)(\s+[1-9]\d*\s*.+)$": fg.RED + style.BRIGHT + r"\g<0>",
        r"^(Lint-\[\w+\])(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(Warning:)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(\*+WARN\*+)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(.WARN)(.+)$": fg.YELLOW + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
        r"^(Warning-\s*\[\s*\w+\s*\])(.+)$": fg.YELLOW
        + style.BRIGHT
        + r"\g<1>"
        + style.NORMAL
        + r"\g<2>",
        r"^(Information[:-])(.+)$": fg.GREEN + style.BRIGHT + r"\g<1>" + style.NORMAL + r"\g<2>",
    }

    #: The tools that `init()` makes for each flow. A tool holds the flow's settings (`dockerized`,
    #: the container) and what it learned about the program. Neither belongs to the class.
    vlogan: Tool
    vhdlan: Tool
    vcs: Tool
    fsdb2vcd: Tool
    vpd2vcd: Tool
    simv: Tool

    class Settings(SimFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **SimFlow.Settings.removed_settings,
            "optimization_flags": "`vcs_flags`, which reach the `vcs` elaboration, or `cflags` for the C "
            "compiler",
            "work_dir": NoReplacement("Xeda passed it to nothing"),
            "ucli": NoReplacement("Xeda always runs VCS with -ucli"),
        }
        simv_flags: List[str] = Field(
            [],
            description="Extra arguments for the compiled simulator executable (`simv`). "
            "Ignored when `one_shot_run` is set.",
        )
        sim_no_save: bool = Field(
            True,
            description="Pass `-no_save` so VCS does not write a restartable simulation snapshot.",
        )
        generate_kdb: bool = Field(
            False,
            description="Generate the Verdi knowledge database (`-kdb`), which Verdi debugging "
            "needs. This slows elaboration.",
        )
        supress_banner: bool = Field(True, description="Suppress the VCS copyright banner (`-nc`).")
        quiet: bool = Field(False, description="Suppress informational output from VCS.")
        vhdl_xlrm: bool = Field(
            False, description="Enable VHDL features beyond the LRM (`vhdlan -xlrm`)."
        )
        one_shot_run: bool = Field(
            False,
            description="Run the simulation in the `vcs` step (`-R`), with no separate `simv` "
            "run. Ignores `simv_flags`.",
        )
        ucli_script: Optional[Path] = Field(
            None,
            description="UCLI (Universal Command Line Interface) script that drives the simulator "
            "executable (`simv`). Xeda always runs `simv` under UCLI with its own script.",
        )
        gui: Optional[Union[bool, str]] = Field(
            None,
            description="Start the GUI (Graphical User Interface) when the simulator runs. A "
            "string names the GUI: `dve` or `verdi`. With `true`, VCS starts Verdi if `VC_HOME` "
            "is set.",
        )
        full64: bool = Field(True, description="Compile and run in 64-bit mode (`-full64`).")
        time_unit: Optional[str] = Field(
            "1ns", description='Default simulation time unit, e.g. "1ns".'
        )
        time_resolution: Optional[str] = Field(
            "1ps", description='Default simulation time precision, e.g. "1ps".'
        )
        sdf_file: Optional[Union[str, Path]] = Field(
            None,
            description="SDF file that back-annotates delays (VCS unified SDF feature).",
        )
        sdf_instance: Optional[str] = Field(
            None,
            description="Hierarchy instance that is the root path of the back-annotation. "
            "Separate hierarchy levels with dots, for example `TB_NAME.UUT_NAME`. Required with "
            "`sdf_file`.",
        )
        sdf_type: Literal["min", "typ", "max"] = Field(
            "typ", description="SDF delay type to back-annotate."
        )
        vcs_warn: Optional[str] = Field(
            "all,noTFIPC,noLCA_FEATURES_ENABLED",
            description="Value of VCS' `+warn` option: a comma-separated list of warning IDs to "
            "enable, or `noID` to disable one.",
        )
        vlogan_warns: Optional[str] = Field(
            None, description="Value of `+warn` for the Verilog analysis step (vlogan)."
        )
        vcs_nowarn: List[str] = Field(
            [], description="Warning IDs to silence, passed as `-nowarn <id>` to VCS."
        )
        lint: Optional[str] = Field(
            "all,TFIPC-L,noVCDE,noTFIPC,noIWU,noOUDPE,noUI",
            description="Value of VCS' `+lint` option: a comma-separated list of lint checks to "
            "enable, or `noID` to disable one.",
        )
        debug_access: Optional[Union[str, bool]] = Field(
            True,
            description="Debug visibility level: true for `-debug_access+all`, false to disable, "
            'or a suffix string such as "+r" or "+class". More access means slower simulation.',
        )
        debug_region: Optional[str] = Field(
            None,
            description="Restrict debug visibility to this region (`-debug_region=...`), e.g. "
            '"cell+encrypt", to keep the performance cost contained.',
        )
        functional_vital: bool = Field(
            False, description="Disable VITAL timing simulation (`vhdlan -functional_vital`)."
        )
        init_std_logic: Optional[Literal["U", "X", "0", "1", "Z", "W", "L", "H", "-"]] = Field(
            None, description="Initial value of `std_logic` signals (`vhdlan -init_std_logic`)."
        )
        initreg: Optional[str] = Field(
            None,
            description="Initial value of registers (`+vcs+initreg`). Use `random` for random "
            "values.",
        )
        lic_wait: Optional[int] = Field(
            100,
            description="How long to wait for a license when none is available (`vhdlan -licw`, "
            "`vcs -vc_lic_wait`).",
        )
        vlogan_flags: List[str] = Field(
            ["+v2k"], description="Extra flags passed to `vlogan` when analyzing Verilog sources."
        )
        vhdlan_flags: List[str] = Field(
            [], description="Extra flags passed to `vhdlan` when analyzing VHDL sources."
        )
        vcs_flags: List[str] = Field(
            [], description="Extra flags passed to `vcs` during elaboration."
        )
        cflags: List[str] = Field(
            ["-O3", "-march=native", "-mtune=native"],
            description="Flags passed through to the C compiler that builds the simulation "
            "executable. The defaults target the build machine, so the binary is not portable.",
        )
        vcs_log_file: Optional[Path] = Field(
            Path("vcs.log"),
            description="File VCS writes its elaboration log to.",
            json_schema_extra=WORKING,
        )
        fsdb: Optional[Path] = Field(
            None,
            description="FSDB (Fast Signal DataBase) waveform file to write. `true` writes "
            "`dump.fsdb`.",
            json_schema_extra=waveform("fsdb"),
        )
        fsdb_size_limit: Optional[int] = Field(
            256,
            description="FSDB size limit in MB (`+fsdb+dump_limit`). Unset uses the VCS default.",
            json_schema_extra=data_size(),
        )
        to_vcd: bool = Field(
            False,
            description="Convert the FSDB or VPD waveform to VCD (Value Change Dump) after the "
            "simulation. The VCD file lies beside the waveform and has its file name with a "
            "`.vcd` suffix.",
        )
        vpd: Optional[Path] = Field(
            None,
            description="VPD waveform file to write. `true` writes `dump.vpd`.",
            json_schema_extra=waveform("vpd"),
        )
        evcd: Optional[Path] = Field(
            None,
            description="EVCD waveform file to write. `true` writes `dump.evcd`.",
            json_schema_extra=waveform("evcd"),
        )
        vpd_size_limit: Optional[int] = Field(
            None,
            description="VPD size limit in MB (`+vpdfilesize`). When the simulation reaches the "
            "limit, it overwrites older history. Needs `vpd`.",
            json_schema_extra=data_size(),
        )

        @field_validator("gui", "debug_access", mode="before")
        @classmethod
        def _switch_text_is_a_switch(cls, value):
            """Read `true` and `false` text (how a command line writes a boolean) as a boolean."""
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            return value

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse an SDF file without the instance it annotates, before anything runs."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        cls.check_waveforms(settings)
        if settings.sdf_file and settings.sdf_instance is None:
            raise FlowSettingsException("`sdf_instance` is required when `sdf_file` is set")

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # a flow built directly is not planned, so check here
        self.check_settings_supported(ss)
        if ss.sdf_file:
            ss.sdf_file = self.process_path(ss.sdf_file, resolve_to=self.design.design_root)
        rules = self.highlight_rules
        # No tool is asked for its version. VCS is not verified on a licensed release, and this
        # adapter has not checked a `vcs --version` query.
        self.vlogan = Tool("vlogan", self, version_flag=None, highlight_rules=rules)
        self.vhdlan = Tool("vhdlan", self, version_flag=None, highlight_rules=rules)
        self.vcs = Tool("vcs", self, version_flag=None, highlight_rules=rules)
        self.fsdb2vcd = Tool("fsdb2vcd", self, version_flag=None, highlight_rules=rules)
        self.vpd2vcd = Tool(
            "vpd2vcd",
            self,
            default_args=["-full64", "-q"],
            version_flag=None,
            highlight_rules=rules,
        )
        if ss.ucli_script:
            ss.ucli_script = self.process_path(ss.ucli_script, resolve_to=self.design.design_root)
        # a user's script drives simv, so its output is the script's and stays uncolored
        driven_by_user = ss.ucli_script is not None
        self.simv = Tool(
            "./simv", self, version_flag=None, highlight_rules=None if driven_by_user else rules
        )

    def run(self):
        """Compile and simulate the design with VCS."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.stop_time is not None and (ss.ucli_script or ss.gui):
            raise FlowSettingsException(
                "VCS `stop_time` cannot be enforced with a user `ucli_script` or GUI control: "
                "use the owned batch script"
            )
        stop_time = None
        if ss.stop_time is not None:
            stop_fs = int(round(convert_unit(ss.stop_time, "fs", from_unit="ns")))
            if stop_fs <= 0:
                raise FlowSettingsException(
                    "VCS `stop_time` must be positive after conversion to femtoseconds, "
                    "for an absolute UCLI breakpoint"
                )
            stop_time = f"{stop_fs}fs"
        env = {
            "VCS_TARGET_ARCH": os.environ.get("VCS_TARGET_ARCH", "amd64"),
            "VCS_ARCH_OVERRIDE": os.environ.get("VCS_ARCH_OVERRIDE", "linux"),
        }
        vlogan_args = list(ss.vlogan_flags)
        vhdlan_args = list(ss.vhdlan_flags)
        vcs_args = list(ss.vcs_flags)
        simv_args = list(ss.simv_flags)
        common_run_args: list[str] = []
        setup: list[str] = []
        for kind in ("fsdb", "vpd", "evcd"):
            dumped = getattr(ss, kind)
            if dumped:
                fid = "FSDB0" if kind == "fsdb" else "VPD0"
                setup += [
                    f"dump -file {tcl_word(dumped)} -type {kind.upper() if kind == 'fsdb' else kind}",
                    f"dump -add . -add / -aggregates -fid {fid}",
                    f"dump -enable -fid {fid}",
                ]
                break
        if ss.gui:
            ss.generate_kdb = True
        if ss.supress_banner:
            vlogan_args.append("-nc")
            vhdlan_args.append("-nc")
            vcs_args.append("-nc")
            simv_args.append("-nc")
        if ss.is_quiet:
            vlogan_args.append("-q")
            vhdlan_args.append("-q")
            vcs_args.append("-q")
        if ss.full64:
            vlogan_args.append("-full64")
            vhdlan_args.append("-full64")
            vcs_args.append("-full64")
        if ss.generate_kdb:
            vlogan_args.append("-kdb")
            vhdlan_args.append("-kdb")
            vcs_args.append("-kdb")
        if ss.lic_wait is not None:
            vhdlan_args += ["-licw", str(ss.lic_wait)]
            vcs_args += ["-vc_lic_wait", str(ss.lic_wait)]
        if ss.cflags:
            vcs_args += ["-CFLAGS", " ".join(ss.cflags)]
        vlogan_args.append(f"-timescale={ss.time_unit}/{ss.time_resolution}")
        if ss.vhdl_xlrm:
            vhdlan_args.append("-xlrm")
        if ss.functional_vital:
            vhdlan_args.append("-functional_vital")
        # every header's directory is an include directory, as for Verilator and yosys
        for d in self.design.header_dirs(tb=True):
            vlogan_args.append(f"+incdir+{d}")
        if self.design.language.vhdl.standard in ("08", "2008"):
            vhdlan_args.append("-vhdl08")
        elif self.design.language.vhdl.standard in ("02", "2002"):
            vhdlan_args.append("-vhdl02")
        if ss.vlogan_warns:
            vlogan_args.append(f"+warn={ss.vlogan_warns}")
        if ss.init_std_logic:
            vhdlan_args += ["-init_std_logic", str(ss.init_std_logic)]
        sources = self.sources_read(tb=True)
        vlog_files = [src for src in sources if src.type is SourceType.Verilog]
        if vlog_files:
            log.info("Analyzing Verilog sources")
            self.vlogan.run(*vlogan_args, *(str(f) for f in vlog_files), env=env)
        sv_files = [src for src in sources if src.type is SourceType.SystemVerilog]
        if sv_files:
            log.info("Analyzing SystemVerilog sources")
            self.vlogan.run(*vlogan_args, "-sverilog", *(str(f) for f in sv_files), env=env)
        vhdl_files = [src for src in sources if src.type is SourceType.Vhdl]
        if vhdl_files:
            log.info("Analyzing VHDL sources")
            self.vhdlan.run(*vhdlan_args, *(str(f) for f in vhdl_files), env=env)
        top = self.design.tb.top[0]
        if ss.verbose:
            vlogan_args.append("-notice")
            vhdlan_args.append("-verbose")
            vcs_args.append("-notice")
        if ss.lint:
            vcs_args.append(f"+lint={ss.lint}")
        if ss.vcs_warn:
            warns = ss.vcs_warn.split(",")
            warns += [f"no{w}" for w in ss.vcs_nowarn]
            vcs_args.append(f"+warn={','.join(warns)}")
        if ss.debug_region is not None:
            vcs_args.append(f"-debug_region={ss.debug_region}")
        if ss.debug_access:
            if isinstance(ss.debug_access, str):
                if ss.debug_access[0] not in ("+", "-", "="):
                    if ss.debug_access == "all":
                        ss.debug_access = "+all"
                    else:
                        raise FlowSettingsException(
                            "`debug_access` must be `all` or start with `+`, `-` or `=`"
                        )
                vcs_args.append(f"-debug_access{ss.debug_access}")
            else:
                vcs_args.append("-debug_access")
            vcs_args.append("+vcs+dumpvars")
        if ss.time_resolution:
            vcs_args.append(f"-sim_res={ss.time_resolution}")

        if ss.fsdb:
            vcs_args += [
                "+vcs+fsdbon",
            ]
            if ss.fsdb_size_limit is not None:
                simv_args.append(f"+fsdb+dump_limit={ss.fsdb_size_limit}")
        if ss.vpd and ss.vpd_size_limit is not None:
            simv_args.append(f"+vpdfilesize+{ss.vpd_size_limit}")

        if ss.sdf_file and ss.sdf_instance:
            vcs_args += [
                "-sdf",
                f"{ss.sdf_type}:{ss.sdf_instance}:{ss.sdf_file}",
            ]
            vcs_args.append("+sdfverbose")

        if self.design.tb.parameters:
            gfile = f"{top}.params"
            with replacing_file(self.run_directory.writable(gfile), encoding="utf-8") as f:
                for k, v in self.design.tb.parameters.items():
                    if v is None:
                        continue
                    elif isinstance(v, bool):
                        v = "1" if v else "0"
                    elif isinstance(v, str):
                        v = f'"{v}"'
                    f.write(f"assign {v} {k}\n")
            vcs_args += ["-lca", "-gfile", gfile]
        if ss.nthreads is not None:
            vcs_args.append(f"-j{ss.nthreads}")
        if ss.vcs_log_file:
            vcs_args += ["-l", str(ss.vcs_log_file)]

        log.info("Running vcs")
        if top:
            vcs_args += ["-top", top]
        if ss.initreg is not None:
            vcs_args.append("+vcs+initreg+random")
            simv_args.append(f"+vcs+initreg+{ss.initreg}")

        if not ss.one_shot_run:
            self.vcs.run(*vcs_args, env=env)
        script = prepare_vcs_runtime(
            self, setup=setup, user_script=ss.ucli_script, stop_time=stop_time
        )
        common_run_args += ["-ucli", "-i", str(script)]
        if ss.gui:
            if isinstance(ss.gui, str) and ss.gui.lower() not in ("true", "false", "1", "0"):
                common_run_args.append(f"-gui={ss.gui}")
            elif ss.gui is True:
                common_run_args.append("-gui")
        runtime_tool = self.vcs if ss.one_shot_run else self.simv
        runtime_tool.redirect_stdout = None
        if ss.one_shot_run:
            self.vcs.run(
                *vcs_args,
                "-R",
                *common_run_args,
                env=env,
                timeout=ss.timeout,
                tee=self.run_directory.writable(RUNTIME_LOG),
                merge_stderr=True,
            )
        else:
            if ss.sim_no_save:
                simv_args.append("-no_save")
            self.simv.run(
                *simv_args,
                *common_run_args,
                env=env,
                timeout=ss.timeout,
                tee=self.run_directory.writable(RUNTIME_LOG),
                merge_stderr=True,
            )
        if ss.to_vcd:
            if ss.fsdb:
                if not self.run_file(ss.fsdb).exists():
                    log.error(f"FSDB file {ss.fsdb} does not exist")
                else:
                    fsdb2vcd_args = [
                        "-consolidate_bus",
                        "-sv",
                    ]
                    self.fsdb2vcd.run(
                        ss.fsdb, *fsdb2vcd_args, "-o", ss.fsdb.with_suffix(".vcd"), check=True
                    )
            if ss.vpd:
                if not self.run_file(ss.vpd).exists():
                    log.error(f"VPD file {ss.vpd} does not exist")
                else:
                    vpd2vcd_args = [
                        "+morevhdl",
                    ]
                    self.vpd2vcd.run(ss.vpd, *vpd2vcd_args, ss.vpd.with_suffix(".vcd"), check=True)
