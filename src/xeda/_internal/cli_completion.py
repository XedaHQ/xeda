"""Plain Click command classes for the shared completion command graph."""

import os
import sys
from collections.abc import MutableMapping, Sequence
from typing import Any, cast

import click


class _CompletionContext(click.Context):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.auto_envvar_prefix = None


class CompletionCommand(click.Command):
    """A command that parses declared environment variables without loading the help renderer."""

    context_class = _CompletionContext


class CompletionDispatchMixin:
    """Dispatch this command's completion requests through Xeda's local adapters."""

    def _main_shell_completion(
        self,
        ctx_args: MutableMapping[str, Any],
        prog_name: str,
        complete_var: str | None = None,
    ) -> None:
        dispatch_shell_completion(cast(click.Command, self), ctx_args, prog_name, complete_var)


def dispatch_shell_completion(
    command: click.Command,
    ctx_args: MutableMapping[str, Any],
    prog_name: str,
    complete_var: str | None = None,
) -> None:
    """Run a local adapter for one Click shell completion request."""
    if complete_var is None:
        complete_name = prog_name.replace("-", "_").replace(".", "_")
        complete_var = f"_{complete_name}_COMPLETE".upper()
    instruction = os.environ.get(complete_var)
    if not instruction:
        return
    from .cli_settings_completion import completion_class

    shell, _, action = instruction.partition("_")
    cls = completion_class(shell)
    if cls is None:
        sys.exit(1)
    complete = cls(command, ctx_args, prog_name, complete_var)
    if action == "source":
        click.echo(complete.source().encode(), nl=False)
        sys.exit(0)
    if action == "complete":
        click.echo(complete.complete().encode())
        sys.exit(0)
    sys.exit(1)


class CompletionGroup(CompletionDispatchMixin, click.Group):
    """A group that exposes the help command that click-extra adds to each execution group."""

    command_class = click.Command

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        # click-extra uses the declared name as the program name, including for `python -m xeda`
        # and library calls. Do the same here.
        return super().main(
            args=args,
            prog_name=prog_name or self.name,
            complete_var=complete_var,
            standalone_mode=standalone_mode,
            **extra,
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.add_command(
            click.Command(
                "help",
                help="Show help for a command.",
                context_settings={"auto_envvar_prefix": None},
                params=[
                    click.Argument(["command_path"], nargs=-1),
                    click.Option(
                        ["--search"],
                        help="Search all subcommands for matching options or descriptions.",
                    ),
                ],
            )
        )
