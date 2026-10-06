"""A setting that names a file, or a directory a tool reads, is typed as a path, so the trace
sees it (a directory by every file under it) and `$DESIGN_ROOT` expands in it.

A trace records the files a run's settings name by walking their *declared* path types
(`trace_inputs.setting_files`); a file named by a `str` setting is invisible to it -- editing it
would leave a stale result looking fresh. The sweep flags every `str`-typed setting of every
registered flow (nested models included, dependencies' settings left to their own flow) whose
dotted name says it names a file, unless it is allowlisted with the reason it is not an input.
"""

import re
from typing import Annotated, Any, Iterator, List, Tuple, get_args, get_origin

from pydantic import BaseModel

from xeda.dataclass import written_role
from xeda.flow import Flow, registered_flows
from xeda.flow.flow import _annotation_contains_path

#: names that say a setting names a file (matched against the dotted name, nested models too)
FILE_NAMES = re.compile(r"(_file|_files|_script|_dirs?|_folder|ini|_libs?)$|path|sdf|wave_opt")

#: `str` settings whose names match but that are not files a run reads, and why
NOT_INPUTS = {
    "yosys.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "yosys_fpga.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "yosys_sim.abc_script": "inline scripts are text; file scripts are registered as implicit inputs",
    "dc.sdf_inst_name": "an instance name in the design hierarchy",
    "dc.sdf_version": "the SDF format version to write",
    "vcs.sdf_instance": "an instance name in the design hierarchy",
    "ghdl_sim.sdf.root": "an instance path in the design hierarchy",
    "modelsim.sdf.root": "an instance path in the design hierarchy",
    "vivado_sim.sdf.root": "an instance path in the design hierarchy",
    "vivado_postsynth_sim.sdf.root": "an instance path in the design hierarchy",
    "vivado_project.sdf.root": "an instance path in the design hierarchy",
    "vivado_sim.work_lib": "a library name",
    "vivado_postsynth_sim.work_lib": "a library name",
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


#: Path-typed settings that name a directory a run reads (an input: every file under it is
#: recorded), by flow; every other directory setting must be one the flow writes (a role:
#: `xeda.dataclass.WORKING`, `deliverable(...)`), which is never an input.
INPUT_DIRECTORIES = {
    "lib_paths": "compiled libraries a tool reads",
    "include_dirs": "include directories Verilator searches",
    "additional_search_path": "a directory DC searches for libraries",
    "prjxray_db": "the Project X-Ray database nextpnr and fpga_pack read for 7-series",
    "search_paths": "directories bsc searches for imported Bluespec packages",
    "verilog_search_paths": "directories bsc searches for the Verilog of imported modules",
    "library_dirs": "directories of the C/C++ libraries bsc_sim links",
}

DIRECTORY_NAMES = re.compile(r"(_dir|_dirs|_folder|_paths)$")


def test_every_directory_setting_is_an_input_or_a_declared_output():
    """A directory a setting names is either read by the run -- listed as its input -- or
    written by it (a role: `xeda.dataclass.WORKING`, `deliverable(...)`): undeclared, a flow's
    own output directory would be taken for an input it rewrites every run."""
    unclassified = []
    for _, (_, cls) in sorted(registered_flows.items()):
        declared = {name for name in cls.Settings.model_fields if written_role(cls.Settings, name)}
        for name, field in cls.Settings.model_fields.items():
            if not DIRECTORY_NAMES.search(name) or not _annotation_contains_path(field.annotation):
                continue
            if (name in INPUT_DIRECTORIES) == (name in declared):
                unclassified.append(f"{cls.name}.{name}")
    assert sorted(set(unclassified)) == []
