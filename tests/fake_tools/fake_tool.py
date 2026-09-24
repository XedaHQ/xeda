#!/usr/bin/env python3

import inspect
import logging
import os
import shutil
import subprocess
from pathlib import Path
from time import sleep
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)
from zipfile import ZipFile

import click

from xeda.dataclass import XedaBaseModel, asdict

log = logging.getLogger()

RESOURCE_DIR = Path(__file__).parent.absolute() / "resource"


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
    else:
        if data is None:
            data = []
        with open(path, "w") as f:
            if isinstance(data, list):
                f.writelines(data)
            else:
                f.write(data)


# The commands of the tool a script runs are recorded, not run: `unknown` catches every command
# tclsh does not know. The recording goes to `fake_<tool>.calls` in the working directory (the
# run directory), one `CALL <n>` line per command followed by its `ARG` lines -- and `ELEM` lines
# for the files of an argument that is a TCL list (`[list "a b.v"]`).
TCL_RECORDER = r"""
set __calls [open {%(calls)s} a]
proc __record {args} {
    puts $::__calls "CALL [llength $args]"
    foreach a $args {
        puts $::__calls "ARG $a"
        if {![catch {llength $a} n] && $n >= 1 && [lindex $a 0] ne $a} {
            foreach e $a { puts $::__calls "ELEM $e" }
        }
    }
    flush $::__calls
    return 1
}
proc unknown {args} { __record {*}$args }
rename source __source
proc source {args} { __record source {*}$args }
proc exec {args} { __record exec {*}$args; return "" }
rename package __package
proc package {sub args} {
    if {$sub eq "require"} { __record package require {*}$args; return 1 }
    __package $sub {*}$args
}
proc set_app_var {name value} { __record set_app_var $name $value; uplevel #0 [list set $name $value] }
set search_path {}
namespace eval rdi { variable mode batch }
rename exit __exit
proc exit {{code 0}} { flush $::__calls; __exit $code }
if {[catch {__source {%(script)s}} e]} {
    puts $::__calls "TCL-ERROR $e"
    puts stderr "TCL-ERROR in %(script)s: $e\n$::errorInfo"
    flush $::__calls
    __exit 1
}
flush $::__calls
"""


def run_tcl(script: Union[str, os.PathLike], tool_name: str) -> int:
    """Run `script` under tclsh the way the tool would, its commands recorded (`TCL_RECORDER`).
    A TCL error fails the fake tool as it fails the real one. Without tclsh the script is not run,
    unless `XEDA_TESTS_REQUIRE_TOOLS` asks for every tool a test uses."""
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        if os.environ.get("XEDA_TESTS_REQUIRE_TOOLS", "").lower() in ("1", "true", "yes", "on"):
            print(
                "fake tool: tclsh is needed to run the TCL script, and XEDA_TESTS_REQUIRE_TOOLS is set"
            )
            return 1
        return 0
    script = Path(script).absolute()
    calls = Path.cwd() / f"fake_{tool_name}.calls"
    runner = Path.cwd() / f"fake_{tool_name}_runner.tcl"
    runner.write_text(TCL_RECORDER % {"calls": calls, "script": script})
    return subprocess.run([tclsh, str(runner)], check=False).returncode


class RunTcl:
    """Execute the TCL script a fake tool is handed, taken from the named option or argument
    (`transform` extracts it, e.g. from `vsim -do "do x.tcl"`)."""

    def __init__(self, tool_name: str, param: str, transform=None, then=None) -> None:
        self.tool_name = tool_name
        self.param = param
        self.transform = transform
        self.then = then

    def __call__(self, **kwargs: Any) -> int:
        script = kwargs.get(self.param)
        if script and self.transform:
            script = self.transform(script)
        status = run_tcl(script, self.tool_name) if script else 0
        if status == 0 and self.then is not None:
            status = self.then(**kwargs)
        return status


@runtime_checkable
class Executer(Protocol):
    def __call__(self, **kwargs: Any) -> int: ...


class WriteFile(Executer):
    def __init__(
        self,
        path: Union[str, os.PathLike],
        data: Union[None, List[str], str, bytes] = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(path, Path):
            path = Path(path)
        self.path = path
        self.data = data
        super().__init__(**kwargs)

    def __call__(self, **kwargs) -> int:
        write_file(self.path, self.data)
        return 0


class TouchFiles(Executer):
    def __init__(self, *paths: Union[str, os.PathLike], **kwargs) -> None:
        self.paths = paths
        super().__init__(**kwargs)

    def __call__(self, **kwargs) -> int:
        for path in self.paths:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        return 0


class FakeTool(XedaBaseModel):
    version: Optional[str] = None
    version_template: Optional[str] = None
    vendor: Optional[str] = None
    help_options: list = ["--help"]
    version_options: list = ["--version"]
    options: dict = {}  # param_decls -> attrs
    arguments: dict = {}  # Dict[str, Optional[Dict[str, Any]]] = {}
    # arguments click cannot parse, rewritten first: an option may not start with a digit
    argv_aliases: dict = {}
    execute_: Executer = lambda **_kwargs: 0

    @property
    def version_banner(self) -> str:
        if self.version_template:
            return inspect.cleandoc(self.version_template.format(**(asdict(self))))
        return "unknown"

    def execute(self, **kwargs) -> int:
        return self.execute_(**kwargs)


class FakeVivado(FakeTool):
    vendor: Optional[str] = "Xilinx, Inc."
    version: Optional[str] = "v2021.2"
    version_template: Optional[str] = """Vivado {version} (64-bit)
        SW Build 1234567 on Tue Oct 11 01:23:45 MDT 2021
        IP Build 1234567 on Thu Oct 22 01:23:45 MDT 2021
        Copyright 1900-2021 {vendor} All Rights Reserved.
    """
    help_options: list = ["-help"]
    version_options: list = ["-version"]
    options: dict = {
        "-mode": ["gui", "tcl", "batch"],
        "-init": dict(type=click.Path(exists=True)),
        "-source": dict(type=click.Path(exists=True)),
        "-verbose": None,
        "-nojournal": None,
        "-notrace": None,
        "-nolog": None,
    }
    arguments: dict = {"project": dict(required=False)}

    def execute(self, **kwargs):
        print("cwd =", Path.cwd())
        tcl = kwargs.get("source")
        if tcl:
            status = run_tcl(tcl, "vivado")
            if status:
                return status
            sleep(0.3)
            with ZipFile(RESOURCE_DIR / "fake_vivado_reports") as zf:
                for file in zf.namelist():
                    if os.path.isdir(file):
                        continue
                    with zf.open(file) as rf:
                        data = rf.read()
                        write_file(Path("reports") / "route_design" / file, data)
        return 0


fake_tools: Dict[str, FakeTool] = dict(
    vivado=FakeVivado(),  # type: ignore
    quartus_sh=FakeTool(
        version="23.1std.0",
        version_template="Quartus Prime Shell\nVersion {version} Build 991 Lite Edition",
        options={"-t": dict(type=click.Path(exists=True), required=True)},
        execute_=RunTcl(
            "quartus_sh",
            "t",
            then=TouchFiles(
                "reports/Flow_Summary.csv",
                "reports/Fitter/Resource_Section/Fitter_Resource_Utilization_by_Entity.csv",
                "reports/Timing_Analyzer/Multicorner_Timing_Analysis_Summary.csv",
            ),
        ),
    ),
    xtclsh=FakeTool(
        version="14.7",
        arguments={"script": dict(required=False, type=click.Path(exists=True))},
        execute_=RunTcl("xtclsh", "script"),
    ),
    dc_shell=FakeTool(
        version="W-2024.09-SP2",
        version_template="dc_shell version    -  {version}",
        version_options=["-version"],
        argv_aliases={"-64bit": "--sixty-four-bit"},
        options={
            "-f": dict(type=click.Path(exists=True)),
            "--sixty-four-bit": None,
            "-topographical_mode": None,
            "-no_home_init": None,
            "-no_local_init": None,
            "-gui": None,
            "-output_log_file": dict(type=click.Path()),
        },
        execute_=RunTcl("dc_shell", "f"),
    ),
    diamondc=FakeTool(
        version="3.13.0.56.2",
        arguments={"script": dict(required=False, type=click.Path(exists=True))},
        execute_=RunTcl("diamondc", "script"),
    ),
    vsim=FakeTool(
        version="2024.1",
        version_template="Model Technology ModelSim vsim {version} Simulator",
        options={
            "-batch": None,
            "-do": dict(type=str),
            "-modelsimini": dict(type=click.Path()),
        },
        # `vsim -do "do run.tcl"`: the script is what the `do` command names
        execute_=RunTcl("vsim", "do", transform=lambda command: command.split(None, 1)[1]),
    ),
)

symlink_name = Path(__file__).stem

tool = fake_tools.get(symlink_name, FakeTool())


FC = Callable[..., Any]


def fake_tool_options(fake_tool: Optional[FakeTool]) -> FC:
    def decorator(f: FC) -> FC:
        print(f"fake_tool={fake_tool}")
        if fake_tool:
            f = click.group(
                invoke_without_command=True,
                context_settings=dict(help_option_names=fake_tool.help_options),
            )(f)
            f = click.version_option(
                fake_tool.version,
                *fake_tool.version_options,
                message=fake_tool.version_banner,
            )(f)
            for arg, attrs in fake_tool.arguments.items():
                if attrs is None:
                    attrs = {}
                f = click.argument(arg, **attrs)(f)
            for param_decls, param_attrs in fake_tool.options.items():
                if isinstance(param_decls, str):
                    param_decls = (param_decls,)
                if param_attrs is None:
                    param_attrs = dict(is_flag=True)
                elif isinstance(param_attrs, list):
                    param_attrs = dict(type=click.Choice(param_attrs))
                elif isinstance(param_attrs, type):
                    param_attrs = dict(type=param_attrs)
                f = click.option(*param_decls, **param_attrs)(f)
        return f

    return decorator


@fake_tool_options(tool)
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    """Dispatch a fake EDA tool invocation."""
    if tool:
        print(f"Fake {ctx.info_name} kwargs:{kwargs} args:{ctx.args}")
        ctx.exit(tool.execute(**kwargs) or 0)


if __name__ == "__main__":
    import sys

    sys.argv[1:] = [tool.argv_aliases.get(arg, arg) for arg in sys.argv[1:]]
    cli()  # pylint: disable=no-value-for-parameter
