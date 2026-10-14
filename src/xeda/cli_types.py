"""CLI parameter types that do not load flow implementations at construction."""

from collections.abc import Callable, Iterable
import re
from typing import Any, Optional, Sequence, Tuple, Union

import click
from click.shell_completion import CompletionItem

from .flow_catalog import flow_names

# Query commands can render documents. Execution commands take a plain `--json` option.
OUTPUT_FORMATS = ("table", "json", "jsonl", "yaml")
DOCUMENT_FORMATS = ("json", "yaml")
#: The tiers of a setting, from the one a user needs most often (`xeda.dataclass.TIERS`, which a
#: test keeps equal: this module stays free of the settings models, so that completion is fast).
TIER_NAMES = ("common", "advanced", "expert")
_SETTING_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]*")
#: The start of a key that continues with a name of the tool (`set_synth_properties.`), written
#: from the table of the setting or from a `flows.<flow>` section. What follows may hold spaces
#: and signs.
_OPAQUE_KEY_START = re.compile(r"(?:flows\.[A-Za-z_][A-Za-z0-9_-]*\.)?([A-Za-z_][A-Za-z0-9_]*)\.")


def _opaque_key_fields() -> frozenset[str]:
    """Return the settings of all flows whose keys are names of the tool. The function runs only
    when a key is not shaped like a setting key, so a plain `-s` pays nothing for it."""
    from . import flows as _flows  # noqa: F401  (registers the built-in flows)
    from .dataclass import opaque_key_fields_of
    from .flow import registered_flows

    return frozenset(
        name
        for _, flow_class in list(registered_flows.values())
        for name in opaque_key_fields_of(flow_class.Settings)
    )


def _is_key_value(token: str) -> bool:
    """Return whether `token` is `KEY=VALUE` with a KEY shaped like a setting key, and never a
    path such as `dir/a=b.toml`. A key may also continue after the name of a setting whose keys
    are names of the tool, with the characters that such a name has
    (`set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS`)."""
    key, sep, _ = token.partition("=")
    if not sep:
        return False
    if _SETTING_KEY.fullmatch(key) is not None:
        return True
    start = _OPAQUE_KEY_START.match(key)
    return start is not None and start.group(1) in _opaque_key_fields()


class OptionEatAll(click.Option):
    """An option that takes `KEY=VALUE` items up to the next option or the first token that is not
    `KEY=VALUE`. The first item must be `KEY=VALUE`.

    Adapted from
    https://stackoverflow.com/questions/48391777/nargs-equivalent-for-options-in-click#answer-48394004.
    """

    def __init__(self, *args, **kwargs):
        self.save_other_options = kwargs.pop("save_other_options", True)
        super().__init__(*args, **kwargs)
        self._previous_parser_process: Optional[
            Callable[[Union[str, Tuple[str, ...]], Any], None]
        ] = None
        self._eat_all_parser = None

    def add_to_parser(self, parser, ctx):
        def parser_process(value: str, state):
            """Replace `process` of the option parser."""
            if not _is_key_value(value):
                raise click.UsageError(
                    f"{'/'.join(self.opts)} takes KEY=VALUE items; got {value!r}", ctx=ctx
                )
            value_list = [value]
            if self._eat_all_parser is not None and self.save_other_options:
                # Take every KEY=VALUE item up to the next option. A token without `=` (the
                # design file, a mistyped path, `dir/a=b.toml`) ends the list, so the positional
                # arguments of the command line are never taken as settings.
                while state.rargs:
                    token = state.rargs[0]
                    if any(token.startswith(prefix) for prefix in self._eat_all_parser.prefixes):
                        break
                    if not _is_key_value(token):
                        break
                    value_list.append(state.rargs.pop(0))
            else:
                # Take everything that remains.
                value_list += state.rargs
                state.rargs[:] = []

            # A repeated option (`-s a=1 -s b=2`) accumulates. Without this code, every
            # occurrence would replace the previous one (the "store" action of click), and all but
            # the last group of settings would be dropped without a message, although the help
            # text documents the repeated form.
            dest = getattr(self._eat_all_parser, "dest", None) or self.name
            previous = state.opts.get(dest)
            if isinstance(previous, tuple):
                value_list = [*previous, *value_list]

            # Call the original process.
            if self._previous_parser_process is not None:
                self._previous_parser_process(tuple(value_list), state)

        super().add_to_parser(parser, ctx)
        for name in self.opts:
            # pylint: disable=protected-access
            our_parser = parser._long_opt.get(name) or parser._short_opt.get(name)
            if our_parser:
                self._eat_all_parser = our_parser
                self._previous_parser_process = our_parser.process  # pyright: ignore
                our_parser.process = parser_process
                break


def output_format_option(
    formats: Sequence[str] = OUTPUT_FORMATS, default: str = "table"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Return the decorator that adds `--format` and the `--json` shorthand to a command whose
    output is a document."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func = click.option(
            "--json",
            "json_flag",
            is_flag=True,
            default=False,
            help="Shorthand for `--format json`.",
        )(func)
        func = click.option(
            "--format",
            "output_format",
            type=click.Choice(list(formats)),
            default=default,
            show_default=True,
            help=(
                "Output format. json/jsonl/yaml are machine-readable, are never truncated to the "
                "terminal width, and are written to stdout on their own."
            ),
        )(func)
        return func

    return decorator


class FlowChoice(click.Choice[str]):
    """The Click parameter type for a flow name.

    `get_flow_class` resolves the name, so every name that it accepts works on the command line:
    the canonical snake_case name, the CamelCase class name, any alias, and dashes for
    underscores. An unknown name gets the close-match suggestions of the resolver. The
    `click.Choice` base class serves shell completion and `--help`.

    The converted value is the canonical name of the flow, so downstream code never has to
    normalize it again.

    With `removed=True`, the name of a removed flow is accepted too, as its canonical name:
    `xeda scrub` only removes directories, and the run directories of a removed flow still exist.
    """

    def __init__(
        self,
        choices: Iterable[str] | Callable[[], Iterable[str]] = flow_names,
        *,
        removed: bool = False,
        **kwargs: Any,
    ) -> None:
        self._choices_provider = choices if callable(choices) else None
        super().__init__(() if callable(choices) else choices, **kwargs)
        self.removed = removed

    @property
    def choices(self) -> tuple[str, ...]:
        if self._choices_provider is not None:
            return tuple(self._choices_provider())
        return self._fixed_choices

    @choices.setter
    def choices(self, value: Iterable[str]) -> None:
        self._fixed_choices = tuple(value)

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if isinstance(value, str):
            # Completion parses complete words too. It needs their spelling and no flow model.
            if ctx is not None and ctx.resilient_parsing:
                return value
            from .flow_runner import FlowNotFoundError, get_flow_class
            from .flow_runner.settings_layers import FlowRemovedError

            if "+" in value:
                self.fail(
                    f"{value!r} is a chain: this command takes one flow. A chain is a "
                    "request for `xeda run`.",
                    param,
                    ctx,
                )
            try:
                return get_flow_class(value).name
            except FlowRemovedError as e:
                if self.removed and e.flow_name:
                    return e.flow_name
                self.fail(str(e), param, ctx)
            except FlowNotFoundError as e:
                self.fail(str(e), param, ctx)
        return super().convert(value, param, ctx)


class ChainChoice(FlowChoice):
    """The request of `xeda run`: one flow name, or a chain `FLOW[.OUTPUT][+FLOW[.OUTPUT]...]`.

    The converted value is the canonical request text: the canonical name of each flow, with its
    output qualifier when one was given, joined by `+`. For a single flow, this is the name of
    that flow, as `FlowChoice` returns it. A malformed chain is a usage error.
    """

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if isinstance(value, str) and ctx is not None and ctx.resilient_parsing:
            return value
        if not isinstance(value, str) or not ("+" in value or "." in value):
            return super().convert(value, param, ctx)
        from .flow_runner.chains import parse_request, request_text
        from .flow_runner import FlowNotFoundError
        from .utils import XedaException

        try:
            request = parse_request(value)
        except (FlowNotFoundError, XedaException) as e:
            self.fail(str(e), param, ctx)
        return request_text(request.elements)

    def shell_complete(
        self, ctx: click.Context, param: click.Parameter, incomplete: str
    ) -> list[CompletionItem]:
        """Complete a flow name as `Choice` does. Once the token has a `+` or a `.`, complete the
        unfinished chain, with the whole token as each candidate."""
        if "+" not in incomplete and "." not in incomplete:
            return super().shell_complete(ctx, param, incomplete)
        import sys

        if "xeda.flow.flow" not in sys.modules:
            from .flow_catalog import complete_builtin_request

            candidates = complete_builtin_request(incomplete)
            return [CompletionItem(candidate) for candidate in candidates]
        from .flow_runner.chains import complete_request
        from .introspect import all_flow_classes

        return [
            CompletionItem(candidate)
            for candidate in complete_request(incomplete, all_flow_classes())
        ]
