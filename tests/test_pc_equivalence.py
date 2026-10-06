"""Goldens for flows that take their producer's files through `add_dependency`: what each of them
hands its tools, recorded before the flows are converted to declared inputs and outputs.

`vivado_postsynth_sim` runs `vivado_synth` and simulates its netlist; `vivado_power` runs
`vivado_postsynth_sim` and reports power from its activity file against the routed checkpoint;
`openroad` builds the whole configuration of its `yosys` dependency from its own settings. A
conversion of them must change none of what the tools are handed. After it, the old behavior is
gone, so what it did is recorded here, from the unchanged code, and the converted flows are held
to it. This test passes on the code it was written against; a later change that moves anything
recorded must either be a bug, or go on `REVIEWED_DELTAS` below, with the reason.

Each request is launched as a user would launch it (`xeda run <flow> design.yaml -s ...`, in a
subprocess, on the fake tools) and records, per run directory it entered:

* which run directories there are, relative to the run root;
* the flow's effective settings (`settings.json`);
* every command the fake tool recorded (`tool_utils.fake_calls`);
* the content digest of every file those commands name -- the sources, netlists, SDF files,
  checkpoints, SAIF file, liberty files, the merged library, the maps and the scripts, and every
  file a script names in turn (a text file's digest is taken after the paths in it are replaced by
  placeholders, so no digest names a directory);
* `results.json`, minus what names the run rather than what it did (hashes, run time, time stamp,
  tool versions).

Every request is spelled with the flat `-s flows.<flow>.<key>=...` of a flow's dependency: that is
the spelling that survives the conversion (the nested `synth.<key>` and `postsynthsim.<key>` do
not), so no request needs mapping when the same test is run on the converted flows. The hashed
run-directory layout is deliberately not recorded: a node's hashed directory name follows from its
settings and the origins of its inputs, and the shape of the settings changes with the conversion,
so those names must differ.

What the fakes carry (checked here, `test_the_fake_vivado_writes_the_seven_outputs`):

* The fake `vivado` used to record `write_verilog`, `write_sdf` and `write_checkpoint` as Tcl calls
  and write nothing, so a flow could not be told to have handed on the wrong netlist: no netlist
  existed. It now writes, for each, a file that says what asked for it -- the command, its mode and
  corner (`funcsim`/`timesim`, `fast`/`slow`), the top, and for a checkpoint the stage it is named
  for (`post_synth`, `post_route`) -- and never a path, so the same request in another directory
  writes the same bytes. `funcsim.v`, `timesim.v`, both SDF corners, both `.dcp` files, the
  exported constraints and the SAIF file come out non-empty and pairwise distinct. The SAIF file
  follows what was simulated (the sources analyzed, the SDF files annotated), and the power
  estimate's XML follows the checkpoint opened and the activity read, so what a flow gives its
  producer's files to shows in its own results. Nothing here falls back to the call record alone.
* No yosys, OpenROAD or KLayout can be relied on, and a real yosys's netlist changes with its
  release. `tests/fake_tools/fake_asic_tool.py` stands in for the three (not on `PATH` unless a
  test puts them there): everything it writes carries the digest of the contents of the files it
  was handed, never their names. Its reports are what `openroad` parses.

What the three nondefault `openroad` configurations capture (each changes what `yosys` is handed,
and none can be derived from `platform` alone), checked in `test_each_openroad_variant_...`:

* `dont_use_cells`: the merged library differs from the default's, since the cells are marked
  `dont_use` in it.
* `corner`: the library differs from the same platform's default corner, which is the comparison
  that attributes the difference to `corner`. `asap7` ships no liberty files, so the request names
  a copy of its shipped description under the design's directory, with a tiny liberty file per
  corner written there (`stage_asap7`, deterministic: the compressed ones carry no time stamp).
  `-s platform=asap7` itself cannot run on a checkout.
* `blocks`: **the liberty and the merged library are identical to the default's.** `blocks` only
  becomes the `black_box` of `yosys`, which renders it as a `blackbox <module>` command in the
  script; nothing about a library follows from it. The variant is distinct through the script
  `yosys` is handed (and the netlist that follows from it), and that is what is recorded and
  asserted. A conversion that dropped it would still hand yosys the same libraries.

The goldens are `tests/resources/pc_equivalence/*.json`. Setting `XEDA_PC_EQUIVALENCE_CAPTURE=1`
writes a golden that does not exist yet and never replaces one: a golden is not regenerated, a
difference is a bug or a reviewed delta.
"""

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest

import xeda

from .tool_utils import (
    REQUIRE_TOOLS,
    fake_calls,
    use_fake_asic_tools,
    use_fake_tools,
)

GOLDENS = Path(__file__).parent / "resources" / "pc_equivalence"
CAPTURE = os.environ.get("XEDA_PC_EQUIVALENCE_CAPTURE", "").lower() in ("1", "true", "yes", "on")
PLATFORMS = Path(xeda.__file__).parent / "platforms"

#: What a conversion legitimately changes, each entry a path into a golden (`/`-separated, `*`
#: for any one key; a key that itself holds `/`, such as a file's path, is named whole) and the
#: reason, per request. It is removed from the golden and from what is observed before they are
#: compared.
_SYNTH = "nodes/sim/vivado_synth"
_SYNTH_DELTAS = {
    f"{_SYNTH}/effective_flow_settings/write_timing_netlist": "Task 2 (R-PC-a): a new setting "
    "beside `write_netlist`, which the consumer switches on with it, so the synthesis writes "
    "what it wrote before",
    f"{_SYNTH}/results/outputs": "Task 2 (PC-2): `vivado_synth`'s declared outputs, recorded "
    "with their digests; the files they name are the ones recorded under `files`",
    f"{_SYNTH}/files/<RUN>/sim/vivado_synth/post_route_design_hook.tcl": "Task 2 (PCD19): the "
    "route hook writes the netlist, the timing netlist with its SDF and the constraints in "
    "blocks of their own; it writes the same files in the same order",
}
_SIM = "nodes/sim/vivado_postsynth_sim"
_POSTSYNTH_DELTAS = {
    **_SYNTH_DELTAS,
    f"{_SIM}/effective_flow_settings/synth": "Task 3: nested synthesis settings are removed; "
    "the synthesis node's own complete settings remain compared with the golden",
    f"{_SIM}/effective_flow_settings/fpga": "Task 3: FPGA shared leaves agree along declared edges",
    f"{_SIM}/effective_flow_settings/clocks": "Task 3: clock shared leaves agree along declared edges",
    f"{_SIM}/results/outputs": "Task 3: checked activity output records; their file contents "
    "remain compared independently",
}
_POWER = "nodes/sim/vivado_power"
# PCD6 changes power's settings model along with its base class. These are settings-shape
# changes, not power-result exemptions. All actual power metrics and tool inputs stay compared.
_POWER_SETTINGS_REMOVED = (
    "analyze_flags",
    "cocotb",
    "debug_traces",
    "elab_debug",
    "elab_flags",
    "fail_severity",
    "initialize_zeros",
    "optimization_flags",
    "postsynthsim",
    "prerun_time",
    "read_oneshot",
    "saif",
    "sdf",
    "sim_flags",
    "stop_time",
    "timeout",
    "timing_sim",
    "vcd",
    "vcd_level",
    "vcd_scope",
    "work_lib",
    "xelab_log",
)
REVIEWED_DELTAS: dict[str, dict[str, str]] = {
    "vivado_postsynth_sim_functional": {**_POSTSYNTH_DELTAS},
    "vivado_postsynth_sim_timing": {
        **_POSTSYNTH_DELTAS,
        f"{_SIM}/results/artifacts/saif": "Task 3: the direct timing request's newly required activity",
        f"{_SIM}/files/<RUN>/sim/vivado_postsynth_sim/activity.saif": "Task 3: the direct timing "
        "request's newly required activity; its dependence on timing annotation is tested separately",
    },
    "vivado_power": {
        **_POSTSYNTH_DELTAS,
        f"{_SIM}/effective_flow_settings/elab_debug": "R-PC-c: VivadoSim.run uses typical "
        "without propagating an elab_debug setting; the xelab command remains compared",
        **{
            f"{_POWER}/effective_flow_settings/{key}": "PCD6 and R-PC-c: power's new reporter "
            "settings model removes simulation and nested producer settings"
            for key in _POWER_SETTINGS_REMOVED
        },
        **{
            f"{_POWER}/effective_flow_settings/{key}": "Task 4: shared FPGA/clock leaves agree "
            "with the synthesis node, whose settings remain compared"
            for key in ("fpga", "clocks")
        },
        f"{_POWER}/results/sim.*": "PCD6: the producer checks simulation evidence before "
        "activity hand-over; power reports only its own metrics",
    },
}


def activity_recording_delta(record: dict, name: str, *, baseline: bool = False) -> dict:
    """Only a direct timing request gains activity calls. Require their exact sequence and
    compare every remaining call with the unchanged golden; never exempt a whole call record."""
    from copy import deepcopy

    if name != "vivado_postsynth_sim_timing":
        return record
    record = deepcopy(record)
    node = record["nodes"]["sim/vivado_postsynth_sim"]
    activity_commands = {"open_saif", "describe", "get_objects", "log_saif", "close_saif"}
    extra = [call for call in node["calls"] if call[0] in activity_commands]
    if baseline:
        assert not extra, "the original timing golden recorded no activity"
    else:
        assert extra == [
            ["open_saif", "activity.saif"],
            ["describe", "./dut"],
            [
                "get_objects",
                "-r",
                "-filter",
                " type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port ",
                "./dut/*",
            ],
            ["log_saif", "1"],
            ["close_saif"],
        ]
    node["calls"] = [call for call in node["calls"] if call[0] not in activity_commands]
    return record


#: Fields of `results.json` that name the run rather than what it did.
IDENTITY_AND_TIMING = {"design_hash", "flow_hash", "settings_hash", "runtime", "timestamp"}

VIVADO_SETTINGS = (
    "flows.vivado_synth.fpga=xc7a12tcsg325-1",
    "flows.vivado_synth.clock.period=5.0",
)
OPENROAD_SETTINGS = ("platform=nangate45", "clock.period=2.0")
PLATFORM_PLACEHOLDER = "{asap7}"


@dataclass(frozen=True)
class Request:
    flow: str
    settings: tuple[str, ...]
    design: str  # "vivado" or "asic"
    #: the request needs the copy of the asap7 description with liberty files (`stage_asap7`)
    asap7: bool = False


REQUESTS: dict[str, Request] = {
    "vivado_postsynth_sim_functional": Request("vivado_postsynth_sim", VIVADO_SETTINGS, "vivado"),
    "vivado_postsynth_sim_timing": Request(
        "vivado_postsynth_sim", (*VIVADO_SETTINGS, "timing_sim=true"), "vivado"
    ),
    "vivado_power": Request("vivado_power", VIVADO_SETTINGS, "vivado"),
    "openroad": Request("openroad", OPENROAD_SETTINGS, "asic"),
    "openroad_blocks": Request("openroad", (*OPENROAD_SETTINGS, "blocks=mul8"), "asic"),
    "openroad_dont_use_cells": Request(
        "openroad", (*OPENROAD_SETTINGS, "dont_use_cells=AND2_X2"), "asic"
    ),
    "openroad_asap7_ss": Request(
        "openroad",
        ("platform=" + PLATFORM_PLACEHOLDER, "clock.period=2.0", "corner=SS"),
        "asic",
        asap7=True,
    ),
    # the control `openroad_asap7_ss` is compared with: the same platform, its default corner
    "openroad_asap7_tt": Request(
        "openroad", ("platform=" + PLATFORM_PLACEHOLDER, "clock.period=2.0"), "asic", asap7=True
    ),
}


# ------------------------------------------------------------------------------ the designs


def write_vivado_design(root: Path) -> None:
    (root / "top.v").write_text("module top(input a, output y); assign y = ~a; endmodule\n")
    (root / "tb.sv").write_text("module tb; reg a; wire y; top dut(a, y); endmodule\n")
    (root / "design.yaml").write_text(
        "name: sim\n"
        "rtl:\n"
        '  sources: ["top.v"]\n'
        "  top: top\n"
        "  clock:\n"
        "    port: a\n"
        "tb:\n"
        '  sources: ["tb.sv"]\n'
        "  top: tb\n"
        "  uut: dut\n"
    )


def write_asic_design(root: Path) -> None:
    (root / "mac.v").write_text(
        "module mul8(input [7:0] a, b, output [15:0] p);\n"
        "  assign p = a * b;\n"
        "endmodule\n"
        "module mac(input clk, input rst, input [7:0] a, b, output reg [19:0] q);\n"
        "  wire [15:0] p;\n"
        "  mul8 m(.a(a), .b(b), .p(p));\n"
        "  always @(posedge clk) if (rst) q <= 0; else q <= q + p;\n"
        "endmodule\n"
    )
    (root / "design.yaml").write_text(
        "name: mac\nrtl:\n" '  sources: ["mac.v"]\n' "  top: mac\n" "  clock:\n" "    port: clk\n"
    )


def stage_asap7(root: Path) -> Path:
    """A copy of the shipped `asap7` description under `root`, with the liberty files it names for
    each corner, which the package does not ship. Each holds one cell named after its file, so the
    libraries differ by corner and by file; the compressed ones carry no time stamp, so the same
    request in another directory gives the same bytes. The copy's `config.toml`."""
    staged = root / "asap7"
    shutil.copytree(PLATFORMS / "asap7", staged)
    config = tomllib.loads((staged / "config.toml").read_text())
    for corner in config["corner"].values():
        for lib in [*corner["lib_files"], corner["dff_lib_file"]]:
            path = staged / lib
            path.parent.mkdir(exist_ok=True)
            name = path.name.split(".")[0]
            text = f'library (x) {{\n  time_unit : "1ps";\n  cell (CELL_{name}) {{\n    area : 1;\n  }}\n}}\n'
            if lib.endswith(".gz"):
                with (
                    open(path, "wb") as raw,
                    gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
                ):
                    zipped.write(text.encode())
            else:
                path.write_text(text)
    return staged / "config.toml"


# ------------------------------------------------------------------------------ the capture


class Placeholders:
    """Replace what names where a launch happened by a placeholder, longest first, as it is
    spelled and as the file system resolves it (macOS's `/var` is `/private/var`)."""

    def __init__(self, work: Path) -> None:
        pairs = [
            (work / "xeda_run", "<RUN>"),
            (work / "design", "<DESIGN>"),
            (work, "<WORK>"),
            (PLATFORMS, "<PLATFORMS>"),
            (Path(xeda.__file__).parent, "<XEDA>"),
        ]
        self.replacements = sorted(
            {(str(path), mark) for path, mark in pairs}
            | {(os.path.realpath(path), mark) for path, mark in pairs},
            key=lambda pair: -len(pair[0]),
        )

    def text(self, text: str) -> str:
        for prefix, mark in self.replacements:
            text = text.replace(prefix, mark)
        return text

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        return value


def file_digest(path: Path, marks: Placeholders) -> str:
    """The digest of a file's content; of a text file's, after `marks`, so that it does not
    depend on the directory the launch was made in."""
    data = path.read_bytes()
    try:
        return hashlib.sha256(marks.text(data.decode()).encode()).hexdigest()
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()


SCRIPT_SUFFIXES = {".ys", ".tcl"}


def candidate_paths(word: str) -> Iterator[str]:
    """The paths a word of a command or script may name: the word, its parts split at `=` and
    whitespace (`-sdfmax dut=/x.sdf`), and what is left of it inside braces, brackets and quotes."""
    yield word
    for sep in "{}[]\"';":
        word = word.replace(sep, " ")
    for part in word.split():
        yield part
        yield from part.split("=")


def named_files(run_dir: Path, words: list[str]) -> Iterator[Path]:
    """The files the words name, in order of appearance, relative to `run_dir` or absolute."""
    for word in words:
        for candidate in candidate_paths(word):
            path = Path(candidate)
            full = path if path.is_absolute() else run_dir / path
            try:
                if candidate and full.is_file():
                    yield full
            except OSError:
                continue


def handed_files(run_dir: Path, calls: list[list[str]]) -> list[Path]:
    """Every file the commands name, and every file the scripts among them name in turn, once
    each, in the order they were found."""
    found: dict[Path, None] = {}
    pending = [word for call in calls for word in call]
    while pending:
        scripts = []
        for path in named_files(run_dir, pending):
            if path not in found:
                found[path] = None
                if path.suffix in SCRIPT_SUFFIXES:
                    scripts.append(path)
        pending = [word for script in scripts for word in script.read_text().split()]
    return list(found)


def results_without_identity(results: dict) -> dict:
    kept = {key: value for key, value in results.items() if key not in IDENTITY_AND_TIMING}
    # the versions the tools reported are the fakes', and say nothing about the flow
    kept["tools"] = [tool.get("executable") for tool in kept.get("tools", [])]
    return kept


def record_node(run_dir: Path, marks: Placeholders) -> dict[str, Any]:
    calls = fake_calls(run_dir)
    files = handed_files(run_dir, calls)
    settings = json.loads((run_dir / "settings.json").read_text())
    results = json.loads((run_dir / "results.json").read_text())
    node: dict[str, Any] = {
        "effective_flow_settings": marks.value(settings["effective_flow_settings"]),
        "calls": marks.value(calls),
        "files": {
            marks.text(str(path)): file_digest(path, marks)
            for path in sorted(files, key=lambda p: marks.text(str(p)))
        },
        "results": marks.value(results_without_identity(results)),
    }
    # a script is read where it is not a digest: what it asks the tool for is what a reviewer
    # compares, and `blocks` shows nowhere else
    scripts = {
        marks.text(str(path)): marks.text(path.read_text())
        for path in files
        if path.suffix == ".ys"
    }
    if scripts:
        node["scripts"] = dict(sorted(scripts.items()))
    return node


def launch(request: Request, work: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Launch `request` in `work` and return what it recorded."""
    design = work / "design"
    design.mkdir(parents=True)
    run_root = work / "xeda_run"
    (write_vivado_design if request.design == "vivado" else write_asic_design)(design)
    items = list(request.settings)
    if request.asap7:
        config = stage_asap7(design)
        items = [item.replace(PLATFORM_PLACEHOLDER, str(config)) for item in items]
    use_fake_tools(monkeypatch)
    if request.design == "asic":
        use_fake_asic_tools(monkeypatch, work / "bin")
    monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", "finish5")
    command = [
        sys.executable,
        "-m",
        "xeda",
        "run",
        request.flow,
        "design.yaml",
        "--run-root",
        str(run_root),
        "-s",
        *items,
        "--json",
    ]
    proc = subprocess.run(
        command, cwd=design, env=dict(os.environ), capture_output=True, text=True, timeout=600
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:] or document
    marks = Placeholders(work)
    run_dirs = sorted(path.parent for path in run_root.rglob("settings.json"))
    record = {
        "request": {"flow": request.flow, "settings": marks.value(items)},
        "run_directories": [str(path.relative_to(run_root)) for path in run_dirs],
        "nodes": {str(path.relative_to(run_root)): record_node(path, marks) for path in run_dirs},
    }
    # nothing recorded names the directory the launch was made in
    text = render(record)
    assert str(work) not in text and os.path.realpath(work) not in text
    return record


# ------------------------------------------------------------------------------ comparison


def without(record: Any, deltas: dict[str, str]) -> Any:
    """`record` without the entries `deltas` names: `/`-separated keys, `*` for any one. A key may
    itself hold `/` (a node's `sim/vivado_synth`, a file's path): any run of segments that names
    a key is taken for it."""

    def prune(node: Any, path: list[str]) -> Any:
        if not path or not isinstance(node, dict):
            return node
        pruned = dict(node)
        for taken in range(1, len(path) + 1):
            head, rest = "/".join(path[:taken]), path[taken:]
            for key in [key for key in node if fnmatchcase(key, head)] if "*" in head else [head]:
                if key not in pruned:
                    continue
                if rest:
                    pruned[key] = prune(pruned[key], rest)
                else:
                    del pruned[key]
        return pruned

    for delta in deltas:
        record = prune(record, delta.split("/"))
    return record


def require_tclsh() -> None:
    if shutil.which("tclsh"):
        return
    if REQUIRE_TOOLS:
        pytest.fail("tclsh is needed: the fake Vivado runs the scripts it is handed under it")
    pytest.skip("tclsh is needed: the fake Vivado runs the scripts it is handed under it")


@pytest.fixture(scope="module")
def captured(tmp_path_factory) -> Callable[[str], dict[str, Any]]:
    """The record of a request, launched once per module (and worker): the tests below read the
    same launch."""
    records: dict[str, dict[str, Any]] = {}

    def get(name: str) -> dict[str, Any]:
        if name not in records:
            if REQUESTS[name].design == "vivado":
                require_tclsh()
            # a context of its own: the environment the launch needs ends with it
            with pytest.MonkeyPatch.context() as monkeypatch:
                records[name] = launch(REQUESTS[name], tmp_path_factory.mktemp(name), monkeypatch)
        return records[name]

    return get


def golden_path(name: str) -> Path:
    return GOLDENS / f"{name}.json"


def render(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=1, sort_keys=True) + "\n"


@pytest.mark.parametrize("name", REQUESTS)
def test_a_request_hands_its_tools_what_it_did_before_the_conversion(captured, name) -> None:
    observed = captured(name)
    path = golden_path(name)
    if CAPTURE and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(observed))
    assert (
        path.exists()
    ), f"no golden for {name}: XEDA_PC_EQUIVALENCE_CAPTURE=1 records a missing one"
    deltas = REVIEWED_DELTAS.get(name, {})
    expected = without(
        activity_recording_delta(json.loads(path.read_text()), name, baseline=True), deltas
    )
    assert without(activity_recording_delta(json.loads(render(observed)), name), deltas) == expected


# ------------------------------------------------------------------------------ teeth


def digests(record: dict[str, Any], node: str, suffix: str) -> dict[str, str]:
    """The digests of the files of `node` whose names end with `suffix`, by name."""
    return {
        name: digest
        for name, digest in record["nodes"][node]["files"].items()
        if name.endswith(suffix)
    }


def only(found: dict[str, str]) -> str:
    assert len(found) == 1, found
    return next(iter(found.values()))


#: The files of `vivado_synth` and `vivado_postsynth_sim` the power request hands on
VIVADO_OUTPUTS = {
    "funcsim.v": "vivado_synth",
    "timesim.v": "vivado_synth",
    "timesim.min.sdf": "vivado_synth",
    "timesim.max.sdf": "vivado_synth",
    "post_synth.dcp": "vivado_synth",
    "post_route.dcp": "vivado_synth",
    "activity.saif": "vivado_postsynth_sim",
}


def test_the_fake_vivado_writes_the_seven_outputs(captured) -> None:
    """Each of the seven files a Vivado flow hands on is written, non-empty, and says something
    no other of them says. Where this fails the golden can only compare call records."""
    record = captured("vivado_power")
    written = {}
    for name, flow in VIVADO_OUTPUTS.items():
        run = next(run for run in record["nodes"] if run.endswith(flow))
        found = digests(record, run, name)
        assert found, f"{flow} handed no {name} to a tool"
        written[name] = only(found)
    assert len(set(written.values())) == len(written), written
    empty = hashlib.sha256(b"").hexdigest()
    assert empty not in written.values()


def test_the_activity_file_follows_what_was_simulated(captured, tmp_path, monkeypatch) -> None:
    """The activity file of the timing simulation (annotated with the slow-corner SDF, of the
    timing netlist) is not that of the functional one, and so power is estimated from another
    thing: the SAIF file of the same flow, asked for beside a functional simulation, differs."""
    require_tclsh()
    timing = captured("vivado_power")
    functional = launch(
        Request("vivado_postsynth_sim", (*VIVADO_SETTINGS, "saif=activity.saif"), "vivado"),
        tmp_path,
        monkeypatch,
    )
    run = next(run for run in functional["nodes"] if run.endswith("vivado_postsynth_sim"))
    timing_run = next(run for run in timing["nodes"] if run.endswith("vivado_postsynth_sim"))
    assert only(digests(functional, run, "activity.saif")) != only(
        digests(timing, timing_run, "activity.saif")
    )


def test_the_timing_and_functional_simulations_are_handed_different_netlists(captured) -> None:
    functional = captured("vivado_postsynth_sim_functional")
    timing = captured("vivado_postsynth_sim_timing")
    run = next(run for run in functional["nodes"] if run.endswith("vivado_postsynth_sim"))
    assert only(digests(functional, run, "funcsim.v")) != only(digests(timing, run, "timesim.v"))
    assert not digests(timing, run, "funcsim.v") and not digests(functional, run, "timesim.v")
    assert digests(timing, run, "timesim.max.sdf") and not digests(
        functional, run, "timesim.max.sdf"
    )


def merged_library(record: dict[str, Any]) -> str:
    """The merged liberty library `openroad` makes and `yosys` reads: one content, wherever the
    flows keep it (today `yosys` reads the file in `openroad`'s run directory)."""
    found = {
        digest
        for node in record["nodes"].values()
        for name, digest in node["files"].items()
        if name.endswith("/merged.lib")
    }
    assert len(found) == 1, found
    return next(iter(found))


def yosys_script(record: dict[str, Any]) -> str:
    run = next(run for run in record["nodes"] if run.endswith("/yosys"))
    return only(digests(record, run, "yosys_synth.ys"))


def yosys_netlist(record: dict[str, Any]) -> str:
    run = next(run for run in record["nodes"] if run.endswith("/yosys"))
    return only(digests(record, run, "/netlist.v"))


def test_each_openroad_variant_hands_yosys_what_the_default_does_not(captured) -> None:
    """Each nondefault configuration differs from the one it is compared with in what the
    golden records, or it would pass vacuously. `blocks` differs in the script, not the library
    (the module docstring says why); `corner` is compared with the same platform's default."""
    default = captured("openroad")
    dont_use = captured("openroad_dont_use_cells")
    blocks = captured("openroad_blocks")
    ss, tt = captured("openroad_asap7_ss"), captured("openroad_asap7_tt")

    assert merged_library(dont_use) != merged_library(default)
    assert merged_library(ss) != merged_library(tt)

    # `blocks` changes no library, and changes the script (and so the netlist) yosys is handed
    assert merged_library(blocks) == merged_library(default)
    assert yosys_script(blocks) != yosys_script(default)
    assert yosys_netlist(blocks) != yosys_netlist(default)
    run = next(run for run in blocks["nodes"] if run.endswith("/yosys"))
    assert (
        "blackbox mul8"
        in blocks["nodes"][run]["scripts"][
            next(
                name for name in blocks["nodes"][run]["scripts"] if name.endswith("yosys_synth.ys")
            )
        ]
    )
    # none of the others holds the module as a black box
    for other in (default, dont_use, ss, tt):
        run = next(run for run in other["nodes"] if run.endswith("/yosys"))
        assert "blackbox" not in "".join(other["nodes"][run]["scripts"].values())
