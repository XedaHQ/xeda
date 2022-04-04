"""Utilities for command line interface"""
import logging
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import click
from click_help_colors import HelpColorsGroup
from overrides import overrides

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


# xdg_home = os.environ.get('XDG_DATA_HOME', os.path.join(
#     os.environ.get('HOME', str(Path.home())), '.local', 'share'))
# completion_user_dir = os.environ.get(
#     'BASH_COMPLETION_USER_DIR', os.path.join(xdg_home, 'bash-completion'))
# dir = Path(completion_user_dir)
# if not dir.exists():
#     dir.mkdir(parents=True)
# completion_file = dir / 'xeda'
# with open(completion_file, 'w') as f:
#     f.write(completion)
# eager_completion = Path.home() / '.bash_completion'
# source_line = f'. {completion_file} # added by xeda'
# if eager_completion.exists():
#     with open(eager_completion, "r+") as f:
#         for line in f:
#             if source_line in line:
#                 break
#         else:  # else-for "completion clause"
#             f.write(source_line + os.linesep)
# else:
#     with open(eager_completion, "w") as f:
#         f.write(source_line + os.linesep)


def discover_flow_class(flow: str) -> Type[Flow]:
    try:
        return get_flow_class(flow, "xeda.flows", __package__)
    except FlowNotFoundError:
        log.critical(
            "Flow %s is not known to Xeda. Please make sure the name is correctly specified.",
            flow,
        )
        sys.exit(1)
