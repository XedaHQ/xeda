"""The shared rich console used for all human-facing output."""

from typing import IO, Optional, TextIO

from rich.console import Console

__all__ = ["console", "console_target", "redirect_console", "restore_console"]

console = Console()


def console_target() -> Optional[IO[str]]:
    """The console's explicit output target, or `None` when it follows `sys.stdout`."""
    # rich exposes no getter for the *unresolved* target: `Console.file` resolves `None`
    # to `sys.stdout`, which would defeat the point of snapshotting it.
    return console._file


def redirect_console(stream: TextIO) -> Optional[IO[str]]:
    """Send all rich console output to `stream`, returning the previous target.

    The CLI's machine-readable modes point this at `sys.stderr` so that human-facing tables,
    prompts and progress never mix into the JSON document on stdout. Pass the returned value to
    `restore_console` to undo it -- the console is module-global, so a redirection left in place
    outlives the command that made it.
    """
    previous = console_target()
    console.file = stream
    return previous


def restore_console(previous: Optional[IO[str]] = None) -> None:
    """Undo `redirect_console`. The default `None` restores rich's own behavior.

    A `file` of `None` makes rich resolve the stream at write time, which is what lets the
    console follow a reassigned `sys.stdout` -- as `click.testing.CliRunner` does for each
    invocation. Restoring a captured stream object instead would pin the console to a stream
    belonging to an invocation that has already finished.
    """
    console.file = previous  # type: ignore[assignment]
