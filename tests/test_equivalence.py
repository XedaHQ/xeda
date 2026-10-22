"""The equivalence digest of an output: what a type calls the same, for a consumer that asks.

A yosys JSON netlist carries `src` attributes, the file and line each module, cell and net came
from. A comment added above the logic moves every line number and nothing else. The digest of the
netlist leaves `src` out of the `attributes` of modules, cells, nets and memories, and nothing
more, so a consumer that does not read source locations can stay fresh. A consumer opts in with
`In(..., equivalence=True)`; the producer records the digest of every output of such a type.
"""

import copy
import json
from pathlib import Path

import pytest

from xeda.flow import MetricSpec
from xeda import Design
from xeda.design import SourceType
from xeda.digest import content_digest
from xeda.equivalence import NORMALIZERS, digests, equivalence_digest
from xeda.flow import Flow, In
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr, YosysFpga

from .settings_samples import flow_classes
from .test_declared_fpga_flows import BLINK, PART, tools  # noqa: F401  (the stand-in tools)
from .tool_utils import producers_of

NETLIST = {
    "creator": "Yosys 0.99",
    "modules": {
        "top": {
            "attributes": {"top": "00000000000000000000000000000001", "src": "top.v:1.1-9.10"},
            "ports": {"clk": {"direction": "input", "bits": [2]}},
            "cells": {
                "ff": {
                    "hide_name": 0,
                    "type": "$_DFF_P_",
                    "parameters": {},
                    "attributes": {"src": "top.v:4.3-6.6", "keep": "1"},
                    "port_directions": {"C": "input", "D": "input", "Q": "output"},
                    "connections": {"C": [2], "D": [3], "Q": [4]},
                }
            },
            "netnames": {
                "clk": {"hide_name": 0, "bits": [2], "attributes": {"src": "top.v:2.9-2.12"}}
            },
            "memories": {"ram": {"attributes": {"src": "top.v:5.1-5.20"}, "width": 8, "size": 16}},
        }
    },
}


def _write(tmp_path: Path, document, name: str = "n.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document) if not isinstance(document, str) else document)
    return path


def _digest(tmp_path: Path, document, name: str = "n.json") -> str | None:
    return equivalence_digest(_write(tmp_path, document, name), (SourceType.JsonNetlist,))


def _with(**changes):
    """The netlist with `changes` applied by a function on the copy."""
    document = copy.deepcopy(NETLIST)
    for apply in changes.values():
        apply(document)
    return document


def test_the_digest_ignores_the_source_locations_of_every_kind_of_object(tmp_path):
    shifted = _with(
        a=lambda d: d["modules"]["top"]["attributes"].update(src="top.v:2.1-10.10"),
        b=lambda d: d["modules"]["top"]["cells"]["ff"]["attributes"].update(src="top.v:5.3-7.6"),
        c=lambda d: d["modules"]["top"]["netnames"]["clk"]["attributes"].update(
            src="top.v:3.9-3.12"
        ),
        d=lambda d: d["modules"]["top"]["memories"]["ram"]["attributes"].update(
            src="top.v:6.1-6.20"
        ),
    )
    assert _digest(tmp_path, shifted, "b.json") == _digest(tmp_path, NETLIST)
    assert _digest(tmp_path, NETLIST) is not None


def test_the_digest_ignores_key_order_and_spacing(tmp_path):
    spaced = json.dumps(NETLIST, indent=4, sort_keys=True)
    assert _digest(tmp_path, spaced, "b.json") == _digest(tmp_path, NETLIST)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["modules"]["top"]["cells"]["ff"]["connections"].update(D=[5]),
        lambda d: d["modules"]["top"]["cells"]["ff"]["parameters"].update(WIDTH="1"),
        lambda d: d["modules"]["top"]["cells"]["ff"]["attributes"].update(keep="0"),
        lambda d: d["modules"]["top"]["cells"]["ff"]["attributes"].update(other="x"),
        lambda d: d["modules"]["top"]["cells"]["ff"].update(type="$_DFF_N_"),
        lambda d: d["modules"]["top"]["attributes"].update(top="2"),
        lambda d: d["modules"]["top"]["ports"]["clk"].update(bits=[9]),
        lambda d: d["modules"]["top"]["memories"]["ram"].update(size=32),
        lambda d: d.update(creator="Yosys 1.0"),
        lambda d: d["modules"].update(extra={"ports": {}, "cells": {}}),
    ],
    ids=[
        "connection",
        "parameter",
        "other attribute value",
        "other attribute",
        "cell type",
        "module attribute",
        "port",
        "memory",
        "creator",
        "module",
    ],
)
def test_the_digest_sees_every_other_difference(tmp_path, change):
    document = _with(change=change)
    assert _digest(tmp_path, document, "b.json") != _digest(tmp_path, NETLIST)


def test_a_name_that_is_src_is_not_a_source_location(tmp_path):
    """Only the `src` key under `attributes` is dropped: a port, a net, a cell or a module
    named `src`, and a `src` key anywhere else, are the design's."""
    base = _digest(tmp_path, NETLIST)
    for change in (
        lambda d: d["modules"]["top"]["ports"].update(src={"direction": "input", "bits": [7]}),
        lambda d: d["modules"]["top"]["netnames"].update(src={"bits": [7], "attributes": {}}),
        lambda d: d["modules"]["top"]["cells"].update(src={"type": "$_NOT_", "attributes": {}}),
        lambda d: d["modules"].update(src={"cells": {}, "ports": {}}),
        lambda d: d["modules"]["top"]["cells"]["ff"].update(src="top.v:4"),
        lambda d: d["modules"]["top"]["cells"]["ff"]["parameters"].update(src="1"),
    ):
        assert _digest(tmp_path, _with(change=change), "b.json") != base
    # and a source location of an object that is named `src` is still a location
    named = _with(
        a=lambda d: d["modules"]["top"]["cells"].update(
            src={"type": "$_NOT_", "attributes": {"src": "top.v:8"}}
        )
    )
    moved = copy.deepcopy(named)
    moved["modules"]["top"]["cells"]["src"]["attributes"]["src"] = "top.v:9"
    assert _digest(tmp_path, moved, "c.json") == _digest(tmp_path, named, "d.json")


def test_a_file_that_is_not_a_netlist_has_no_digest(tmp_path):
    for text in ("not json", "[1, 2]", "", '{"modules": 3}'):
        path = _write(tmp_path, text, "bad.json")
        digest = equivalence_digest(path, (SourceType.JsonNetlist,))
        if text == '{"modules": 3}':
            assert digest is not None  # valid JSON of another shape: compared as it is
        else:
            assert digest is None, text
    assert equivalence_digest(tmp_path / "missing.json", (SourceType.JsonNetlist,)) is None


def test_a_type_without_a_normalizer_has_no_digest(tmp_path):
    path = _write(tmp_path, NETLIST)
    assert equivalence_digest(path, (SourceType.Verilog,)) is None
    assert equivalence_digest(path, (SourceType.JsonNetlist, SourceType.Verilog)) is None
    assert set(NORMALIZERS) == {SourceType.JsonNetlist}


# --------------------------------------------------------------------------- the declaration


def test_an_input_that_opts_in_needs_a_type_that_has_a_normalizer():
    class _Ok(Flow):
        """Opts in on a JSON netlist."""

        results_description: dict[str, MetricSpec] = {}

        class Inputs(Flow.Inputs):
            netlist: Path = In(SourceType.JsonNetlist, equivalence=True, description="A netlist.")

        def run(self) -> None:
            pass

    try:
        with pytest.raises(TypeError, match="equivalence"):

            class _Bad(Flow):
                """Opts in on a type that has no normalizer."""

                results_description: dict[str, MetricSpec] = {}

                class Inputs(Flow.Inputs):
                    text: Path = In(SourceType.Verilog, equivalence=True, description="Text.")

                def run(self) -> None:
                    pass

    finally:
        for name in (_Ok.name, _Ok.__name__):
            registered_flows.pop(name, None)


def test_only_nextpnr_s_netlist_opts_in():
    from xeda.flow.io import declared_inputs

    opted = {
        (cls.name, name)
        for cls, _ in flow_classes()
        for name, d in declared_inputs(cls).items()
        if d.equivalence
    }
    assert opted == {("nextpnr", "netlist")}


# ------------------------------------------------------------------- recorded and compared

CASES = {
    "same": None,
    "comment": "src",
    "logic": "cell",
}


def _netlist(root: Path, line: int, extra_cell: bool = False) -> str:
    document = copy.deepcopy(NETLIST)
    document["modules"]["top"]["cells"]["ff"]["attributes"]["src"] = f"top.v:{line}.3-6.6"
    if extra_cell:
        document["modules"]["top"]["cells"]["extra"] = {"type": "$_NOT_", "connections": {}}
    return json.dumps(document)


@pytest.fixture
def varying_synthesis(monkeypatch, tools):  # noqa: F811
    """`yosys_fpga` writes the netlist `state` says; nextpnr's stand-in is the shared one."""
    state = {"line": 4, "extra": False}

    def run(self):
        netlist = self.run_path / self.settings.netlist_json
        netlist.write_text(_netlist(self.run_path, state["line"], state["extra"]))
        self.outputs.netlist = netlist

    monkeypatch.setattr(YosysFpga, "run", run)
    return state, tools


def _design(root: Path) -> Design:
    root.mkdir(exist_ok=True)
    if not (root / "blink.v").exists():
        (root / "blink.v").write_text(BLINK)
    return Design(name="blink", design_root=root, rtl={"sources": ["blink.v"], "top": "blink"})


def _launch(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run_flow(Nextpnr, _design(tmp_path / "d"), {"fpga": {"part": PART}})
    assert flow is not None and flow.succeeded
    return flow, runner


def test_the_producer_records_the_digest_of_the_output_of_a_type_that_defines_one(
    tmp_path, monkeypatch, varying_synthesis
):
    flow, runner = _launch(tmp_path, monkeypatch)
    (yosys,) = producers_of(runner, flow)
    recorded = yosys.results["outputs"]["netlist"]
    path = Path(recorded["path"])
    assert recorded["equivalence"] == equivalence_digest(path, (SourceType.JsonNetlist,))
    # a type that defines none records none
    assert "equivalence" not in flow.results["outputs"]["config"]


def test_a_netlist_that_differs_only_in_source_locations_leaves_nextpnr_fresh(
    tmp_path, monkeypatch, varying_synthesis, caplog
):
    state, calls = varying_synthesis
    first, _ = _launch(tmp_path, monkeypatch)
    assert len(calls) == 1
    (tmp_path / "d" / "blink.v").write_text("// a line above the logic\n" + BLINK)
    state["line"] = 5
    caplog.set_level("INFO")
    flow, runner = _launch(tmp_path, monkeypatch)
    (yosys,) = producers_of(runner, flow)
    assert not yosys.reused, "the edit re-runs synthesis"
    assert flow.reused and flow.stale_reason is None, flow.stale_reason
    assert flow.run_id == first.run_id
    assert len(calls) == 1, "nextpnr did not run again"
    assert "differs from the one it consumed only in what its type ignores" in caplog.text
    # the next launch finds both up to date, and says nothing of a re-run
    caplog.clear()
    again, _ = _launch(tmp_path, monkeypatch)
    assert again.reused and "ran again" not in caplog.text


def test_a_netlist_that_differs_in_the_logic_runs_nextpnr_again(
    tmp_path, monkeypatch, varying_synthesis
):
    state, calls = varying_synthesis
    _launch(tmp_path, monkeypatch)
    (tmp_path / "d" / "blink.v").write_text(BLINK.replace("~q", "q"))
    state["extra"] = True
    flow, _ = _launch(tmp_path, monkeypatch)
    assert not flow.reused and flow.stale_reason == "`netlist` from `yosys_fpga` changed"
    assert len(calls) == 2


def test_a_python_hook_in_nextpnr_stops_the_comparison_without_source_locations(
    tmp_path, monkeypatch, varying_synthesis
):
    """A hook runs inside nextpnr and can read a cell's `src` attribute to decide where to place
    it: with one, a netlist whose source locations moved is not the netlist that was placed."""
    state, calls = varying_synthesis
    root = tmp_path / "d"
    root.mkdir()
    (root / "hook.py").write_text("# a placement hook\n")
    monkeypatch.chdir(tmp_path)

    def launch(**settings):
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        design = _design(root)
        flow = runner.run_flow(Nextpnr, design, {"fpga": {"part": PART}, **settings})
        assert flow is not None and flow.succeeded
        return flow

    for hook in ({"pre_place": "hook.py"}, {"extra_args": ["--pre-place=hook.py"]}):
        settings = Nextpnr.Settings.from_input({"fpga": {"part": PART}, **hook})
        assert not Nextpnr.input_equivalence(settings, "netlist")
    assert Nextpnr.input_equivalence(
        Nextpnr.Settings.from_input({"fpga": {"part": PART}}), "netlist"
    )
    launch(pre_place=str(root / "hook.py"))
    (root / "blink.v").write_text("// a line above the logic\n" + BLINK)
    state["line"] = 5
    flow = launch(pre_place=str(root / "hook.py"))
    # with a hook, nextpnr is judged on every source too: the edit itself moves it
    assert not flow.reused and "blink.v" in flow.stale_reason, flow.stale_reason
    assert len(calls) == 2


def test_both_digests_are_of_the_one_read_of_the_file(tmp_path, monkeypatch):
    """A file edited between two reads could give a content digest of one netlist and an
    equivalence digest of another; `digests` reads the file once."""
    import hashlib

    path = _write(tmp_path, NETLIST)
    types = (SourceType.JsonNetlist,)
    assert digests(path, types) == (content_digest(path), equivalence_digest(path, types))
    changed = _with(change=lambda d: d.update(creator="Yosys 1.0"))
    other = json.dumps(changed).encode()
    of_other, of_original = _digest(tmp_path, changed, "o.json"), _digest(tmp_path, NETLIST)
    assert of_other != of_original
    reads = []

    def read_bytes(self):
        reads.append(self)
        return other

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    sha, equivalence = digests(path, types)
    assert len(reads) == 1
    assert sha == hashlib.sha3_256(other).hexdigest()[:32]
    assert equivalence == of_other


def test_a_type_without_a_normalizer_is_streamed_and_has_no_equivalence(tmp_path):
    path = _write(tmp_path, NETLIST)
    assert digests(path, (SourceType.Verilog,)) == (content_digest(path), None)
