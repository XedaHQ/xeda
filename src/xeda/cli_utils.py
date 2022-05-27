"""Utilities for command line interface"""
import logging
import re
import sys
from dataclasses import dataclass
from functools import reduce
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import click
from click_help_colors import HelpColorsGroup
from overrides import overrides
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .console import console
from .dataclass import asdict
from .flow_runner import FlowNotFoundError, get_flow_class
from .flows.flow import Flow
from .utils import set_hierarchy, try_convert

__all__ = [
    "ClickMutex",
    "OptionEatAll",
    "ConsoleLogo",
    "XedaHelpGroup",
    "discover_flow_class",
]


log = logging.getLogger(__name__)


@dataclass
class XedaOptions:
    verbose: bool = False
    quiet: bool = False
    debug: bool = False


class ClickMutex(click.Option):
    """Mutual exclusion of options"""

    def __init__(self, *args, **kwargs):
        self.mutually_exclusive_with: list = kwargs.pop("mutually_exclusive_with", [])
        self.required_if: list = kwargs.pop("required_if", [])
        if self.mutually_exclusive_with:
            kwargs["help"] = (
                kwargs.get("help", "")
                + "Option is mutually exclusive with "
                + ", ".join(self.mutually_exclusive_with)
                + "."
            ).strip()
        if self.required_if:
            kwargs["help"] = (
                kwargs.get("help", "")
                + "Option is required only if "
                + " or ".join(self.mutually_exclusive_with)
                + " is specified "
            ).strip()
        super().__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        if self.name in opts:
            for mutex_opt in self.mutually_exclusive_with:
                if mutex_opt in opts:
                    raise click.UsageError(
                        f"Option {self.name} is mutually exclusive with {mutex_opt}."
                    )
        else:
            for dependent_opt in self.required_if:
                if dependent_opt in opts:
                    raise click.UsageError(
                        f"Option {self.name} is required when {dependent_opt} is specified."
                    )

        return super().handle_parse_result(ctx, opts, args)


class OptionEatAll(click.Option):
    """
    Taken from https://stackoverflow.com/questions/48391777/nargs-equivalent-for-options-in-click#answer-48394004.
    """  # noqa: ignore=E501 pylint: disable=C0301

    @overrides
    def __init__(self, *args, **kwargs):
        self.save_other_options = kwargs.pop("save_other_options", True)
        super().__init__(*args, **kwargs)
        self._previous_parser_process: Optional[
            Callable[[Union[str, Tuple[str, ...]], click.parser.ParsingState], None]
        ] = None
        self._eat_all_parser = None

    @overrides
    def add_to_parser(self, parser, ctx):
        def parser_process(value: str, state: click.parser.ParsingState):
            """method to hook to the parser.process"""
            value_list = [value]
            if self._eat_all_parser is not None and self.save_other_options:
                # grab everything up to the next option
                done = False
                while state.rargs:
                    for prefix in self._eat_all_parser.prefixes:
                        if state.rargs[0].startswith(prefix):
                            done = True
                            break
                    if done:
                        break
                    value_list.append(state.rargs.pop(0))
            else:
                # grab everything remaining
                value_list += state.rargs
                state.rargs[:] = []

            # call the actual process
            if self._previous_parser_process is not None:
                self._previous_parser_process(tuple(value_list), state)

        super().add_to_parser(parser, ctx)
        for name in self.opts:
            # pylint: disable=protected-access
            our_parser = parser._long_opt.get(name) or parser._short_opt.get(name)
            if our_parser:
                self._eat_all_parser = our_parser
                self._previous_parser_process = our_parser.process  # type: ignore
                our_parser.process = parser_process
                break


class ConsoleLogo:
    """Xeda ASCII logo on console"""

    logo: str = """
:==-+-:        :=-.-='.---==:::::::==-. .--=:::::::::=.          ,+==::::::+=-.
 .= {X}O{O} -=     .=--.==. *  ,...........:: *:  {X}........{O}  `=-      ,='            *
   :=:--=-  =-..-=.   +  .-:::::::::::' +: {X}=+++++++++-{O} `:+   ,=' .*########:  *
     -=.-:=-.-:=:     +  +:             +: {X}=+++++++++*.{O} `=- .=: :*########%:  *
       -==-.-=-       +  +: ,:::::::-,  +: {X}=++++++++++*:{O} := =: :*%########%:  *
      .==-.-==-       +  +: `:::::::='  +: {X}=++++++++++*:{O} := =: `*%#######%*'  *
     -= -:==. ==-     +  +:             +: {X}=++++++++++*:{O} .= =:                *
   := -==-  ==. ==-   *  `+..........-. +. {X}=+++++++++*:{O} .+  =: .==::::::::=.  *
 .:-.==:     .=- {X}O{O} =. :=+-............: *, {X}:++++++++'{O} .+-   =: -=          +  *
:=--==.        :=--.=: `=+:::::::::::-' `*-::::::::::+-'    `=-='          `-='
    """

    @classmethod
    def print(cls):
        """print the logo if console is wide enough"""
        if console.width >= 80:
            console.print(
                cls.logo.format(X="[dark_orange]", O="[/]"),
                highlight=False,
                emoji=False,
            )


class XedaHelpGroup(HelpColorsGroup):
    """How to display CLI help"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.help_headers_color = "yellow"
        self.help_options_color = "green"

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter):
        ConsoleLogo.print()
        super().format_usage(ctx, formatter)


# DictStrHier = Dict[str, "StrOrDictStrHier"]
DictStrHier = Dict[str, Any]
StrOrDictStrHier = Union[str, DictStrHier]


def settings_to_dict(
    settings: Union[
        None, List[str], Tuple[str, ...], Dict[str, StrOrDictStrHier], Flow.Settings
    ],
) -> Dict[str, Any]:
    if not settings:
        return {}
    if isinstance(settings, (tuple, list)):
        res: DictStrHier = {}
        for override in settings:
            sp = override.split("=")
            if len(sp) != 2:
                raise ValueError("Settings should be in KEY=VALUE format!")
            key, val = sp
            set_hierarchy(res, key, try_convert(val, convert_lists=True))
        return res
    if isinstance(settings, Flow.Settings):
        return asdict(settings)
    if isinstance(settings, dict):
        return settings
    raise TypeError(f"overrides is of unsupported type: {type(settings)}")


def print_flow_settings(flow, options: XedaOptions):
    flow_class = discover_flow_class(flow)
    schema = flow_class.Settings.schema(by_alias=True)
    type_defs = {}

    def get_type(field):
        if isinstance(field, (list, tuple)):
            return Text("|".join(str(get_type(f)) for f in field))
        typ = field.get("type")
        ref = field.get("$ref")
        if typ is None and ref:
            typ = ref.split("/")[-1]
            typ_def = schema.get("definitions", {}).get(typ)
            if typ not in type_defs:
                type_defs[typ] = typ_def
            elif type_defs[typ] != typ_def:
                log.critical(
                    "type definition for %s changed!\nPrevious def:\n %s new def:\n %s",
                    typ,
                    type_defs[typ],
                    typ_def,
                )
            return Text(typ, style="blue")
        additional = field.get("additionalProperties")
        if typ == "object" and additional:
            return Text(f"Dict[string -> {get_type(additional)}]")

        if typ == "array":
            items = field.get("items")
            if items:
                return Text(f"array[{get_type(items)}]")

        def join_types(lst, joiner, style="red"):
            return reduce(
                lambda x, y: x + Text(joiner, style) + y, (get_type(t) for t in lst)
            )

        allof = field.get("allOf")
        if allof:
            return join_types(allof, " & ")  # intersection
        anyOf = field.get("anyOf")
        if anyOf:
            return join_types(anyOf, " | ")  # union
        return Text(typ)

    table = Table(
        title=f"{flow} settings",
        show_header=True,
        header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    table.add_column("Property", header_style="bold green", style="bold")
    table.add_column("Type", max_width=32)
    table.add_column("Default", max_width=42)
    table.add_column("Description")

    def fmt_default(v: Any) -> str:
        if isinstance(v, str):
            return f'"{v}"'
        if isinstance(v, bool):
            return str(v).lower()
        return str(v)

    for name, field in schema.get("properties", {}).items():
        if name in ["results"] or name in Flow.Settings.__fields__.keys():
            continue
        required = name in schema.get("required", [])
        desc: str = field.get("description", "")
        typ = get_type(field)
        req_or_def = (
            "[red]<required>[/red]" if required else fmt_default(field.get("default"))
        )
        table.add_row(name, typ, req_or_def, desc)
    console.print(table)

    for typ_name, typ_def in list(type_defs.items()):
        for prop, prop_def in typ_def.get("properties").items():
            get_type(prop_def)
    for typ_name, typ_def in list(type_defs.items()):
        c = "blue"
        table = Table(
            title=f"Type [{c}]{typ_name}[/{c}]",
            show_header=True,
            header_style="bold green",
            title_style=Style(frame=True, bold=True),
            box=box.SQUARE_DOUBLE_HEAD,
            show_lines=True,
        )
        if options.debug:
            console.print(f"Type: {typ_name}")
            console.print_json(data=typ_def)
        table.add_column("property")
        table.add_column("type")
        table.add_column("description")
        for prop, prop_def in typ_def.get("properties").items():
            if prop_def.get("hidden_from_schema"):
                continue
            if (
                typ_name.endswith("__Settings")
                and prop in Flow.Settings.__fields__.keys()
            ):
                continue
            desc = prop_def.get("description", prop_def.get("title", "-"))
            desc = re.sub(r"\s*\.*\s*$", "", desc)
            table.add_row(prop, get_type(prop_def), desc)
        console.print(table)


def discover_flow_class(flow: str) -> Type[Flow]:
    try:
        return get_flow_class(flow, "xeda.flows", __package__)
    except FlowNotFoundError:
        log.critical(
            "Flow %s is not known to Xeda. Please make sure the name is correctly specified.",
            flow,
        )
        sys.exit(1)
