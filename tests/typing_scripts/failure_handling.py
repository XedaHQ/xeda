"""Handle a failed launch, a stale plan and a refused request."""

from typing import assert_type

import xeda
from xeda.errors import RecordError, RunFailedError, SpecError, StalePlanError, XedaError

design = xeda.Design.load("design.yaml")
spec = xeda.Spec("ghdl", design)
ws = xeda.Workspace(confirm=lambda question: question.removal)


def on_event(event: xeda.Event) -> None:
    assert_type(event.kind, str)
    assert_type(event.node, str | None)
    assert_type(event.outcome, xeda.Outcome | None)


token = xeda.CancelToken()
try:
    result = ws.run(spec, cancel=token, on_event=on_event)
    result.require_passed()
    assert_type(result.record, xeda.RunRecord)
except RunFailedError as error:
    assert_type(error.result, xeda.Result)
    assert_type(error.code, str)
    assert_type(error.message, str)
except StalePlanError as error:
    assert_type(error.code, str)
except SpecError as error:
    assert_type(error.code, str)
except RecordError as error:
    assert_type(error.code, str)
except XedaError as error:
    print(error.code, error.message)

scrubbed = ws.scrub(design_name=design.name)
assert_type(scrubbed, xeda.ScrubResult)
assert_type(xeda.scrub(design_name=design.name, flow="ghdl"), xeda.ScrubResult)
