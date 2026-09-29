"""A setting that names a file is typed as a path, so the trace sees it and `$DESIGN_ROOT`
expands in it.

A trace records the files a run's settings name by walking their *declared* path types
(`trace_inputs.setting_files`); a file named by a `str` setting is invisible to it -- editing it
would leave a stale result looking fresh. The sweep flags every `str`-typed setting of every
registered flow (nested models included, dependencies' settings left to their own flow) whose
dotted name says it names a file, unless it is allowlisted with the reason it is not an input.
"""

import re
from typing import Annotated, Any, Iterator, List, Tuple, get_args, get_origin

from pydantic import BaseModel

from xeda.flow import Flow, registered_flows
from xeda.flow.flow import _annotation_contains_path

#: names that say a setting names a file (matched against the dotted name, nested models too)
FILE_NAMES = re.compile(r"(_file|_files|_script|_dir|ini|_libs?)$|path|sdf|wave_opt")

#: `str` settings whose names match but that are not files a run reads, and why
NOT_INPUTS = {
    "yosys.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "yosys_fpga.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "yosys_sim.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "bsc.bobj_dir": "the directory bsc writes its object files into",
    "verilator.sim_dir": "the build directory Verilator writes the model into",
    "vcs.work_dir": "the directory VCS compiles into",
    "vcs.vcs_log_file": "the log VCS writes",
    "yosys.log_file": "the log yosys writes",
    "yosys_fpga.log_file": "the log yosys writes",
    "yosys_sim.log_file": "the log yosys writes",
    "dc.sdf_inst_name": "an instance name in the design hierarchy",
    "dc.sdf_version": "the SDF format version to write",
    "vcs.sdf_instance": "an instance name in the design hierarchy",
    "ghdl_sim.sdf.root": "an instance path in the design hierarchy",
    "modelsim.sdf.root": "an instance path in the design hierarchy",
    "vivado_sim.sdf.root": "an instance path in the design hierarchy",
    "vivado_postsynth_sim.sdf.root": "an instance path in the design hierarchy",
    "vivado_power.sdf.root": "an instance path in the design hierarchy",
    "vivado_project.sdf.root": "an instance path in the design hierarchy",
    "vivado_sim.work_lib": "a library name",
    "vivado_postsynth_sim.work_lib": "a library name",
    "vivado_power.work_lib": "a library name",
    "vivado_project.work_lib": "a library name",
}


def _leaves(annotation: Any) -> Iterator[Any]:
    if get_origin(annotation) is Annotated:
        yield from _leaves(get_args(annotation)[0])
        return
    args = get_args(annotation)
    if not args:
        yield annotation
        return
    for arg in args:
        yield from _leaves(arg)


def _str_file_settings(model: type[BaseModel], prefix: str, seen: frozenset) -> Iterator[str]:
    if model in seen:
        return
    seen = seen | {model}
    dependencies = model.dependency_settings if issubclass(model, Flow.Settings) else {}
    for name, field in model.model_fields.items():
        if name in dependencies:
            continue  # a dependency's own flow is swept by itself
        dotted = f"{prefix}{name}"
        leaves = list(_leaves(field.annotation))
        if (
            FILE_NAMES.search(dotted)
            and str in leaves
            and not _annotation_contains_path(field.annotation)
        ):
            yield dotted
        for leaf in leaves:
            if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                yield from _str_file_settings(leaf, f"{dotted}.", seen)


def _flows() -> List[Tuple[str, type]]:
    return sorted({(cls.name, cls) for _module, cls in registered_flows.values()})


def test_every_setting_that_names_a_file_is_a_path():
    flagged = sorted(
        name
        for flow, cls in _flows()
        for name in _str_file_settings(cls.Settings, f"{flow}.", frozenset())
        if name not in NOT_INPUTS
    )
    assert not flagged, (
        "type these as paths (a trace cannot see a file named by a `str`), or add them to "
        f"NOT_INPUTS with the reason they are not inputs: {flagged}"
    )


def test_the_allowlist_names_only_settings_that_exist():
    existing = {
        name
        for flow, cls in _flows()
        for name in _str_file_settings(cls.Settings, f"{flow}.", frozenset())
    }
    assert set(NOT_INPUTS) <= existing, sorted(set(NOT_INPUTS) - existing)


def test_an_inline_abc_script_is_text_and_a_script_file_a_path(tmp_path):
    from xeda.flows.yosys import YosysFpga

    inline = YosysFpga.Settings.from_input({"abc_script": "+echo $PWD"}, design_root=tmp_path)
    assert inline.abc_script == "+echo,$PWD"
    reloaded = YosysFpga.Settings.from_input(inline.model_dump(mode="json"), design_root=tmp_path)
    assert reloaded.abc_script == inline.abc_script
    script = YosysFpga.Settings.from_input(
        {"abc_script": "$DESIGN_ROOT/map.abc"}, design_root=tmp_path
    )
    assert script.abc_script == "$DESIGN_ROOT/map.abc"
