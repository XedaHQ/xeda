"""A hidden option is never suggested.

Xeda keeps a few options hidden only to say what replaced them (`--xeda-run-dir`, `--cwd`, ...).
click suggests the close matches of a mistyped option from every option the command has, so a
suggestion could send a user straight to one of them. Every command leaves a hidden option out of
its suggestions, whichever command it is.
"""

import json
from collections.abc import Iterator

import click
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.cli_utils import XedaCommand, XedaHelpGroup


def _commands(
    command: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    """Every command of the tree under `command`, with the names that reach it."""
    yield path, command
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            yield from _commands(sub, (*path, name))


def _hidden_names(command: click.Command) -> list[str]:
    return [
        name
        for param in command.params
        if getattr(param, "hidden", False)
        for name in (*param.opts, *param.secondary_opts)
    ]


HIDDEN = [(path, name) for path, command in _commands(cli) for name in _hidden_names(command)]


def _mistyped(name: str) -> str:
    """`name` as a user who lost its last letter would type it: a close match of it."""
    return name[:-1]


def test_the_tree_has_hidden_options_for_the_sweep_to_cover():
    commands = {" ".join(path) for path, _ in HIDDEN}

    assert {"run", "dse", "scrub"} <= commands


@pytest.mark.parametrize(
    ("path", "name"), HIDDEN, ids=[f"{' '.join(path)} {name}" for path, name in HIDDEN]
)
def test_no_command_suggests_one_of_its_hidden_options(path, name):
    """For every hidden option of every command: mistyping it suggests no hidden option."""
    command = cli
    for step in path:
        command = command.commands[step]  # type: ignore[attr-defined]
    parent = click.Context(cli)
    with pytest.raises(click.NoSuchOption) as raised:
        command.make_context(path[-1], [_mistyped(name)], parent=parent)

    hidden = set(_hidden_names(command))
    assert not hidden & set(raised.value.possibilities or ()), raised.value.format_message()


@pytest.mark.parametrize("command", ["run", "dse", "scrub"])
def test_the_message_of_a_mistyped_removed_option_names_no_hidden_option(command):
    result = CliRunner().invoke(cli, [command, "--xeda-run"])
    text = click.unstyle(result.output)

    assert result.exit_code == 2, text
    assert "No such option '--xeda-run'" in text
    assert "--xeda-run-dir" not in text


def test_a_mistyped_option_still_suggests_the_options_the_command_has():
    """The sweep must not have been satisfied by suggesting nothing."""
    result = CliRunner().invoke(cli, ["run", "--run-dirs"])
    text = click.unstyle(result.output)

    assert "Did you mean one of: '--hashed-run-dirs', '--run-root'?" in text
    assert "--xeda-run-dir" not in text


def test_the_error_document_of_a_mistyped_option_suggests_no_hidden_option():
    result = CliRunner().invoke(cli, ["run", "--run-roo", "--json"])
    document = json.loads(result.stdout)

    assert result.exit_code == 2
    assert document["success"] is False
    assert "'--run-root'" in document["error"]["message"]
    assert "--xeda-run-dir" not in document["error"]["message"]


def test_every_command_xeda_defines_takes_the_rule():
    """A command added later is covered before it has a hidden option of its own. The `help`
    commands are click-extra's, and have no options."""
    left_out = [
        " ".join(path) or "xeda"
        for path, command in _commands(cli)
        if not isinstance(command, (XedaCommand, XedaHelpGroup)) and command.name != "help"
    ]

    assert not left_out


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["run", "--zzzzzzzz"], "No such option '--zzzzzzzz'."),
        (["run", "-Q"], "No such option '-Q'."),
    ],
)
def test_a_mistyped_option_with_nothing_close_is_reported_as_before(arguments, message):
    result = CliRunner().invoke(cli, arguments)
    text = click.unstyle(result.output)

    assert result.exit_code == 2, text
    assert message in text
    assert "Did you mean" not in text
