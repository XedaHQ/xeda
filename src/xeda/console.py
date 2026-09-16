"""The shared rich console used for all human-facing output."""

from typing import TextIO

from rich.console import Console

__all__ = ["console", "redirect_console"]

console = Console()


def redirect_console(stream: TextIO) -> None:
    """Send all rich console output to `stream`.

    The CLI's machine-readable modes point this at `sys.stderr` so that human-facing tables,
    prompts and progress never mix into the JSON document on stdout.
    """
    console.file = stream
