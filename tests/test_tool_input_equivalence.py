"""Goldens for what `vivado_postsynth_sim` and `vivado_power` hand their tools, recorded
from the flows as they were before they took their producers' files through declared inputs and
outputs.

`vivado_postsynth_sim` runs `vivado_synth` and simulates its netlist; `vivado_power` runs
`vivado_postsynth_sim` and reports power from its activity file against the routed checkpoint.
Moving them to declared inputs and outputs had to change none of what the tools are handed. The goldens
record what the earlier code did, and the flows are held to them: a later change that moves
anything recorded must either be a bug, or go on `REVIEWED_DELTAS` below, with the reason.

Each request is launched as a user would launch it (`xeda run <flow> design.yaml -s ...`, in a
subprocess, on the fake tools) and records, per run directory it entered:

* which run directories there are, relative to the run root;
* the flow's effective settings (`settings.json`);
* every command the fake tool recorded (`tool_utils.fake_calls`);
* the content digest of every file those commands name -- the sources, netlists, SDF files,
  checkpoints, SAIF file, liberty files, the merged library, the maps and the scripts, and every
  file a script names in turn (a text file's digest is taken after the paths in it are replaced by
  placeholders, so no digest names a directory);
* the record in `results.json` (`xeda.records`), minus what names the run rather than what it did:
  its outcome and error, its metrics, the kind and the files of each output, and its artifacts.

Every request is spelled with the flat `-s flows.<flow>.<key>=...` of a producer's own section,
the one spelling the flows accept now. The hashed run-directory layout is deliberately not
recorded: a node's hashed directory name follows from its settings and the origins of its inputs,
which differ from the earlier flows' settings, so those names must differ.

What the fakes carry (checked here, `test_the_fake_vivado_writes_the_seven_outputs`):

* The fake `vivado` writes, for each `write_verilog`, `write_sdf` and `write_checkpoint`, a file
  that says what asked for it, so a flow that handed on the wrong netlist can be told -- the command, its mode and
  corner (`funcsim`/`timesim`, `fast`/`slow`), the top, and for a checkpoint the stage it is named
  for (`post_synth`, `post_route`) -- and never a path, so the same request in another directory
  writes the same bytes. `funcsim.v`, `timesim.v`, both SDF corners, both `.dcp` files, the
  exported constraints and the SAIF file come out non-empty and pairwise distinct. The SAIF file
  follows what was simulated (the sources analyzed, the SDF files annotated), and the power
  estimate's XML follows the checkpoint opened and the activity read, so what a flow gives its
  producer's files to shows in its own results. Nothing here falls back to the call record alone.
`yosys_asic` is different: no earlier code recorded it. It records what `yosys` is handed under an
ASIC configuration, on the fake `yosys` and the `tiny` test platform (a platform file and a PDK
store in the launch directory), so that a later change to what the flow hands its tool shows.

The goldens are `tests/resources/tool_input_equivalence/*.json`. Setting `XEDA_TESTS_CAPTURE_GOLDENS=1`
writes a golden that does not exist yet and never replaces one: a golden is not regenerated, a
difference is a bug or a reviewed delta.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest

import xeda
from xeda.records import read_record
from xeda.units import Time

from .tiny_platform import TINY_RESIDUE, tiny_store
from .tool_utils import (
    REQUIRE_TOOLS,
    fake_calls,
    use_fake_asic_tools,
    use_fake_tools,
)

GOLDENS = Path(__file__).parent / "resources" / "tool_input_equivalence"
CAPTURE = os.environ.get("XEDA_TESTS_CAPTURE_GOLDENS", "").lower() in ("1", "true", "yes", "on")
PLATFORMS = Path(xeda.__file__).parent / "platforms"

#: What a conversion legitimately changes, each entry a path into a golden (`/`-separated, `*`
#: for any one key; a key that itself holds `/`, such as a file's path, is named whole) and the
#: reason, per request. It is removed from the golden and from what is observed before they are
#: compared. A tuple of keys names each key whole.
#: Settings that went from every flow, or from a nested flow's settings.
_REMOVED_EVERYWHERE = {
    **{
        f"nodes/*/effective_flow_settings/{key}": "no flow read this setting: it is removed"
        for key in ("timeout_seconds", "no_console")
    },
    **{
        f"nodes/*/effective_flow_settings/ghdl/{key}": "a nested flow's settings: the same "
        "settings, or ones `ghdl_synth` does not use (it compiles and links nothing)"
        for key in (
            "timeout_seconds",
            "no_console",
            "compiler_flags",
            "assembler_flags",
            "linker_flags",
        )
    },
}
_SYNTH = "nodes/sim/vivado_synth"
_SYNTH_DELTAS = {
    f"{_SYNTH}/effective_flow_settings/write_timing_netlist": "A new setting "
    "beside `write_netlist`, which the consumer switches on with it, so the synthesis writes "
    "what it wrote before",
    f"{_SYNTH}/results/outputs": "`vivado_synth`'s declared outputs, recorded "
    "with their digests; the files they name are the ones recorded under `files`",
    **{
        f"{_SYNTH}/effective_flow_settings/{run}/steps": "a run writes nothing it derives into "
        "its settings: the empty `ARGS` and `TCL` of each step, `flatten_hierarchy` and the "
        "hooks it attaches are in the script and the hooks, which stay compared under `files`"
        for run in ("synth", "impl")
    },
    f"{_SYNTH}/files/<RUN>/sim/vivado_synth/post_route_design_hook.tcl": "The "
    "route hook writes the netlist, the timing netlist with its SDF and the constraints in "
    "blocks of their own; it writes the same files in the same order",
}
_SIM = "nodes/sim/vivado_postsynth_sim"
_POSTSYNTH_DELTAS = {
    **_SYNTH_DELTAS,
    f"{_SIM}/effective_flow_settings/synth": "nested synthesis settings are removed; "
    "the synthesis node's own complete settings remain compared with the golden",
    f"{_SIM}/effective_flow_settings/fpga": "FPGA shared leaves agree along declared edges",
    f"{_SIM}/effective_flow_settings/clocks": "clock shared leaves agree along declared edges",
    **{
        f"{_SIM}/effective_flow_settings/{key}": "a simulation has no use for it, and nothing "
        "read it: it is a setting of the flows that synthesize"
        for key in ("suppress_msgs", "initialize_zeros", "read_oneshot")
    },
    f"{_SIM}/results/outputs": "checked activity output records; their file contents "
    "remain compared independently",
}
_POWER = "nodes/sim/vivado_power"
# Power's settings model changed along with its base class. These are settings-shape
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
    "suppress_msgs",
    "timeout",
    "timing_sim",
    "vcd",
    "vcd_level",
    "vcd_scope",
    "work_lib",
    "xelab_log",
)
REVIEWED_DELTAS: dict[str, dict[tuple[str, ...] | str, str]] = {
    "vivado_postsynth_sim_functional": {**_POSTSYNTH_DELTAS},
    "vivado_postsynth_sim_timing": {
        **_POSTSYNTH_DELTAS,
        f"{_SIM}/results/artifacts/saif": "the direct timing request's newly required activity",
        f"{_SIM}/files/<RUN>/sim/vivado_postsynth_sim/activity.saif": "the direct timing "
        "request's newly required activity; its dependence on timing annotation is tested separately",
    },
    "vivado_power": {
        **_POSTSYNTH_DELTAS,
        f"{_SIM}/effective_flow_settings/elab_debug": "VivadoSim.run uses typical "
        "without propagating an elab_debug setting; the xelab command remains compared",
        **{
            f"{_POWER}/effective_flow_settings/{key}": "power's new reporter "
            "settings model removes simulation and nested producer settings"
            for key in _POWER_SETTINGS_REMOVED
        },
        **{
            f"{_POWER}/effective_flow_settings/{key}": "shared FPGA/clock leaves agree "
            "with the synthesis node, whose settings remain compared"
            for key in ("fpga", "clocks")
        },
        f"{_POWER}/results/sim.*": "the producer checks simulation evidence before "
        "activity hand-over; power reports only its own metrics",
        **{
            f"{_POWER}/results/Fake: {row}": "a summary row that names no metric gives none; "
            "the checkpoint and the activity that the report was made from stay compared under "
            "`calls` and `files`"
            for row in ("activity", "checkpoint")
        },
    },
    "yosys_asic": {
        "nodes/*/effective_flow_settings/script_format": "Xeda selects the format of the script "
        "from the settings, and no setting chooses it; the script and the commands that run it "
        "are compared",
    },
}

#: Where a conversion moved a file without changing it: in what is observed, each name is
#: replaced by the one the golden recorded, per request, so the file's digest and every command
#: that names it are still compared.
REVIEWED_RENAMES: dict[str, dict[str, str]] = {
    # the activity a consumer (or a timing request) asks of the simulation is written under the
    # conventional name of a deliverable, `outputs/<design>.saif`, as a located one is; the file
    # and the commands that name it are the golden's, bar the name
    name: {"outputs/sim.saif": "activity.saif"}
    for name in ("vivado_postsynth_sim_timing", "vivado_power")
}

for _deltas in REVIEWED_DELTAS.values():
    _deltas.update(_REMOVED_EVERYWHERE)

#: The times of a clock are quantities with units now, and `settings.json` writes them as text
#: (`"5 ns"`) where the goldens have the number of nanoseconds (`5.0`). The same clock is meant
#: if and only if the text is that of the golden's number in nanoseconds: `5 ns` for `5.0`. Only
#: the leaves `clocks/<name>/<field>` are compared this way; every other leaf compares as it is.
CLOCK_TIME_FIELDS = ("period", "rise", "uncertainty", "skew")

#: The same clocks, spelled on the command line with a unit where the goldens' requests have a
#: bare number of nanoseconds (the request records how it was spelled).
REVIEWED_REQUEST_SPELLINGS = {
    "flows.vivado_synth.clock.period=5ns": "flows.vivado_synth.clock.period=5.0",
    "clock.period=2ns": "clock.period=2.0",
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
        # The recorder is set up after the simulation loads and before it runs, and closed after:
        # a recording that moved would be filtered out below and go unnoticed.
        names = [call[0] for call in node["calls"]]
        loaded, run = names.index("xsim"), names.index("run")
        positions = [
            names.index(name) for name in ("open_saif", "describe", "get_objects", "log_saif")
        ]
        assert loaded < positions[0] < positions[1] < positions[2] < positions[3] < run
        assert run < names.index("close_saif")
    node["calls"] = [call for call in node["calls"] if call[0] not in activity_commands]
    return record


VIVADO_SETTINGS = (
    "flows.vivado_synth.fpga=xc7a12tcsg325-1",
    "flows.vivado_synth.clock.period=5ns",
)


@dataclass(frozen=True)
class Request:
    flow: str
    settings: tuple[str, ...]
    #: the fake tools and the design the request runs on: "vivado", or "yosys" for `yosys_asic`
    kind: str = "vivado"


REQUESTS: dict[str, Request] = {
    "vivado_postsynth_sim_functional": Request("vivado_postsynth_sim", VIVADO_SETTINGS),
    "vivado_postsynth_sim_timing": Request(
        "vivado_postsynth_sim", (*VIVADO_SETTINGS, "timing_sim=true")
    ),
    "vivado_power": Request("vivado_power", VIVADO_SETTINGS),
    "yosys_asic": Request(
        "yosys",
        ("platform_file=tiny.yaml", "clock.period=2ns", "abc.strategy=speed_roundtrip"),
        kind="yosys",
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


def write_yosys_asic_design(root: Path, work: Path) -> list[str]:
    """Write a design, the platform file and the PDK store of the `tiny` platform; return the
    settings that locate the store."""
    (root / "top.v").write_text(
        "module top(input clk, input rst, input [3:0] a, output reg [3:0] q);\n"
        "  always @(posedge clk) if (rst) q <= 0; else q <= q + a;\n"
        "endmodule\n"
    )
    (root / "design.yaml").write_text(
        'name: counter\nrtl:\n  sources: ["top.v"]\n  top: top\n  clock:\n    port: clk\n'
    )
    shutil.copy(TINY_RESIDUE, root / "tiny.yaml")
    return [f"pdk_root={tiny_store(work, 'pdks')}"]


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


def results_of_the_record(run_dir: Path) -> dict:
    """What the record in `results.json` says of the run, without what names the run (its id, its
    times, its identity)."""
    document = read_record(run_dir, run_root=run_dir.parent).to_dict()
    return {
        "outcome": document["outcome"],
        "error": document["error"],
        "metrics": document["metrics"],
        "outputs": {
            name: {"kind": output["kind"], "names": output["names"]}
            for name, output in document["outputs"].items()
        },
        "artifacts": document["artifacts"],
    }


def record_node(run_dir: Path, marks: Placeholders) -> dict[str, Any]:
    calls = fake_calls(run_dir)
    files = handed_files(run_dir, calls)
    settings = json.loads((run_dir / "settings.json").read_text())
    node: dict[str, Any] = {
        "effective_flow_settings": marks.value(settings["effective_flow_settings"]),
        "calls": marks.value(calls),
        "files": {
            marks.text(str(path)): file_digest(path, marks)
            for path in sorted(files, key=lambda p: marks.text(str(p)))
        },
        "results": marks.value(results_of_the_record(run_dir)),
    }
    # a script is read where it is not a digest: what it asks the tool for is what a reviewer
    # compares, and `black_box` shows nowhere else
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
    items = list(request.settings)
    if request.kind == "yosys":
        items += write_yosys_asic_design(design, work)
        use_fake_asic_tools(monkeypatch, work / "bin")
    else:
        write_vivado_design(design)
        use_fake_tools(monkeypatch)
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


def without(record: Any, deltas: dict[tuple[str, ...] | str, str]) -> Any:
    """`record` without the entries `deltas` names: a tuple of keys, or `/`-separated keys, `*`
    for any one. In the `/`-separated form a key may itself hold `/` (a node's
    `sim/vivado_synth`, a file's path): any run of segments that names a key is taken for it."""

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
        record = prune(record, list(delta) if isinstance(delta, tuple) else delta.split("/"))
    return record


def golden_clock_times(observed: Any, golden: Any, path: tuple[str, ...] = ()) -> Any:
    """Return `observed` with each leaf `clocks/<name>/<field>` (a field of `CLOCK_TIME_FIELDS`)
    that is the text of the golden's number of nanoseconds replaced by that number, and the
    command-line spellings of `REVIEWED_REQUEST_SPELLINGS` by the golden's. A leaf that is
    anything else stays as observed, so that a difference shows."""
    if isinstance(observed, dict) and isinstance(golden, dict):
        return {
            key: golden_clock_times(value, golden[key], (*path, key)) if key in golden else value
            for key, value in observed.items()
        }
    if isinstance(observed, list) and isinstance(golden, list) and len(observed) == len(golden):
        return [
            golden_clock_times(value, expected, path) for value, expected in zip(observed, golden)
        ]
    if (
        len(path) >= 3
        and path[-3] == "clocks"
        and path[-1] in CLOCK_TIME_FIELDS
        and isinstance(observed, str)
        and isinstance(golden, (int, float))
        and not isinstance(golden, bool)
        and observed == str(Time.of(golden, "ns"))
    ):
        return golden
    if (
        isinstance(observed, str)
        and observed in REVIEWED_REQUEST_SPELLINGS
        and golden == REVIEWED_REQUEST_SPELLINGS[observed]
    ):
        return golden
    return observed


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
            if REQUESTS[name].kind == "vivado":
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
    ), f"no golden for {name}: XEDA_TESTS_CAPTURE_GOLDENS=1 records a missing one"
    deltas = REVIEWED_DELTAS.get(name, {})
    expected = without(
        activity_recording_delta(json.loads(path.read_text()), name, baseline=True), deltas
    )
    text = render(observed)
    for new, old in REVIEWED_RENAMES.get(name, {}).items():
        text = text.replace(new, old)
    recorded = golden_clock_times(json.loads(text), json.loads(path.read_text()))
    assert without(activity_recording_delta(recorded, name), deltas) == expected


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
    "outputs/sim.saif": "vivado_postsynth_sim",
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
        Request("vivado_postsynth_sim", (*VIVADO_SETTINGS, "saif=activity.saif")),
        tmp_path,
        monkeypatch,
    )
    run = next(run for run in functional["nodes"] if run.endswith("vivado_postsynth_sim"))
    timing_run = next(run for run in timing["nodes"] if run.endswith("vivado_postsynth_sim"))
    assert only(digests(functional, run, "activity.saif")) != only(
        digests(timing, timing_run, "outputs/sim.saif")
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


def test_only_the_text_of_the_golden_number_of_nanoseconds_is_accepted_for_a_clock_time() -> None:
    """The comparison of a clock's times is narrow: `5 ns` stands for the golden's `5.0` in
    `clocks/<name>/period`, and nothing else stands for anything."""
    golden = {"effective_flow_settings": {"clocks": {"main_clock": {"period": 5.0, "rise": 0.0}}}}

    def observe(**clock: Any) -> dict[str, Any]:
        return {"effective_flow_settings": {"clocks": {"main_clock": clock}}}

    same = golden_clock_times(observe(period="5 ns", rise="0 ns"), golden)
    assert same == golden
    assert golden_clock_times(observe(period="5000 ps", rise="0 ns"), golden) != golden
    assert (
        golden_clock_times(observe(period="6 ns", rise="0 ns"), golden)["effective_flow_settings"][
            "clocks"
        ]["main_clock"]["period"]
        == "6 ns"
    )
    # a leaf that is no clock time is not accepted, whatever it says
    other = {"effective_flow_settings": {"timeout": 5.0}}
    assert golden_clock_times({"effective_flow_settings": {"timeout": "5 ns"}}, other) != other
    # the golden's own text is not a number to convert
    assert golden_clock_times(
        observe(period="5 ns"),
        {"effective_flow_settings": {"clocks": {"main_clock": {"period": "5 ns"}}}},
    ) == observe(period="5 ns")
