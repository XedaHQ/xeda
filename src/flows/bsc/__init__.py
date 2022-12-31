import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import Field

from ...design import Design
from ...flow import Flow
from ...gtkwave import get_color
from ...tool import Docker, Tool

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
        verilog_out_dir: Union[Path, str] = "gen_rtl"
        bobj_dir: str = "bobjs"
        reset_prefix: Optional[str] = None
        unspecified_to: Optional[str] = "X"
        opt_undetermined_vals: bool = True
        warn_flags: List[str] = [
            "-warn-method-urgency",
            "-warn-action-shadowing",
        ]
        werror: List[str] = Field(
            ["G0010", "G0005", "G0117"], description="Promote these warnings as errors."
        )
        optimize: bool = True
        docker: Optional[Docker] = Docker(image="bsc")  # pyright: ignore

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        self.bsc = Tool("bsc", docker="bsc")
        self.bluetcl = self.bsc.derive("bluetcl")

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
                        print(f"writing translation of {name} into {f.name}")
                        f.write(translate)

    def run(self):
        assert isinstance(self.settings, self.Settings)

        bluespec_sources = self.design.sources_of_type("Bluespec")
        top_file = bluespec_sources[-1]

        # bsc_exec = shutil.which("bsc")
        # assert bsc_exec, "`bsc` not found in PATH!"
        # BLUESPEC_PREFIX = os.path.dirname(os.path.dirname(bsc_exec))

        bsc_flags = []

        if self.settings.verbose:
            bsc_flags.append("-verbose")
        elif self.settings.quiet:
            bsc_flags.append("-quiet")

        def path_from_setting(p: Union[str, Path]) -> Path:
            p = str(p)
            if not os.path.isabs(p):
                if p.startswith("$DESIGN_ROOT/"):
                    return self.design.root_path / p
            return Path(p)

        vout_dir = path_from_setting(self.settings.verilog_out_dir)

        bobj_dir = Path(self.settings.bobj_dir).absolute()
        bobj_dir.mkdir(exist_ok=True)

        bsc_flags += [
            "-steps-max-intervals",
            "6000000",
            "-steps-warn-interval",
            "2000000",
            "-show-compiles",
            "-show-module-use",
            "-show-version",
        ]

        if self.settings.werror:
            bsc_flags += ["-promote-warnings", ":".join(self.settings.werror)]

        bsc_flags += self.settings.warn_flags

        bsc_flags += [
            "-bdir",
            str(bobj_dir),
            "-info-dir",
            str(bobj_dir),
        ]

        bsc_flags += [
            "-check-assert",
            "-cross-info",
            "-lift",
            "-use-proviso-sat",
            ##
            "-verilog-declare-first",
            ##
            "-warn-action-shadowing",
            "-warn-method-urgency",
            "-warn-undet-predicate",
        ]
        if self.settings.debug:
            bsc_flags += [
                "-keep-fires",
                "-keep-inlined-boundaries",
                "-show-schedule",
                "-keep-method-conds",
                "-sched-dot",
                # "-show-timestamps",
                "-show-range-conflict",
                "-show-method-conf",
                "-readable-mux",
            ]
        else:
            bsc_flags += [
                ##
                "-remove-empty-rules",
                "-remove-false-rules",
                "-remove-prim-modules",
                "-remove-starved-rules",
                "-remove-unused-modules",
                ##
                "-no-keep-fires",
                "-no-keep-inlined-boundaries",
                "-show-range-conflict",
                "-no-show-timestamps",  # regenerated files should be the same
                # '-aggressive-conditions',  # DO NOT USE!!! BUGGY!!
                # "-opt-mux-expand", ## Broken?!!! Leads to incorrect behavior
                "-opt-AndOr",
                "-opt-aggressive-inline",
                "-opt-bit-const",
                "-opt-bool",
                "-opt-final-pass",
                "-opt-if-mux",
                "-opt-join-defs",
                "-opt-mux",
                "-opt-mux-const",
                "-opt-sched",
            ]
        if self.settings.optimize:
            bsc_flags.append("-O")
        if self.settings.opt_undetermined_vals:
            bsc_flags.append("-opt-undetermined-vals")
        if self.settings.unspecified_to:
            bsc_flags += [
                "-unspecified-to",
                self.settings.unspecified_to,
            ]
        if self.settings.reset_prefix:
            bsc_flags += [
                "-reset-prefix",
                self.settings.reset_prefix,
            ]
        vout_dir.mkdir(exist_ok=True)

        flags = self.get_bsc_flags()
        verilog_paths = flags["vPath"]

        # bsc_flags += ["-vsearch", ":".join(verilog_paths)]

        if self.incremental:
            src_ir_paths = []
            for src in bluespec_sources[:-1]:
                dirname = src.path.parent
                p = str(dirname.absolute())
                if p not in src_ir_paths:
                    src_ir_paths.append(p)
            if src_ir_paths:
                src_ir_paths.insert(0, "+")
                bsc_flags += ["-p", ":".join(src_ir_paths)]
        else:
            for src in bluespec_sources[:-1]:
                self.bsc.run(*bsc_flags, "-u", src)

        bsc_defines = self.design.rtl.parameters

        verilog_defines: Dict[str, Any] = {}

        verilog_defines["BSV_NO_INITIAL_BLOCKS"] = None
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

        if self.incremental:
            log.warning("bsc won't re-generate the Verilog file if it was externally changed")
            vloggen_flags.append("-u")

        vloggen_flags += [
            "-vdir",
            str(vout_dir),
            "-verilog",
        ]

        self.bsc.run(*bsc_flags, *vloggen_flags, top_file)

        gtkwave_dir = vout_dir.parent / "gtkwave"
        self.write_gtkwave_tr(bobj_dir, "XoodyakLwc", gtkwave_dir)

        modules = get_use_mods(vout_dir, self.design.rtl.top)
        modules.insert(0, self.design.rtl.top)
        log.debug(f"modules: {modules}")
        verilog_sources: List[Path] = []
        for mod in modules:
            verilog_name = f"{mod}.v"
            verilog_path = vout_dir / verilog_name
            if verilog_path in verilog_sources:
                continue
            if verilog_path.exists():
                verilog_sources.append(verilog_path)
            else:
                for vpath in verilog_paths:
                    vpath = Path(vpath)
                    found_file = next(vpath.glob(os.path.join("**", verilog_name)), None)
                    if found_file:
                        print(f"Copying used verilog {found_file} to {verilog_path}")
                        shutil.copyfile(found_file, verilog_path, follow_symlinks=True)
                        verilog_sources.insert(0, verilog_path)
                        break

        for v in verilog_sources:
            prepend_to_file(
                v,
                ["`define " + (k if v is None else f"{k} {v}") for k, v in verilog_defines.items()],
            )

        self.artifacts.verilog = [src.resolve() for src in verilog_sources]

    def parse_reports(self):
        return True
