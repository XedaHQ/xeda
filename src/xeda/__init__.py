"""Xeda's public package API."""

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._internal.log_context import TaggingNullHandler
from ._internal.version import __version__

# The library writes no log output of its own. The application decides where records go. This
# handler writes nothing: it tags the records of a launch (`log_context.py`).
logging.getLogger(__name__).addHandler(TaggingNullHandler())

# Each public name loads its module on first use, so `import xeda` stays fast.
_LAZY_EXPORTS = {
    "CancelToken": ("._internal.cancel", "CancelToken"),
    "DeliveryRecord": ("._internal.outcomes", "DeliveryRecord"),
    "Design": ("._internal.design", "Design"),
    "DesignPart": ("._internal.design", "DesignPart"),
    "Disposition": ("._internal.outcomes", "Disposition"),
    "ErrorInfo": ("._internal.records", "ErrorInfo"),
    "Event": ("._internal.events", "Event"),
    "InputRecord": ("._internal.records", "InputRecord"),
    "InputSource": ("._internal.records", "InputSource"),
    "LocalSettings": ("._internal.local_settings", "LocalSettings"),
    "MetricInfo": ("._internal.records", "MetricInfo"),
    "NodeResult": ("._internal.outcomes", "NodeResult"),
    "OpenRun": ("._internal.records", "OpenRun"),
    "Outcome": ("._internal.outcomes", "Outcome"),
    "OutputRecord": ("._internal.records", "OutputRecord"),
    "Plan": ("._internal.plan", "Plan"),
    "PlanDelivery": ("._internal.plan", "PlanDelivery"),
    "PlanInput": ("._internal.plan", "PlanInput"),
    "PlanNode": ("._internal.plan", "PlanNode"),
    "Project": ("._internal.project", "Project"),
    "Question": ("._internal.types", "Question"),
    "RecordProblem": ("._internal.records", "RecordProblem"),
    "Remote": ("._internal.remote_value", "Remote"),
    "Result": ("._internal.outcomes", "Result"),
    "Rtl": ("._internal.design", "Rtl"),
    "RunListing": ("._internal.records", "RunListing"),
    "RunRecord": ("._internal.records", "RunRecord"),
    "ScrubResult": ("._internal.flow_runner.scrub", "ScrubResult"),
    "Source": ("._internal.design", "Source"),
    "Spec": ("._internal.spec", "Spec"),
    "SweepPoint": ("._internal.sweep", "SweepPoint"),
    "SweepResult": ("._internal.sweep", "SweepResult"),
    "Testbench": ("._internal.design", "Testbench"),
    "UNSET": ("._internal.types", "UNSET"),
    "Unset": ("._internal.types", "Unset"),
    "Workspace": ("._internal.workspace", "Workspace"),
    "load_record": ("._internal.workspace_verbs", "load_record"),
    "local_settings": ("._internal.local_settings", "local_settings"),
    "plan": ("._internal.workspace_verbs", "plan"),
    "records": ("._internal.workspace_verbs", "records"),
    "run": ("._internal.workspace_verbs", "run"),
    "scrub": ("._internal.workspace_verbs", "scrub"),
    "sweep": ("._internal.workspace_verbs", "sweep"),
}

__all__ = [
    "CancelToken",
    "DeliveryRecord",
    "Design",
    "DesignPart",
    "Disposition",
    "ErrorInfo",
    "Event",
    "InputRecord",
    "InputSource",
    "LocalSettings",
    "MetricInfo",
    "NodeResult",
    "OpenRun",
    "Outcome",
    "OutputRecord",
    "Plan",
    "PlanDelivery",
    "PlanInput",
    "PlanNode",
    "Project",
    "Question",
    "RecordProblem",
    "Remote",
    "Result",
    "Rtl",
    "RunListing",
    "RunRecord",
    "ScrubResult",
    "Source",
    "Spec",
    "SweepPoint",
    "SweepResult",
    "Testbench",
    "UNSET",
    "Unset",
    "Workspace",
    "__version__",
    "load_record",
    "local_settings",
    "plan",
    "records",
    "run",
    "scrub",
    "sweep",
]

if TYPE_CHECKING:
    from ._internal.cancel import (
        CancelToken as CancelToken,
    )
    from ._internal.design import (
        Design as Design,
        DesignPart as DesignPart,
        Rtl as Rtl,
        Source as Source,
        Testbench as Testbench,
    )
    from ._internal.events import (
        Event as Event,
    )
    from ._internal.flow_runner.scrub import (
        ScrubResult as ScrubResult,
    )
    from ._internal.local_settings import (
        LocalSettings as LocalSettings,
        local_settings as local_settings,
    )
    from ._internal.outcomes import (
        DeliveryRecord as DeliveryRecord,
        Disposition as Disposition,
        NodeResult as NodeResult,
        Outcome as Outcome,
        Result as Result,
    )
    from ._internal.plan import (
        Plan as Plan,
        PlanDelivery as PlanDelivery,
        PlanInput as PlanInput,
        PlanNode as PlanNode,
    )
    from ._internal.project import (
        Project as Project,
    )
    from ._internal.records import (
        ErrorInfo as ErrorInfo,
        InputRecord as InputRecord,
        InputSource as InputSource,
        MetricInfo as MetricInfo,
        OpenRun as OpenRun,
        OutputRecord as OutputRecord,
        RecordProblem as RecordProblem,
        RunListing as RunListing,
        RunRecord as RunRecord,
    )
    from ._internal.remote_value import (
        Remote as Remote,
    )
    from ._internal.spec import (
        Spec as Spec,
    )
    from ._internal.sweep import (
        SweepPoint as SweepPoint,
        SweepResult as SweepResult,
    )
    from ._internal.types import (
        Question as Question,
        UNSET as UNSET,
        Unset as Unset,
    )
    from ._internal.workspace import (
        Workspace as Workspace,
    )
    from ._internal.workspace_verbs import (
        load_record as load_record,
        plan as plan,
        records as records,
        run as run,
        scrub as scrub,
        sweep as sweep,
    )


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
