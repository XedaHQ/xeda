#!/usr/bin/env python3

import inspect
import logging
import os
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
from xeda.dataclass import asdict, XedaBaseModel

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


@runtime_checkable
class Executer(Protocol):
    def __call__(self, **kwargs: Any) -> int:
        ...


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
    execute_: Executer = lambda **_kwargs: 0

    @property
    def version_banner(self) -> str:
        if self.version_template:
            return inspect.cleandoc(self.version_template.format(**(asdict(self))))
        return "unknown"

    def execute(self, **kwargs) -> int:
        return self.execute_(**kwargs)


class FakeVivado(FakeTool):
    vendor = "Xilinx, Inc."
    version = "v2021.2"
    version_template = """Vivado {version} (64-bit)
        SW Build 1234567 on Tue Oct 11 01:23:45 MDT 2021
        IP Build 1234567 on Thu Oct 22 01:23:45 MDT 2021
        Copyright 1900-2021 {vendor} All Rights Reserved.
    """
    help_options = ["-help"]
    version_options = ["-version"]
    options = {
        "-mode": ["gui", "tcl", "batch"],
        "-init": dict(type=click.Path(exists=True)),
        "-source": dict(type=click.Path(exists=True)),
        "-verbose": None,
        "-nojournal": None,
        "-notrace": None,
        "-nolog": None,
    }
    arguments = {"project": dict(required=False)}

    def execute(self, **kwargs):
        print("cwd =", Path.cwd())
        tcl = kwargs.get("source")
        if tcl:
            sleep(0.3)
            with ZipFile(RESOURCE_DIR / "fake_vivado_reports") as zf:
                for file in zf.namelist():
                    if os.path.isdir(file):
                        continue
                    with zf.open(file) as rf:
                        data = rf.read()
                        write_file(Path("reports") / "route_design" / file, data)


fake_tools: Dict[str, FakeTool] = dict(
    vivado=FakeVivado(),  # type: ignore
    quartus_sh=FakeTool(
        options={"-t": dict(type=click.Path(exists=True), required=True)},
        execute_=TouchFiles(
            "reports/Flow_Summary.csv",
            "reports/Fitter/Resource_Section/Fitter_Resource_Utilization_by_Entity.csv",
            "reports/Timing_Analyzer/Multicorner_Timing_Analysis_Summary.csv",
        ),
    ),
    xtclsh=FakeTool(
        arguments={"script": dict(required=False, type=click.Path(exists=True))}
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
    if tool:
        print(f"Fake {ctx.info_name} kwargs:{kwargs} args:{ctx.args}")
        tool.execute(**kwargs)


if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter
