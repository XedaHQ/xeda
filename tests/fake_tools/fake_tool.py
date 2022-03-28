#!/usr/bin/env python3

import logging
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Set, Union
from pathlib import Path
import click
import inspect
import os
from zipfile import ZipFile
from xeda.utils import XedaBaseModel

log = logging.getLogger()

RESOURCE_DIR = Path(__file__).parent.absolute() / "resource"


class FakeTool(XedaBaseModel):
    name: str
    executables: Set[str]
    version: str
    version_template: str
    vendor: str
    help_options: list
    version_options: list
    # params: Dict[
    #     Union[str, Tuple[str, ...]], Union[None, Type, List[str], Dict[str, Any]]
    # ] = {}  # param_decls -> attrs
    argument: dict = {}  # Dict[str, Optional[Dict[str, Any]]] = {}

    @property
    def version_banner(self):
        return inspect.cleandoc(self.version_template.format(**(self.dict())))

    def execute(self, *args, **kwargs) -> int:
        return 0

    def generate_file(
        self, path: Path, data: Union[None, List[str], str, bytes] = None
    ):
        if not path.parent.exists():
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


class FakeVivado(FakeTool):
    name = "Vivado"
    executables: Set[str] = {"vivado"}
    vendor = "Xilinx, Inc."
    version = "v2021.2"
    version_template = """Vivado {version} (64-bit)
        SW Build 3367213 on Tue Oct 19 02:47:39 MDT 2021
        IP Build 3369179 on Thu Oct 21 08:25:16 MDT 2021
        Copyright 1986-2021 {vendor} All Rights Reserved.
    """
    help_options = ["-help"]
    version_options = ["-version"]
    params = {
        "-mode": ["gui", "tcl", "batch"],
        "-init": str,
        "-source": str,
        "-verbose": None,
        "-nojournal": None,
        "-notrace": None,
        "-nolog": None,
    }
    argument = {"project": dict(required=False)}

    def execute(self, *args, **kwargs):
        print("cwd =", Path.cwd())
        tcl = kwargs.get("source")
        assert tcl
        tcl = Path(tcl)
        assert tcl.exists()
        sleep(0.3)
        with ZipFile(RESOURCE_DIR / "fake_vivado_reports") as zf:
            for file in zf.namelist():
                if os.path.isdir(file):
                    continue
                with zf.open(file) as rf:
                    data = rf.read()
                    self.generate_file(Path("reports") / "post_route" / file, data)


fake_tools: Dict[str, FakeTool] = dict(vivado=FakeVivado())

symlink_name = Path(__file__).stem

tool = fake_tools.get(symlink_name)


FC = Callable[..., Any]


def fake_tool_options(fake_tool: Optional[FakeTool]) -> FC:
    def decorator(f: FC) -> FC:
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
            for arg, attrs in fake_tool.argument.items():
                if attrs is None:
                    attrs = {}
                f = click.argument(arg, **attrs)(f)
            for param_decls, param_attrs in fake_tool.params.items():
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
        print(f"Fake {tool.name} {kwargs} args:{ctx.args}")
        tool.execute(**kwargs)


if __name__ == "__main__":
    cli()
