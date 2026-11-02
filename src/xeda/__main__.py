"""Support running the CLI as `python -m xeda`, equivalent to the `xeda` console script."""

from ._internal.cli import cli

if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter
