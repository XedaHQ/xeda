"""Xeda's public package API."""

import logging
import sys
import types
from importlib import import_module
from typing import TYPE_CHECKING, Any

from .log_context import TaggingNullHandler
from .version import __version__

# The library writes no log output of its own. The application decides where records go. This
# handler writes nothing: it tags the records of a launch (`log_context.py`).
logging.getLogger(__name__).addHandler(TaggingNullHandler())

# Each public name loads its module on first use, so `import xeda` stays fast.
_LAZY_EXPORTS = {
    "CancelToken": (".cancel", "CancelToken"),
    "DeliveryRecord": (".outcomes", "DeliveryRecord"),
    "Design": (".design", "Design"),
    "DesignPart": (".design", "DesignPart"),
    "Disposition": (".outcomes", "Disposition"),
    "ErrorInfo": (".records", "ErrorInfo"),
    "Event": (".events", "Event"),
    "InputRecord": (".records", "InputRecord"),
    "InputSource": (".records", "InputSource"),
    "LocalSettings": (".local_settings", "LocalSettings"),
    "MetricInfo": (".records", "MetricInfo"),
    "NodeResult": (".outcomes", "NodeResult"),
    "OpenRun": (".records", "OpenRun"),
    "Outcome": (".outcomes", "Outcome"),
    "OutputRecord": (".records", "OutputRecord"),
    "Plan": (".plan", "Plan"),
    "PlanDelivery": (".plan", "PlanDelivery"),
    "PlanInput": (".plan", "PlanInput"),
    "PlanNode": (".plan", "PlanNode"),
    "Project": (".project", "Project"),
    "Question": (".types", "Question"),
    "RecordProblem": (".records", "RecordProblem"),
    "Remote": (".remote_value", "Remote"),
    "Result": (".outcomes", "Result"),
    "Rtl": (".design", "Rtl"),
    "RunListing": (".records", "RunListing"),
    "RunRecord": (".records", "RunRecord"),
    "ScrubResult": (".flow_runner.scrub", "ScrubResult"),
    "Source": (".design", "Source"),
    "Spec": (".spec", "Spec"),
    "SweepPoint": (".sweep", "SweepPoint"),
    "SweepResult": (".sweep", "SweepResult"),
    "Testbench": (".design", "Testbench"),
    "UNSET": (".types", "UNSET"),
    "Unset": (".types", "Unset"),
    "Workspace": (".workspace", "Workspace"),
    "load": (".workspace_verbs", "load"),
    "plan": (".workspace_verbs", "plan"),
    "records": (".workspace_verbs", "records"),
    "run": (".workspace_verbs", "run"),
    "scrub": (".workspace_verbs", "scrub"),
    "sweep": (".workspace_verbs", "sweep"),
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
    "load",
    "plan",
    "records",
    "run",
    "scrub",
    "sweep",
]

if TYPE_CHECKING:
    from .cancel import (
        CancelToken as CancelToken,
    )
    from .design import (
        Design as Design,
        DesignPart as DesignPart,
        Rtl as Rtl,
        Source as Source,
        Testbench as Testbench,
    )
    from .events import (
        Event as Event,
    )
    from .flow_runner.scrub import (
        ScrubResult as ScrubResult,
    )
    from .local_settings import (
        LocalSettings as LocalSettings,
    )
    from .outcomes import (
        DeliveryRecord as DeliveryRecord,
        Disposition as Disposition,
        NodeResult as NodeResult,
        Outcome as Outcome,
        Result as Result,
    )
    from .plan import (
        Plan as Plan,
        PlanDelivery as PlanDelivery,
        PlanInput as PlanInput,
        PlanNode as PlanNode,
    )
    from .project import (
        Project as Project,
    )
    from .records import (
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
    from .remote_value import (
        Remote as Remote,
    )
    from .spec import (
        Spec as Spec,
    )
    from .sweep import (
        SweepPoint as SweepPoint,
        SweepResult as SweepResult,
    )
    from .types import (
        Question as Question,
        UNSET as UNSET,
        Unset as Unset,
    )
    from .workspace import (
        Workspace as Workspace,
    )
    from .workspace_verbs import (
        load as load,
        plan as plan,
        records as records,
        run as run,
        scrub as scrub,
        sweep as sweep,
    )


class _Package(types.ModuleType):
    """The module `xeda`, which keeps three verbs that are also names of submodules.

    Importing the submodule `xeda.plan` (or `xeda.records`, `xeda.sweep`) binds it as the
    attribute `plan` of this package. That binding must not replace the function `xeda.plan`.
    `from xeda.plan import Plan` is unaffected, because it reads `sys.modules`."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _LAZY_EXPORTS and isinstance(value, types.ModuleType):
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Package


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
