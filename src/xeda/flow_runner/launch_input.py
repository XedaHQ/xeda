"""The first stage of a launch: what the request alone decides.

The input settings of a flow, validated in context, and the refusals that need only the plan.

Free functions with explicit arguments. This module never imports `default_runner`: the
launcher passes what each function needs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..dataclass import DELIVERABLE_ROLE, written_role
from ..deliver import DeliveryError
from ..design import Design
from ..flow import Flow, FlowSettingsError
from ..flow.io import declared_outputs
from ..utils import LOCATION_FORMS
from .resolver import Plan, PlanNode, check_launchable
from .settings_layers import settings_in_context, suggest_dependency_node

log = logging.getLogger(__name__)


def input_settings(
    flow_class: type[Flow],
    flow_settings: dict[str, Any] | Flow.Settings | None,
    design: Design,
    runner_cwd: Path,
    depender: Flow | None = None,
    *,
    debug: bool = False,
) -> Flow.Settings:
    """The run's input settings, validated in context and owned by the launcher.

    Never modified afterwards: the flow works on a copy of its own (`flow.settings`), which
    its `__init__`/`init()`/`run()` may complete with derived values, resolved paths and
    outputs without any of that leaking into the run's identity.
    """
    try:
        settings = settings_in_context(flow_class, flow_settings, design.root_path, runner_cwd)
    except FlowSettingsError as error:
        if depender is None:
            suggest_dependency_node(flow_class, error)
        raise
    if debug:
        log.debug(
            "Flow '%s' settings: %s",
            flow_class.name,
            settings.model_dump_json(exclude_unset=True, indent=2),
        )
        settings.debug = True  # the launcher's `--debug` is part of the input
    # a path the flow writes that leads out of its run directory, a setting the
    # flow cannot run without, and a design it cannot run are reported now, before anything
    # is set up for the run
    check_launchable(flow_class, settings, design)
    return settings


def setting_naming(flow_class: type[Flow], output: str) -> str | None:
    """The deliverable setting of `flow_class` that names its output `output`: the one that
    switches it on (`enabled_by`), else a setting of the output's own name."""
    declaration = declared_outputs(flow_class).get(output)
    for name in (declaration.enabled_by if declaration else None, output):
        if name and written_role(flow_class.Settings, name) == DELIVERABLE_ROLE:
            return name
    return None


def refuse_outputs_to_a_programmer(
    plan: Plan, node: PlanNode, outputs_to: Path, *, remote: bool = False
) -> None:
    """`--outputs-to` copies what the requested flow writes. A flow that programs a device
    (`Flow.action_reason`) writes no outputs, so there is nothing to deliver: a `DeliveryError`
    before anything runs, a dry run alike, naming what would deliver the file it reads -- the
    setting of the flow that writes it in this plan, given a location (`LOCATION_FORMS`); on a
    `remote` run, where that setting would deliver on the
    remote host, the request of that flow, whose own outputs come back -- or the design source it
    is."""
    flow_class = node.flow_class
    if flow_class.action_reason is None:
        return
    producers, settings, sources = [], [], []
    for selected in node.inputs:
        sources += [str(path) for path in selected.sources]
        for reference in selected.references:
            producer = plan.node(reference.node)
            producers.append(producer.name)
            setting = setting_naming(producer.flow_class, reference.output)
            if setting is not None:
                settings.append(f"-s flows.{producer.name}.{setting}=$PWD/<file>")
    message = (
        f"--outputs-to {outputs_to}: {flow_class.name} writes no outputs "
        f"({flow_class.action_reason}), so there is nothing to deliver"
    )
    if remote and producers:
        message += ". To receive the file it reads, request the flow that writes it instead: " + (
            ", ".join(
                f"xeda run --remote {name} ... --outputs-to {outputs_to}" for name in producers
            )
        )
    elif settings:
        message += (
            ". To receive the file it reads, give the setting that writes it a location "
            f"({LOCATION_FORMS}) instead: " + ", ".join(settings)
        )
    if sources:
        message += ". The file it reads is a design source: " + ", ".join(sources)
    raise DeliveryError(message, before_run=True)
