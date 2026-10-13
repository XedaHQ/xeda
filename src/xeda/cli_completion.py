"""Plain Click command classes for the shared completion command graph."""

from collections.abc import Sequence
from typing import Any

import click


class _CompletionContext(click.Context):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.auto_envvar_prefix = None


class CompletionCommand(click.Command):
    """A command that parses declared environment variables without loading the help renderer."""

    context_class = _CompletionContext


class CompletionGroup(click.Group):
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
