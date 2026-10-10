"""CLI parameter types that do not load flow implementations at construction."""

from collections.abc import Callable, Iterable
import re
from typing import Any, Optional, Sequence, Tuple, Union

import click
from click.shell_completion import CompletionItem

from .flow_catalog import flow_names

# Query commands can render documents; execution commands use a plain --json flag.
OUTPUT_FORMATS = ("table", "json", "jsonl", "yaml")
DOCUMENT_FORMATS = ("json", "yaml")
_SETTING_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]*")


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


def _is_key_value(token: str) -> bool:
    """`KEY=VALUE` with a KEY shaped like a settings key (never a path such as `dir/a=b.toml`)."""
    key, sep, _ = token.partition("=")
    return bool(sep) and _SETTING_KEY.fullmatch(key) is not None


class OptionEatAll(click.Option):
    """
    Taken from https://stackoverflow.com/questions/48391777/nargs-equivalent-for-options-in-click#answer-48394004.

    Takes `KEY=VALUE` items up to the next option or the first token that is not `KEY=VALUE`;
    the first item must be `KEY=VALUE`.
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
            """method to hook to the parser.process"""
            if not _is_key_value(value):
                raise click.UsageError(
                    f"{'/'.join(self.opts)} takes KEY=VALUE items; got {value!r}", ctx=ctx
                )
            value_list = [value]
            if self._eat_all_parser is not None and self.save_other_options:
                # grab every KEY=VALUE item up to the next option; a token without `=` (the
                # design file, a mistyped path, `dir/a=b.toml`) ends the list, so the command line's positional
                # arguments are never eaten as settings
                while state.rargs:
                    token = state.rargs[0]
                    if any(token.startswith(prefix) for prefix in self._eat_all_parser.prefixes):
                        break
                    if not _is_key_value(token):
                        break
                    value_list.append(state.rargs.pop(0))
            else:
                # grab everything remaining
                value_list += state.rargs
                state.rargs[:] = []

            # A repeated option (`-s a=1 -s b=2`) accumulates. Without this every occurrence
            # *replaced* the previous one -- click's "store" action -- so all but the last group of
            # settings were silently dropped, although the help text documents the repeated form.
            dest = getattr(self._eat_all_parser, "dest", None) or self.name
            previous = state.opts.get(dest)
            if isinstance(previous, tuple):
                value_list = [*previous, *value_list]

            # call the actual process
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
    """`--format` plus a `--json` shorthand, for commands whose output is a document."""

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
    """Click parameter type for a flow name.

    Resolution is delegated to `get_flow_class`, so every name it accepts works on the command
    line too -- the canonical snake_case name, the CamelCase class name, any alias, and dashes
    for underscores -- and an unknown name gets the resolver's close-match suggestions. The
    `click.Choice` base is kept for shell completion and `--help`.

    Returns the flow's canonical name, so downstream code never has to re-normalize.

    With `removed=True` a removed flow's name is accepted too, as its canonical name: `xeda
    scrub` only removes directories, and those of a removed flow's runs are still there.
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
            # Completion parses complete words too. It needs their spelling, not a flow model.
            if ctx is not None and ctx.resilient_parsing:
                return value
            from .flow_runner import FlowNotFoundError, get_flow_class
            from .flow_runner.settings_layers import FlowRemovedError

            if "+" in value:
                self.fail(
                    f"{value!r} is a flow chain: this command takes one flow. A chain is a "
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
    """`xeda run`'s request: one flow name, or a chain `FLOW[.OUTPUT][+FLOW[.OUTPUT]...]`.

    Returns the canonical request text -- each flow's canonical name, with its output
    qualifier when one was given, joined by `+` -- which for a single flow is that flow's
    name, as `FlowChoice` returns. A malformed chain is a usage error.
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
        """Complete a flow name as `Choice` does; once the token has a `+` or a `.`, complete
        the unfinished chain, each candidate the whole token."""
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
