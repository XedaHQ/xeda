"""Force the format of the script of `yosys` in a test.

Xeda selects the format from the settings (`YosysBase.script_format`): a Tcl script only when a
setting needs one (`Yosys.tcl_reasons`), and a script of yosys otherwise. A test that checks both
template sets of `yosys` forces one format with `forced_script_format`, around the planning and
the rendering. It changes no setting, so the commands that the two scripts run are compared for
the same settings. Only `yosys` has Tcl templates: the other yosys flows always write a script of
yosys, and the forcing leaves them alone.
"""

import contextlib
from typing import Iterator, Literal, Optional

import pytest

from xeda.flows import Yosys

ScriptFormat = Literal["ys", "tcl"]


@contextlib.contextmanager
def forced_script_format(script_format: Optional[ScriptFormat]) -> Iterator[None]:
    """Make every yosys flow write a script of `script_format` in the block. `None` leaves the
    selection to the settings."""
    if script_format is None:
        yield
        return
    assert script_format in ("ys", "tcl"), script_format
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Yosys, "script_format", classmethod(lambda _cls, _settings: script_format))
        yield


def force_script_format(monkeypatch: pytest.MonkeyPatch, script_format: ScriptFormat) -> None:
    """Make every yosys flow write a script of `script_format` for the rest of the test."""
    assert script_format in ("ys", "tcl"), script_format
    monkeypatch.setattr(Yosys, "script_format", classmethod(lambda _cls, _settings: script_format))


def split_script_format(settings: Optional[dict]) -> tuple[Optional[dict], Optional[ScriptFormat]]:
    """Return a copy of the flow settings `settings` without the key `script_format`, which a
    test gives to choose the template set, and the format that it names (or `None`)."""
    if settings is None or "script_format" not in settings:
        return settings, None
    rest = dict(settings)
    return rest, rest.pop("script_format")
