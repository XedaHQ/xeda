# © 2022-2025 [Kamyar Mohajerani](mailto:kammoh@gmail.com)
"""Support running the CLI as `python -m xeda`, equivalent to the `xeda` console script."""

from .cli import cli

if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter
