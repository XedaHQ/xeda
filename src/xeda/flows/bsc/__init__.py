from functools import cached_property
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field

from ...design import SourceType
from ...flow import Flow
from ...gtkwave import get_color
from ...tool import Tool
from ...utils import unique

log = logging.getLogger(__name__)


def val_to_int(v):
    if isinstance(v, int):
        return v
    m = re.match(r"(\d+)'(b|d|h|o)(\d+)", v)
    if m:
        base_char = m.group(2)
        base = 10 if base_char == "d" else 2 if base_char == "b" else 16 if base_char == "h" else 8
        return int(m.group(3), base)
    return int(v)


def prepend_to_file(filename, lines):
    with open(filename, "r+") as f:
        to_add = "\n".join(lines) + "\n"
        content = f.read()
        if not content.startswith(to_add):
            f.seek(0, 0)
            f.write(to_add + content)


def get_use_mods(use_dir: Path, mod: str):
    use_path = use_dir / f"{mod}.use"
    uses = []
    if use_path.exists():
        with open(use_path) as f:
            for line in f.readlines():
                line = line.strip()
                if line not in uses:
                    uses.extend([x for x in get_use_mods(use_dir, line) if x not in uses])
                    uses.append(line)
    return uses


class Bsc(Flow):
    class Settings(Flow.Settings):
        verilog_out_dir: Path = Field(
            Path("gen_rtl"), description="Folder where generated Verilog files are stored."
        )
        bobj_dir: str = "bobjs"
        reset_prefix: Optional[str] = None
        positive_reset: bool = True
        sched_conditions: bool = Field(
            True, description="include method conditions when computing rule conflicts"
        )
        unspecified_to: Optional[str] = "X"
        warn_flags: List[str] = [
            "-warn-method-urgency",
            "-warn-action-shadowing",
            "-warn-undet-predicate",
        ]
        suppress_warnings: List[str] = []
        promote_warnings: List[str] = Field(
            ["G0009", "G0010", "G0005", "G0117"], description="Promote these warnings as errors."
        )
        optimize: bool = True
        extra_optimize_flags: List[str] = [
            "-opt-AndOr",  # An aggressive optimization of And Or expressions
            # "-opt-aggressive-inline", # aggressive inline of verilog assignments
            "-opt-bit-const",  # simplify bit operations with constants
            "-opt-bool",  # use BDD simplifier on booleans (slow but good)
            "-opt-final-pass",  # final pass optimization to unnest expression (et al)
            # "-opt-if-mux", # turn nested "if" into one mux
            # "-opt-if-mux-size", 4, # maximum mux size to inline when doing -opt-if-mux
            "-opt-join-defs",  # join identical definitions
            "-opt-mux",  # simplify muxes
            "-opt-mux-const",  # simplify constants in muxes aggressively
            # "-opt-mux-expand", # simplify muxes by blasting constants. Broken?!!! Leads to incorrect behavior
            "-opt-sched",  # simplify scheduler expressions
        ]
        opt_undetermined_vals: bool = True
        split_if: bool = Field(
            False,
            description="split 'if' in actions [See 'Bluespec Compiler User Guide' section 3.10]",
        )
        lift: bool = Field(
            True,
            description="lift method calls in 'if' actions [See 'Bluespec Compiler User Guide' section 3.10]",
        )
        aggressive_conditions: bool = Field(
            False,
            description="Aggressively propagate implicit conditions of actions in if-statements to the rule predicate [See 'Bluespec Compiler User Guide' section 3.10].",
        )
        synthesize_to_boolean: bool = Field(
            False, description="Synthesize all primitives into simple boolean ops"
        )
        haskell_runtime_flags: List[str] = Field(
            ["-K128M", "-H1G"],
            description="Flags passed along to the Haskell compiler run-time system that is used to execute BSC.",
        )
        gtkwave_package: Optional[str] = Field(
            None, description="Generate gtkwave translation filters for Bluespec types."
        )
        docker: Optional[str] = "bsc"
        incremental: bool = Field(False, description="Only compile modified packages.")
        cleanup_bobjs: bool = Field(
            True,
            description="Remove _all_ '.bo' files from `bobj_dir` before running. BSC randomly skips updating compilation artifacts, resultling to very weired bugs. Highly recommended to keep it enabled.",
        )

    @cached_property
    def bsc(self):
        return Tool("bsc", docker="bsc", version_flag="-v")

    @cached_property
    def bluetcl(self):
        return self.bsc.derive("bluetcl")

    def get_bsc_flags(self):
        def convert_value(v: str):
            list_regex = re.compile(r"\s*\[(.*)\]\s*")
            str_regex = re.compile(r'\s*"(.*)"\s*')
            match = list_regex.match(v)
            if match:
                return [convert_value(s) for s in match.group(1).split(",")]
            match = str_regex.match(v)
            if match:
                return match.group(1)
            return v

        kv_regex = re.compile(r"^\s+(\w+)\s=\s(.*),\s*$")
        flags = {}
        stdout = self.bsc.run("-print-flags-raw", stdout=True, check=True)
        if stdout:
            for line in stdout.splitlines():
                match = kv_regex.match(line)
                if match:
                    flags[match.group(1)] = convert_value(match.group(2))
        return flags

    def write_gtkwave_tr(self, bobj_dir, package, gtkwave_dir: Path):
        import vcd
        import vcd.gtkw

        script = self.copy_from_template("bluetcl_typeinfo.tcl")

        out = self.bluetcl.run(script, bobj_dir, package, stdout=True)

        assert out

        if self.settings.debug:
            print(f"output:\n{out}")

        type_info = dict(yaml.full_load(out))

        for name, t in type_info.items():
            if t["type"] == "Enum":
                members = t["members"]
                kvs = [re.split(r"\s*=\s*", x.strip()) for x in members]
                tr = [
                    (val_to_int(x[1]) if len(x) == 2 else i, x[0], get_color(i))
                    for i, x in enumerate(kvs)
                ]
                width = t.get("width")
                if width is not None:
                    sz = int(width)
                    datafmt = "hex" if sz >= 4 else "bin"
                    translate = vcd.gtkw.make_translation_filter(tr, datafmt=datafmt, size=sz)
                    gtkwave_dir.mkdir(exist_ok=True, parents=True)
                    with open(gtkwave_dir / (name + ".gwtr"), "w") as f:
                        log.info(f"writing translation of {name} into {f.name}")
                        f.write(translate)

    def run(self):
        assert isinstance(self.settings, self.Settings)

        bluespec_sources = self.design.sources_of_type(SourceType.Bluespec, rtl=True, tb=True)
        verilog_sources = self.design.sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, rtl=True, tb=True
        )
        top_file = bluespec_sources[-1]
        bsc_flags = []

        if self.settings.verbose:
            bsc_flags.append("-verbose")
        elif self.settings.quiet:
            bsc_flags.append("-quiet")

        vout_dir = self.settings.verilog_out_dir
        bobj_dir = Path(self.settings.bobj_dir).absolute()
        if self.settings.cleanup_bobjs and bobj_dir.exists():
            for bobj in bobj_dir.glob("*.bo"):
                if bobj.is_file():
                    bobj.unlink()
        bobj_dir.mkdir(exist_ok=True)

        bsc_flags += [
            "-steps-max-intervals",
            "6000000",
            "-steps-warn-interval",
            "2000000",
            "-show-compiles",  # enabled by default
            "-show-module-use",
            "-show-version",
        ]
        if not self.settings.split_if and not self.settings.lift:
            log.warning(
                "Lifting (lift=true) is recommended when rule splitting is turned off (split_if=false)."
            )
        if self.settings.split_if and self.settings.lift:
            log.warning(
                "When rule splitting is on (split_if=true), lifting is not required and can make rules more resource hungry."
            )
        if self.settings.split_if:
            self.settings.lift = False
            self.settings.aggressive_conditions = True
        bsc_flags.append("-split-if" if self.settings.split_if is True else "-no-split-if")
        bsc_flags.append("-lift" if self.settings.lift is True else "-no-lift")
        if self.settings.aggressive_conditions:
            bsc_flags.append("-aggressive-conditions")

        if self.settings.synthesize_to_boolean:
            bsc_flags.append("-synthesize")
        if self.settings.promote_warnings:
            bsc_flags += ["-promote-warnings", ":".join(self.settings.promote_warnings)]
        bsc_flags += self.settings.warn_flags
        if self.settings.suppress_warnings:
            bsc_flags += [
                "-suppress-warnings",
                ":".join(self.settings.suppress_warnings),
            ]

        bsc_flags += [
            "-bdir",
            str(bobj_dir),
            "-info-dir",
            str(bobj_dir),
        ]

        bsc_flags += [
            "-check-assert",
            "-use-proviso-sat",
            ##
            "-verilog-declare-first",
            "-show-range-conflict",
            ##
        ]
        if self.settings.debug:
            bsc_flags += [
                "-show-schedule",
                "-show-elab-progress",
                "-keep-method-conds",
                "-sched-dot",
                "-show-method-conf",
                "-readable-mux",
                "-cross-info",  # apply heuristics for preserving source code positions
                "-keep-fires",
                "-keep-inlined-boundaries",
                "-show-timestamps",  # enabled by default
                "-show-stats",
            ]
        else:
            bsc_flags += [
                "-remove-false-rules",  # enabled by default
                # "-remove-prim-modules",  # remove primitives that are local modules
                "-remove-empty-rules",  # enabled by default
                "-remove-starved-rules",
                "-remove-unused-modules",
                ##
                "-no-keep-fires",
                "-no-keep-inlined-boundaries",
                ##
                "-no-show-timestamps",  # regenerated files should be the same
            ]
        if self.settings.optimize:
            bsc_flags.append("-O")
            bsc_flags += self.settings.extra_optimize_flags
            if self.settings.opt_undetermined_vals:
                bsc_flags.append("-opt-undetermined-vals")
        uv = self.settings.unspecified_to
        if (
            not self.settings.optimize
            or not self.settings.opt_undetermined_vals
            and uv
            and uv == "X"
        ):
            uv = None
        if uv:
            bsc_flags += [
                "-unspecified-to",
                uv,
            ]
        if self.settings.reset_prefix:
            bsc_flags += [
                "-reset-prefix",
                self.settings.reset_prefix,
            ]
        vout_dir.mkdir(exist_ok=True)

        if verilog_sources:
            vsearch_paths = unique([str(p.path.parent) for p in verilog_sources])
            bsc_flags += ["-vsearch", "+:" + ":".join(vsearch_paths)]

        bsc_defines = self.design.rtl.parameters

        verilog_defines: Dict[str, Any] = {}

        # verilog_defines["BSV_NO_INITIAL_BLOCKS"] = None

        if self.settings.positive_reset:
            bsc_defines["BSV_POSITIVE_RESET"] = None
            verilog_defines["BSV_POSITIVE_RESET"] = None

        for param_name, param_value in bsc_defines.items():
            bsc_flags.extend(
                [
                    "-D",
                    f"{param_name}" if param_value is None else f"{param_name}={param_value}",
                ]
            )

        vloggen_flags = [
            "-remove-dollar",
        ]

        assert self.design.rtl.top, "design.rtl.top must be specified"
        vloggen_flags += ["-g", self.design.rtl.top]

        if self.incremental and self.settings.incremental:
            src_ir_paths = []
            for src in bluespec_sources[:-1]:
                dirname = src.path.parent
                p = str(dirname.absolute())
                if p not in src_ir_paths:
                    src_ir_paths.append(p)
            if src_ir_paths:
                src_ir_paths.insert(0, "+")
                bsc_flags += ["-p", ":".join(src_ir_paths)]
            log.warning("bsc won't re-generate the Verilog file if it was externally changed")
            vloggen_flags.append("-u")
        else:
            for src in bluespec_sources[:-1]:
                self.bsc.run(*bsc_flags, "-u", src)

        vloggen_flags += [
            "-vdir",
            str(vout_dir),
            "-verilog",
        ]

        self.bsc.run(*bsc_flags, *vloggen_flags, top_file)

        if self.settings.gtkwave_package:
            gtkwave_dir = vout_dir.parent / "gtkwave"
            self.write_gtkwave_tr(bobj_dir, self.settings.gtkwave_package, gtkwave_dir)

        modules = get_use_mods(vout_dir, self.design.rtl.top)
        modules.insert(0, self.design.rtl.top)
        log.debug(f"modules: {modules}")

        gen_verilog_files: List[Path] = []
        flags = self.get_bsc_flags()
        verilog_paths = unique(flags["vPath"])
        for mod in modules:
            verilog_name = f"{mod}.v"
            verilog_path = vout_dir / verilog_name
            if verilog_path in gen_verilog_files:
                continue
            if verilog_path.exists():
                gen_verilog_files.append(verilog_path)
            else:
                for vpath in verilog_paths:
                    vpath = Path(vpath)
                    found_file = next(vpath.glob(os.path.join("**", verilog_name)), None)
                    if found_file:
                        log.info(
                            "Copying imported Verilog file `%s` to `%s`", found_file, verilog_path
                        )
                        shutil.copyfile(found_file, verilog_path, follow_symlinks=True)
                        gen_verilog_files.insert(0, verilog_path)
                        break

        for v in gen_verilog_files:
            prepend_to_file(
                v,
                ["`define " + (k if v is None else f"{k} {v}") for k, v in verilog_defines.items()],
            )

        self.artifacts.verilog = [src.resolve() for src in gen_verilog_files]

    def parse_reports(self):
        return True
