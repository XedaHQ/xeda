"""The command line's detailed logs shorten a logger's name, and leave the record alone.

`[xeda.flow_runner.default_runner]` is long, so the detailed format shows
`[flow_runner.default_runner]`. A log record is one object that every handler of the process sees,
though: the shortening is part of how one handler *shows* a record, never a change to it, or the
next handler (pytest's `caplog`, a handler an API user installed) reads a name that is no
logger's.
"""

import logging
from collections.abc import Iterator

import pytest

from xeda.cli import setup_logger

pytestmark = pytest.mark.python_compat


class _Collect(logging.Handler):
    """A handler of another party: it keeps the records it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def root_logger_as_it_was() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _log_through_the_cli(detailed: bool, before: _Collect, after: _Collect) -> None:
    """`before` is a handler that was on the root logger when the CLI set its logging up, `after`
    one added later; the CLI's own handler writes to stderr."""
    root = logging.getLogger()
    root.addHandler(before)
    setup_logger(logging.INFO, detailed)
    root.addHandler(after)
    logging.getLogger("xeda.flow_runner.default_runner").info("hello")


@pytest.mark.parametrize("detailed", [True, False])
def test_every_other_handler_sees_the_logger_s_own_name(capsys, detailed):
    before, after = _Collect(), _Collect()
    _log_through_the_cli(detailed, before, after)

    for handler in (before, after):
        assert [record.name for record in handler.records] == ["xeda.flow_runner.default_runner"]


def test_the_detailed_logs_show_the_name_without_the_package(capsys):
    _log_through_the_cli(True, _Collect(), _Collect())

    shown = capsys.readouterr().err
    assert "[flow_runner.default_runner]" in shown
    assert "xeda.flow_runner" not in shown
    assert "INFO hello" in shown


def test_the_plain_logs_show_no_name(capsys):
    _log_through_the_cli(False, _Collect(), _Collect())

    shown = capsys.readouterr().err
    assert shown.strip() == "INFO hello"


def test_a_logger_outside_the_package_keeps_its_name(capsys):
    setup_logger(logging.INFO, True)
    logging.getLogger("pebble.pool").info("hello")

    assert "[pebble.pool]" in capsys.readouterr().err


def test_setting_the_logs_up_again_does_not_shorten_twice(capsys):
    setup_logger(logging.INFO, True)
    setup_logger(logging.INFO, True)
    logging.getLogger("xeda.xeda.inner").info("hello")

    assert capsys.readouterr().err.count("[xeda.inner]") == 1
