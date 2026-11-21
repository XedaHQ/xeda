"""The staging of the `bsc` and `bsc_sim` flows, which needs no tool.

A flow links the listed sources, the search folders and the Verilog files into its run directory
and hands bsc the flags that name them. These tests build that stage and read it back, or read
the flags, without running bsc. They are in the file list of the `macos` job in
`.github/workflows/ci.yml`, because they create and read symbolic links. The tests that run bsc
are in `test_bsc.py`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda._internal.design import SourceType
from xeda._internal.flow import Flow, FlowSettingsException
from xeda._internal.flows import Bsc, BscSim
from xeda._internal.flows import bsc as bsc_module
from xeda._internal.run_dir import RunDirectory


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _accum_design(root: Path, **extra: Any) -> Design:
    """Three packages in three folders: a library, the top and a testbench."""
    _write(
        root,
        {
            "lib/Accum.bsv": "package Accum; endpackage\n",
            "rtl/Top.bsv": "package Top; endpackage\n",
            "tb/Tb.bsv": "package Tb; endpackage\n",
        },
    )
    fields: dict[str, Any] = {
        "rtl": {"sources": ["lib/Accum.bsv", "rtl/Top.bsv"], "top": "mkTop"},
        "tb": {"sources": ["tb/Tb.bsv"], "top": "mkTb"},
    }
    fields.update(extra)
    return Design(name="accum", root=root, **fields)


def _flow(flow_class: type[Flow], design: Design, run_path: Path, **settings: Any) -> Any:
    """A flow in `run_path`, a run directory xeda manages (under the run root its parent)."""
    run_path.mkdir(parents=True, exist_ok=True)
    run_directory = RunDirectory.claimed(run_path, run_path.parent)
    return flow_class(settings, design, run_path, run_directory=run_directory)


def _pairs(flags: list[str], flag: str) -> list[str]:
    """The arguments `flag` is given with in `flags`."""
    return [flags[i + 1] for i, f in enumerate(flags) if f == flag and i + 1 < len(flags)]


def test_a_verilog_search_path_with_a_colon_is_rejected(tmp_path):
    design = _accum_design(tmp_path / "d")
    (tmp_path / "d" / "a:b").mkdir()
    flow = _flow(Bsc, design, tmp_path / "run", verilog_search_paths=["a:b"])
    with pytest.raises(FlowSettingsException, match="':'"):
        flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)


def test_a_missing_verilog_search_folder_fails_the_run(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run", verilog_search_paths=["nowhere"])
    with pytest.raises(FlowSettingsException, match="`verilog_search_paths` names 'nowhere'"):
        flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)


def test_a_search_path_with_a_colon_is_staged_under_a_name_without_one(tmp_path):
    """bsc gets `bsc_search/0`, never the folder, so a colon in the folder name is no problem."""
    root = tmp_path / "d"
    design = _accum_design(root)
    _write(root, {"a:b/Extra.bsv": "package Extra; endpackage\n"})
    flow = _flow(Bsc, design, tmp_path / "run", search_paths=["a:b"])
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert flags[flags.index("-p") + 1] == "bsc_sources:bsc_search/0:%/Libraries"
    flow._stage(tb=False)
    assert (
        flow.run_path / "bsc_search" / "0" / "Extra.bsv"
    ).resolve() == root / "a:b" / "Extra.bsv"


@pytest.mark.parametrize("simulator", ["bluesim", "verilator", "iverilog"])
def test_the_path_flags_of_each_simulator(simulator, tmp_path):
    """`-fdir` reaches every compile; `-vsearch` only a Verilog one (Bluesim reads no Verilog);
    the output directory is `-simdir` for Bluesim and `-vdir` otherwise."""
    design = _accum_design(tmp_path / "d")
    (tmp_path / "d" / "files").mkdir()
    flow = _flow(BscSim, design, tmp_path / "run", simulator=simulator, fdir="files")
    backend = "sim" if simulator == "bluesim" else "verilog"
    flags = flow._path_flags(backend, tmp_path / "out", design.rtl.sources)
    assert flags[flags.index("-fdir") + 1] == str(tmp_path / "d" / "files")
    assert ("-vsearch" in flags) == (simulator != "bluesim")
    assert flags[flags.index("-simdir" if backend == "sim" else "-vdir") + 1] == str(
        tmp_path / "out"
    )


BH_INCLUDE_PKG = """package Knob where

#include "init.h"

interface Knob_IFC =
    value :: Bit 4

{-# verilog mkKnob #-}
mkKnob :: Module Knob_IFC
mkKnob = module
    r :: Reg (Bit 4) <- mkReg INIT
    rules
      "tick": when True ==> r := r + 1
    interface
      value = r
"""


def _bh_include_design(root: Path, init: int, *, listed: bool = True) -> Design:
    _write(root, {"Knob.bs": BH_INCLUDE_PKG, "init.h": f"#define INIT {init}\n"})
    sources = ["Knob.bs", "init.h"] if listed else ["Knob.bs"]
    return Design(name="knob", root=root, rtl={"sources": sources, "top": "mkKnob"})


def test_a_c_header_is_staged_only_with_cpp(tmp_path):
    design = _bh_include_design(tmp_path / "d", 5)
    assert design.rtl.sources[1].type is SourceType.CHeader
    for flow_class in (Bsc, BscSim):
        assert SourceType.CHeader in flow_class.reads_sources
    off = _flow(Bsc, design, tmp_path / "off")._stage_entries(tb=False)
    on = _flow(Bsc, design, tmp_path / "on", cpp=True)._stage_entries(tb=False)
    assert [str(link) for link in off] == ["bsc_sources/Knob.bs"]
    assert [str(link) for link in on] == ["bsc_sources/Knob.bs", "bsc_sources/init.h"]


def _search_flow(tmp_path: Path, files: dict[str, str], search: list[str], **fields: Any) -> Any:
    root = tmp_path / "d"
    _write(root, files)
    design = Design(name="s", root=root, **fields)
    return _flow(Bsc, design, tmp_path / "run", search_paths=search)


def _search_names(flow: Any, index: int = 0) -> list[str]:
    folder = flow.run_path / "bsc_search" / str(index)
    return sorted(p.name for p in folder.iterdir())


def _stage_tree(folder: Path) -> list[str]:
    """Each entry of a stage folder: a folder as `name/`, a link as `name -> target`."""
    found = []
    for path in sorted(folder.rglob("*")):
        name = str(path.relative_to(folder))
        if path.is_symlink():
            assert os.path.isabs(os.readlink(path))
            found.append(f"{name} -> {path.resolve().relative_to(folder.parent.parent.parent)}")
        else:
            assert path.is_dir(), path
            found.append(name + "/")
    return found


def test_a_search_folder_is_staged_as_file_links_less_what_bsc_must_not_read(tmp_path, caplog):
    stub = "package X; endpackage\n"
    flow = _search_flow(
        tmp_path,
        {
            "Top.bsv": "package Top; endpackage\n",
            "lib/Pure.bsv": stub,
            "lib/Pure.bo": "",  # a compiled file beside its source
            "lib/Old.bs": stub,
            "lib/Old.bo": "",
            "lib/Lone.bo": "",  # no source beside it: stays
            "lib/Top.bsv": stub,  # defines a listed package
            "lib/Top.bo": "",
            "lib/sub/x.bsvi": "\n",
            "lib/sub/Pure.bo": "",  # the same rules hold at every folder level
            "lib/sub/Pure.bsv": stub,
            "lib/sub/Top.bs": stub,
            "lib/sub/deep/y.bsvi": "\n",
            "lib/sub/.git/x": "",  # version control metadata, at any depth
            "lib/.git/config": "",
            "lib/notes.txt": "",
        },
        ["lib"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    with caplog.at_level(logging.INFO):
        flow._stage(tb=False)
    stage = flow.run_path / "bsc_search" / "0"
    assert _stage_tree(stage) == [
        "Lone.bo -> d/lib/Lone.bo",
        "Old.bs -> d/lib/Old.bs",
        "Pure.bsv -> d/lib/Pure.bsv",
        "notes.txt -> d/lib/notes.txt",
        "sub/",
        "sub/Pure.bsv -> d/lib/sub/Pure.bsv",
        "sub/deep/",
        "sub/deep/y.bsvi -> d/lib/sub/deep/y.bsvi",
        "sub/x.bsvi -> d/lib/sub/x.bsvi",
    ]
    assert not (stage / "sub").is_symlink()
    (message,) = [r.getMessage() for r in caplog.records if "does not see" in r.getMessage()]
    for name in ("Pure.bo", "Old.bo", "Top.bsv", "Top.bo", "sub/Pure.bo", "sub/Top.bs"):
        assert name in message
    assert "Lone.bo" not in message


def test_an_include_below_version_control_metadata_of_a_search_folder_is_not_found(tmp_path):
    """The walker of the trace leaves `.git` out at every depth, so the stage holds no file of
    it, and an include of `sub/.git/x.bsvi` fails in bsc as for any missing file."""
    flow = _search_flow(
        tmp_path,
        {"Top.bsv": "package Top; endpackage\n", "lib/sub/.git/x.bsvi": "\n", "lib/sub/ok": ""},
        ["lib"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    flow._stage(tb=False)
    stage = flow.run_path / "bsc_search" / "0"
    assert not (stage / "sub" / ".git").exists()
    assert (stage / "sub" / "ok").is_symlink()


def test_a_link_to_a_folder_in_a_search_folder_is_staged_as_a_real_folder(tmp_path):
    flow = _search_flow(
        tmp_path,
        {"Top.bsv": "package Top; endpackage\n", "real/Q.bsv": "package Q; endpackage\n"},
        ["lib"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    (tmp_path / "d" / "lib").mkdir()
    (tmp_path / "d" / "lib" / "linked").symlink_to(tmp_path / "d" / "real")
    flow._stage(tb=False)
    stage = flow.run_path / "bsc_search" / "0" / "linked"
    assert not stage.is_symlink() and stage.is_dir()
    assert (stage / "Q.bsv").is_symlink()


def test_the_stage_of_the_design_root_leaves_out_the_run_root(tmp_path):
    """`search_paths: [.]` with the run root inside the design root: the stage never links the
    run root, or any folder that holds the run directory, into itself."""
    root = tmp_path / "d"
    _write(root, {"Top.bsv": "package Top; endpackage\n", "Other.bsv": "package O; endpackage\n"})
    design = Design(name="s", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    run = root / "xeda_run" / "s" / "bsc"
    run.mkdir(parents=True)
    flow = Bsc(
        {"search_paths": ["."]},
        design,
        run,
        run_directory=RunDirectory.claimed(run, root / "xeda_run"),
    )
    flow._stage(tb=False)
    assert _search_names(flow) == ["Other.bsv"]


def test_a_stale_search_stage_is_replaced(tmp_path):
    flow = _search_flow(
        tmp_path,
        {
            "Top.bsv": "package Top; endpackage\n",
            "lib/A.bsv": "package A; endpackage\n",
            "lib2/B.bsv": "package B; endpackage\n",
        },
        ["lib", "lib2"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    flow._stage(tb=False)
    (flow.run_path / "bsc_search" / "1" / "stale").symlink_to(tmp_path)
    (flow.run_path / "bsc_search" / "7").mkdir()
    flow._stage(tb=False)
    assert sorted(p.name for p in (flow.run_path / "bsc_search").iterdir()) == ["0", "1"]
    assert _search_names(flow, 1) == ["B.bsv"]


def test_a_search_path_that_is_no_folder_fails_the_run(tmp_path):
    """A `search_paths` entry that names no folder (a typo, for one) fails the run and names
    the entry, rather than leaving bsc a search folder that holds nothing."""
    flow = _search_flow(
        tmp_path,
        {"Top.bsv": "package Top; endpackage\n", "lib/A.bsv": "package A; endpackage\n"},
        ["lib", "libz"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    with pytest.raises(FlowSettingsException, match="`search_paths` names 'libz'"):
        flow._stage(tb=False)


def test_the_path_flags_name_no_source_folder_and_no_current_directory(tmp_path):
    design = _accum_design(tmp_path / "d", rtl={"sources": ["lib/Accum.bsv", "rtl/Top.bsv"]})
    flow = _flow(Bsc, design, tmp_path / "run", search_paths=["libs"])
    flags = flow._path_flags("verilog", tmp_path / "run" / "gen_rtl", design.rtl.sources)
    assert flags[flags.index("-p") + 1] == "bsc_sources:bsc_search/0:%/Libraries"
    assert flags[flags.index("-vsearch") + 1] == "bsc_verilog:%/Libraries:%/Verilog"
    assert flags[flags.index("-bdir") + 1] == "bobjs"
    assert flags[flags.index("-vdir") + 1] == "gen_rtl"
    for flag in ("-p", "-vsearch"):
        entries = flags[flags.index(flag) + 1].split(":")
        assert "+" not in entries and "." not in entries


def test_the_stage_holds_exactly_the_listed_files_of_the_parts_the_flow_reads(tmp_path):
    root = tmp_path / "d"
    _write(
        root,
        {
            "Top.bsv": "package Top; endpackage\n",
            "Tb.bsv": "package Tb; endpackage\n",
            "Limit.bsvi": "\n",
            "vlib/m.v": "module m(); endmodule\n",
            "vlib/unlisted.v": "module u(); endmodule\n",
            "other/Unlisted.bsv": "package Unlisted; endpackage\n",
        },
    )
    design = Design(
        name="s",
        root=root,
        rtl={"sources": ["Limit.bsvi", "Top.bsv", "Top.bsv", "vlib/m.v"], "top": "mkTop"},
        tb={"sources": ["Tb.bsv"], "top": "mkTb"},
    )
    rtl_only = _flow(Bsc, design, tmp_path / "run1")._stage_entries(tb=False)
    assert {str(link) for link in rtl_only} == {
        "bsc_sources/Limit.bsvi",
        "bsc_sources/Top.bsv",
        "bsc_verilog/m.v",
    }
    both = _flow(BscSim, design, tmp_path / "run2")._stage_entries(tb=True)
    assert {str(link) for link in both} == {*map(str, rtl_only), "bsc_sources/Tb.bsv"}
    assert rtl_only[Path("bsc_sources/Top.bsv")] == root / "Top.bsv"
    assert bsc_module.staged_name(design.rtl.sources[1]) == Path("bsc_sources/Top.bsv")


def test_two_listed_files_of_one_name_cannot_share_a_stage_folder(tmp_path):
    root = tmp_path / "d"
    _write(root, {"a/P.bsv": "package P; endpackage\n", "b/P.bsv": "package P; endpackage\n"})
    design = Design(name="s", root=root, rtl={"sources": ["a/P.bsv", "b/P.bsv"], "top": "mkP"})
    message = "two listed sources have the file name P.bsv"
    with pytest.raises(FlowSettingsException, match=message) as error:
        Bsc.check_design_supported(design, Bsc.Settings())
    assert str(root / "a" / "P.bsv") in str(error.value)
    assert str(root / "b" / "P.bsv") in str(error.value)
    # The run keeps its own check: the design of a run holds the generated sources.
    with pytest.raises(FlowSettingsException, match=message):
        _flow(Bsc, design, tmp_path / "run")._stage(tb=False)


def test_two_listed_files_whose_names_differ_only_by_case_cannot_share_a_stage_folder(tmp_path):
    """Any OS refuses them, since on a file system that ignores case they would be one file."""
    root = tmp_path / "d"
    _write(
        root, {"a/Pkg.bsv": "package Pkg; endpackage\n", "b/pkg.bsv": "package pkg; endpackage\n"}
    )
    design = Design(name="s", root=root, rtl={"sources": ["a/Pkg.bsv", "b/pkg.bsv"], "top": "mkP"})
    for flow_class in (Bsc, BscSim):
        with pytest.raises(FlowSettingsException, match="differ only by letter case"):
            flow_class.check_design_supported(design, flow_class.Settings())


def test_listed_files_of_other_stage_folders_may_share_a_name_by_case(tmp_path):
    """A Verilog file `P.v` and a Bluespec file `p.bsv` are in two stage folders."""
    root = tmp_path / "d"
    _write(root, {"P.v": "module P(); endmodule\n", "p.bsv": "package p; endpackage\n"})
    design = Design(name="s", root=root, rtl={"sources": ["P.v", "p.bsv"], "top": "mkP"})
    Bsc.check_design_supported(design, Bsc.Settings())


def test_installed_libraries_come_after_the_search_paths_and_before_bsc_libraries(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(
        Bsc,
        design,
        tmp_path / "run",
        search_paths=["libs", "more"],
        installed_libraries=["FPGA/Xilinx", "TLM3"],
    )
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert _pairs(flags, "-p") == [
        "bsc_sources:bsc_search/0:bsc_search/1:%/Libraries/FPGA/Xilinx:%/Libraries/TLM3"
        ":%/Libraries"
    ]
    sim = flow._path_flags("sim", tmp_path / "out", design.rtl.sources)
    assert _pairs(sim, "-p") == _pairs(flags, "-p")


def test_no_installed_library_leaves_the_path_as_it_was(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run")
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert _pairs(flags, "-p") == ["bsc_sources:%/Libraries"]
