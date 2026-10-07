"""Per-target run directories: a run's directory is
`<run root>/<design>[/<target>]/<flow>[_<hash>]`, and nothing else.

A selected target is part of where a run lives and no part of what it is: two targets that yield
the same effective design have the same hashes and the same flow names, in directories of their
own, built and kept fresh separately. The oracles:

- every path -- planned, entered, direct API, chain, hashed or not -- names the design's own
  target, and a design without one keeps `<design>/<flow>`;
- a plan is bound to the target it was made for, though an equal-effective design of another
  target has equal hashes, and a target that is no name is refused at the path boundary;
- a pre-target run (`<design>/<flow>`) is never taken for a target's, nor touched by it.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import FlowFatalError, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectoryError

from . import tool_utils
from .io_flows import _Join, _Maker, _Taker
from .test_fpga_chains import RESOURCES as CHAINS
from .test_openfpgaloader import assert_fake_loader

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")


@pytest.fixture(autouse=True)
def private_registry():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


#: how a design file writes its targets, and which one a test selects
LAYOUTS = {
    "none": (None, None),
    "empty-table": ({}, None),
    "sole": ({"only": {}}, None),
    "a": ({"a": {}, "b": {}}, "a"),
    "b": ({"a": {}, "b": {}}, "b"),
}
BY_LAYOUT = pytest.mark.parametrize("layout", LAYOUTS, ids=list(LAYOUTS))
#: the target a layout selects (what the design records), None for no target
SELECTED = {"none": None, "empty-table": None, "sole": "only", "a": "a", "b": "b"}


def write_design(tmp_path: Path, targets, sources=(), name="d", file="d.yaml") -> Path:
    """A design file `design/<file>` of the design `name`: one root for every file written."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    document = {"name": name, "rtl": {"sources": list(sources), "top": "t"}}
    if targets is not None:
        document["targets"] = targets
    path = root / file
    path.write_text(yaml.safe_dump(document))
    return path


def loaded(tmp_path: Path, layout: str) -> Design:
    targets, selected = LAYOUTS[layout]
    return Design.from_file(write_design(tmp_path, targets), target=selected)


def runner(tmp_path: Path, **kwargs) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **kwargs)


def parent_of(tmp_path: Path, layout: str, name: str = "d") -> Path:
    """Where a layout's run directories lie: `<design>/<target>`, or `<design>` without one."""
    base = tmp_path / "xeda_run" / name
    return base / SELECTED[layout] if SELECTED[layout] else base


# ---------------------------------------------------------------------- the path table


@BY_LAYOUT
@pytest.mark.parametrize("hashed", [False, True], ids=["unhashed", "hashed"])
def test_every_planned_node_lies_under_its_target(tmp_path, layout, hashed):
    design = loaded(tmp_path, layout)
    assert design.target == SELECTED[layout]
    plan = runner(tmp_path, hashed_run_dirs=hashed).plan(_Taker, design)
    assert [node.name for node in plan.nodes] == ["__maker", "__taker"]
    for node in plan.nodes:
        basename = f"{node.name}_{node.flowrun_hash[:16]}" if hashed else node.name
        assert node.run_path == parent_of(tmp_path, layout) / basename
    assert plan.context.target == SELECTED[layout]
    assert not (tmp_path / "xeda_run").exists(), "a plan writes nothing"


@BY_LAYOUT
@pytest.mark.parametrize("hashed", [False, True], ids=["unhashed", "hashed"])
def test_a_launch_enters_exactly_the_planned_directories(tmp_path, layout, hashed):
    design = loaded(tmp_path, layout)
    launcher = runner(tmp_path, hashed_run_dirs=hashed)
    plan = launcher.plan(_Taker, design)
    flow = launcher.run(_Taker, design)
    assert flow.succeeded
    assert [f.run_path for f in launcher.launched] == [n.run_path for n in plan.nodes]
    # a producer is a sibling of its consumer *within the target*, never nested under it
    parent = parent_of(tmp_path, layout)
    assert {f.run_path.parent for f in launcher.launched} == {parent}
    assert {p for p in (tmp_path / "xeda_run" / "d").rglob("results.json")} == {
        f.run_path / "results.json" for f in launcher.launched
    }


def test_equal_targets_have_equal_hashes_and_separate_directories(tmp_path):
    path = write_design(tmp_path, {"a": {}, "b": {}})
    a, b = (Design.from_file(path, target=t) for t in "ab")
    launcher = runner(tmp_path, hashed_run_dirs=True)
    plan_a, plan_b = launcher.plan(_Taker, a), launcher.plan(_Taker, b)
    assert [n.flowrun_hash for n in plan_a.nodes] == [n.flowrun_hash for n in plan_b.nodes]
    assert plan_a.context.design_hash == plan_b.context.design_hash
    for node_a, node_b in zip(plan_a.nodes, plan_b.nodes):
        assert node_a.run_path.name == node_b.run_path.name  # one basename ...
        assert node_a.run_path != node_b.run_path  # ... in two parents
        assert node_a.run_path.parent.name == "a" and node_b.run_path.parent.name == "b"


def test_a_b_a_b_builds_each_target_once_and_keeps_each_fresh(tmp_path):
    path = write_design(tmp_path, {"a": {}, "b": {}})
    outcomes = []
    for target in "abab":
        launcher = runner(tmp_path)
        design = Design.from_file(path, target=target)
        flow = launcher.run(_Taker, design)
        assert flow.succeeded
        outcomes.append((target, [f.reused for f in launcher.launched]))
        assert flow.run_path == tmp_path / "xeda_run" / "d" / target / "__taker"
    assert outcomes == [
        ("a", [False, False]),
        ("b", [False, False]),  # B does not find A's finished runs
        ("a", [True, True]),
        ("b", [True, True]),
    ]


def test_a_source_edit_makes_a_target_stale_and_never_moves_its_hashed_directory(tmp_path):
    root = tmp_path / "design"
    root.mkdir()
    data = root / "given.dat"
    data.write_text("one\n")
    path = write_design(
        tmp_path, {"a": {}, "b": {}}, sources=[{"file": "given.dat", "type": "Data"}]
    )

    def build(target):
        launcher = runner(tmp_path, hashed_run_dirs=True)
        flow = launcher.run(_Taker, Design.from_file(path, target=target))
        return flow, [f.run_path for f in launcher.launched]

    first, paths = build("a")
    build("b")
    assert first.results["read"] == "one\n"
    data.write_text("two\n")
    again, again_paths = build("a")
    assert not again.reused and again.results["read"] == "two\n"
    assert again_paths == paths, "the directory is named by the hash of settings, not content"
    assert all(p.parent.name == "a" for p in paths)
    # B's directory was neither read nor touched by A's rebuild
    assert build("b")[0].results["read"] == "two\n"


@pytest.mark.parametrize("hashed", [False, True], ids=["unhashed", "hashed"])
def test_the_direct_apis_name_the_designs_target(tmp_path, hashed):
    path = write_design(tmp_path, {"a": {}, "b": {}})
    design = Design.from_file(path, target="b")
    launcher = runner(tmp_path, hashed_run_dirs=hashed)
    plan = launcher.resolve(_Taker, design, {})
    suffix = f"_{plan.node('__taker').flowrun_hash[:16]}" if hashed else ""
    taker_path = tmp_path / "xeda_run" / "d" / "b" / f"__taker{suffix}"
    assert plan.node("__taker").run_path == taker_path
    assert launcher.run_path_of("d", "__taker", plan.node("__taker").flowrun_hash, target="b") == (
        taker_path
    )
    assert launcher.get_flow_run_path(
        "d", "__taker", plan.node("__taker").flowrun_hash, target="b"
    ) == (taker_path)
    flow = launcher.launch_flow(_Taker, design, {})  # no plan: identity names the path itself
    assert flow.run_path == taker_path and flow.succeeded
    flow = launcher.run_flow(_Taker, design, {}, plan=plan)
    assert flow.run_path == taker_path


def test_no_target_keeps_the_pre_target_path(tmp_path):
    launcher = runner(tmp_path)
    assert launcher.run_path_of("d", "__taker") == tmp_path / "xeda_run" / "d" / "__taker"
    assert launcher.run_path_of("d", "__taker", target=None) == (
        tmp_path / "xeda_run" / "d" / "__taker"
    )
    assert launcher.run_path_of("d", "__taker", "0123456789abcdef0123", target="t") == (
        tmp_path / "xeda_run" / "d" / "t" / "__taker"
    )


def test_a_diamond_runs_each_node_once_in_the_target_in_plan_order(tmp_path):
    """`__join` takes a fork's outputs through two branches: one node per producer, all in the
    target's directory, entered exactly as planned."""
    design = Design.from_file(write_design(tmp_path, {"a": {}, "b": {}}), target="b")
    launcher = runner(tmp_path, hashed_run_dirs=True)
    plan = launcher.plan(_Join, design)
    assert len(plan.nodes) > 3
    assert {node.run_path.parent for node in plan.nodes} == {tmp_path / "xeda_run" / "d" / "b"}
    assert launcher.run(_Join, design).succeeded
    assert [f.run_path for f in launcher.launched] == [n.run_path for n in plan.nodes]
    assert len({f.run_path for f in launcher.launched}) == len(plan.nodes)


@pytest.mark.parametrize("target, flow", [("arty", "vivado_synth"), ("ulx3s", "nextpnr")])
@pytest.mark.parametrize("hashed", [False, True], ids=["unhashed", "hashed"])
def test_a_selected_target_plans_the_flat_design_but_for_its_directory(
    tmp_path, target, flow, hashed
):
    """The flat design, with the one thing that may differ: the target's name and the directory it
    names. Every node's settings, identity, input origins and switches are the flat design's."""
    from .test_targets import KNIGHT, RESOURCES

    launcher = runner(tmp_path, hashed_run_dirs=hashed)
    selected = launcher.plan(flow, design=KNIGHT, target=target)
    flat = launcher.plan(flow, design=RESOURCES / f"knight_{target}_flat.yaml")
    assert [n.name for n in selected.nodes] == [n.name for n in flat.nodes]
    for chosen, plain in zip(selected.nodes, flat.nodes):
        assert chosen.flowrun_hash == plain.flowrun_hash
        assert chosen.settings == plain.settings
        assert chosen.origins == plain.origins and chosen.switched_on == plain.switched_on
        assert chosen.run_path == plain.run_path.parent / target / plain.run_path.name
    assert (selected.context.target, flat.context.target) == (target, None)
    assert selected.context.design_hash == flat.context.design_hash


# ---------------------------------------------------------------- a chain, through the CLI

CHAIN = "yosys_fpga+nextpnr+fpga_pack"
STAGES = CHAIN.split("+")


@pytest.fixture
def toolchain(tmp_path, monkeypatch) -> Path:
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    assert assert_fake_loader() == prefix / "bin/openFPGALoader"
    return prefix


def chain_design(tmp_path: Path, targets) -> Path:
    """The ULX3S blinky as a design file with `targets`."""
    root = tmp_path / "design"
    shutil.copytree(CHAINS, root, ignore=shutil.ignore_patterns("__pycache__"))
    document = yaml.safe_load((root / "ulx3s.yaml").read_text())
    document["targets"] = targets
    (root / "targeted.yaml").write_text(yaml.safe_dump(document))
    return root / "targeted.yaml"


def xeda(*args) -> tuple:
    result = CliRunner().invoke(cli, [str(arg) for arg in (*args, "--json")])
    return result, json.loads(result.stdout)


@pytest.mark.parametrize("selection", [["--target", "a"], []], ids=["explicit", "sole"])
def test_a_chain_runs_every_stage_in_its_target(tmp_path, toolchain, selection):
    targets = {"a": {}, "b": {}} if selection else {"a": {}}
    design = chain_design(tmp_path, targets)
    result, document = xeda("run", CHAIN, design, *selection)
    assert result.exit_code == 0, result.output
    root = tmp_path / "xeda_run" / "chain_ulx3s" / "a"
    assert [Path(n["run_path"]) for n in document["nodes"]] == [root / s for s in STAGES]
    assert document["target"] == "a"
    assert sorted(p.name for p in (tmp_path / "xeda_run" / "chain_ulx3s").iterdir()) == ["a"]


def test_a_chain_of_two_equal_targets_builds_in_two_places_and_stays_fresh_in_each(
    tmp_path, toolchain
):
    design = chain_design(tmp_path, {"a": {}, "b": {}})
    base = tmp_path / "xeda_run" / "chain_ulx3s"

    def states(*selection):
        result, document = xeda("run", CHAIN, design, "--hashed-run-dirs", *selection)
        assert result.exit_code == 0, result.output
        return {n["node"]: n["state"] for n in document["nodes"]}, [
            Path(n["run_path"]) for n in document["nodes"]
        ]

    ran_a, paths_a = states("--target", "a")
    ran_b, paths_b = states("--target", "b")
    assert set(ran_a.values()) == set(ran_b.values()) == {"ran"}
    assert [p.name for p in paths_a] == [p.name for p in paths_b]
    assert {p.parent for p in paths_a} == {base / "a"} and {p.parent for p in paths_b} == {
        base / "b"
    }
    assert set(states("--target", "a")[0].values()) == {"fresh"}
    assert set(states("--target", "b")[0].values()) == {"fresh"}
    # the two fake toolchains ran once per stage and target, no stage twice
    calls = sorted((tmp_path / "xeda_run").rglob("fake_fpga.calls.jsonl"))
    assert len(calls) == 2 * len(STAGES) and len({c.parent for c in calls}) == len(calls)


def test_a_dry_run_plans_the_target_s_directories_and_writes_nothing(tmp_path, toolchain):
    design = chain_design(tmp_path, {"a": {}, "b": {}})
    result, document = xeda("run", CHAIN, design, "--target", "b", "--dry-run")
    assert result.exit_code == 0, result.output
    base = tmp_path / "xeda_run" / "chain_ulx3s" / "b"
    assert [Path(n["run_path"]) for n in document["plan"]["nodes"]] == [base / s for s in STAGES]
    assert not (tmp_path / "xeda_run").exists()


# ---------------------------------------------------------------------- plan binding


def test_a_launcher_keeps_each_design_s_paths_however_it_is_reused(tmp_path):
    path = write_design(tmp_path, {"a": {}, "b": {}})
    a, b = (Design.from_file(path, target=t) for t in "ab")
    launcher = runner(tmp_path)
    plan_a, plan_b = launcher.resolve(_Taker, a, {}), launcher.resolve(_Taker, b, {})
    flow = launcher.launch_flow(_Taker, a, {}, plan=plan_a)
    assert flow.run_path == tmp_path / "xeda_run" / "d" / "a" / "__taker"
    flow = launcher.launch_flow(_Taker, b, {}, plan=plan_b)
    assert flow.run_path == tmp_path / "xeda_run" / "d" / "b" / "__taker"
    # the last-loaded target is reporting metadata: naming a path never reads it
    launcher.target = "a"
    assert launcher.plan(_Taker, b).node("__taker").run_path.parent.name == "b"


@pytest.mark.parametrize("through", ["plan", "resolve"])
def test_a_plan_of_one_target_is_refused_for_an_equal_design_of_another(tmp_path, through):
    path = write_design(tmp_path, {"a": {}, "b": {}})
    a, b = (Design.from_file(path, target=t) for t in "ab")
    launcher = runner(tmp_path)
    plan = launcher.plan(_Taker, a) if through == "plan" else launcher.resolve(_Taker, a, {})
    assert launcher.plan(_Taker, b).context.design_hash == plan.context.design_hash
    with pytest.raises(FlowFatalError, match="does not match this request's context"):
        launcher.launch_flow(_Taker, b, {}, plan=plan)
    with pytest.raises(FlowFatalError, match="does not match this request's context"):
        launcher.run_flow(_Taker, b, {}, plan=plan)
    assert not (tmp_path / "xeda_run").exists(), "refused before the run root, a lock, a run"


def test_a_plan_of_a_target_is_refused_for_the_same_design_without_one(tmp_path):
    targeted = Design.from_file(write_design(tmp_path, {"a": {}}))
    flat = Design.from_file(write_design(tmp_path, None, file="flat.yaml"))
    launcher = runner(tmp_path)
    assert targeted.target == "a" and flat.target is None
    plan = launcher.resolve(_Taker, targeted, {})
    flat_plan = launcher.resolve(_Taker, flat, {})
    assert plan.context.design_hash == flat_plan.context.design_hash
    with pytest.raises(FlowFatalError, match="does not match this request's context"):
        launcher.launch_flow(_Taker, flat, {}, plan=plan)
    with pytest.raises(FlowFatalError, match="does not match this request's context"):
        launcher.launch_flow(_Taker, targeted, {}, plan=flat_plan)


@pytest.mark.parametrize("target", ["../out", "a/b", "", "has space", "1st", ".", "..", "a\0b"])
def test_a_target_that_is_no_name_is_refused_at_the_path_boundary(tmp_path, target):
    launcher = runner(tmp_path)
    with pytest.raises(RunDirectoryError, match="not a target name"):
        launcher.run_path_of("d", "__taker", target=target)
    with pytest.raises(RunDirectoryError, match="not a target name"):
        launcher.get_flow_run_path("d", "__taker", target=target)
    # a design that carries one (a model is built with any `target`) cannot plan or launch
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    design = Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}, target=target)
    with pytest.raises(RunDirectoryError, match="not a target name"):
        launcher.plan(_Taker, design)
    with pytest.raises(RunDirectoryError, match="not a target name"):
        launcher.launch_flow(_Taker, design, {})
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize(
    "target", ["vivado_synth", "VivadoSynth", "nextpnr", "vivado-synth", "NEXTPNR"]
)
def test_a_target_is_not_named_as_a_flow_at_the_path_boundary(tmp_path, target):
    """`<design>/<target>/<flow>` and a flow's own `<design>/<flow>` would be one directory."""
    with pytest.raises(RunDirectoryError, match="name of a flow"):
        runner(tmp_path).run_path_of("d", "vivado_synth", target=target)


def test_targets_that_differ_only_in_letter_case_are_refused_at_load(tmp_path):
    """One directory on a file system that ignores case: the loader refuses the table."""
    from xeda.design import DesignValidationError

    path = write_design(tmp_path, {"Arty": {}, "arty": {}})
    with pytest.raises(DesignValidationError, match="differ only in letter case"):
        Design.from_file(path, target="arty")


def test_a_target_directory_leading_out_of_the_run_root_is_refused(tmp_path):
    launcher = runner(tmp_path)
    design = Design.from_file(write_design(tmp_path, {"a": {}}))
    plan_root = tmp_path / "xeda_run"
    from xeda.run_root import ensure_run_root

    ensure_run_root(plan_root, start=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plan_root / "d").mkdir()
    (plan_root / "d" / "a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunDirectoryError, match="leads out"):
        launcher.plan(_Taker, design)
    with pytest.raises(RunDirectoryError, match="leads out"):
        launcher.launch_flow(_Taker, design, {})
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------- the older layout


def test_a_pre_target_run_is_neither_reused_nor_touched_by_a_target(tmp_path):
    """No migration, no fallback: `<design>/<flow>` holds a run no board was recorded for."""
    flat = Design.from_file(write_design(tmp_path, None, file="flat.yaml"))
    launcher = runner(tmp_path)
    assert launcher.run(_Taker, flat).succeeded
    direct = [tmp_path / "xeda_run" / "d" / name for name in ("__maker", "__taker")]
    before = tool_utils.run_outputs_state(*direct)

    targeted = Design.from_file(write_design(tmp_path, {"a": {}}))
    launcher = runner(tmp_path)
    flow = launcher.run(_Taker, targeted)
    assert flow.succeeded and not flow.reused
    assert [f.reused for f in launcher.launched] == [False, False]
    assert flow.run_path == tmp_path / "xeda_run" / "d" / "a" / "__taker"
    assert tool_utils.run_outputs_state(*direct) == before

    # and a request without a target goes on using the direct runs
    launcher = runner(tmp_path)
    assert launcher.run(_Taker, flat).reused
    assert tool_utils.run_outputs_state(*direct) == before


def test_a_target_run_directory_is_not_a_run_of_the_flat_layout(tmp_path):
    targeted = Design.from_file(write_design(tmp_path, {"a": {}}))
    assert runner(tmp_path).run(_Taker, targeted).succeeded
    base = tmp_path / "xeda_run" / "d"
    assert sorted(p.name for p in base.iterdir() if p.is_dir()) == ["a"]
    flat = Design.from_file(write_design(tmp_path, None, file="flat.yaml"))
    launcher = runner(tmp_path)
    assert not launcher.run(_Taker, flat).reused
    assert sorted(p.name for p in base.iterdir() if p.is_dir()) == ["__maker", "__taker", "a"]


def test_the_maker_alone_is_not_reached_through_the_taker_s_target(tmp_path):
    """A producer requested by itself lives in the same target parent as when it is a producer."""
    design = Design.from_file(write_design(tmp_path, {"a": {}}))
    launcher = runner(tmp_path)
    assert launcher.run(_Maker, design).succeeded
    assert launcher.launched[-1].run_path == tmp_path / "xeda_run" / "d" / "a" / "__maker"
    taker = runner(tmp_path)
    assert taker.run(_Taker, design).succeeded
    assert [f.reused for f in taker.launched] == [True, False]


# ------------------------------------------------------------------------- the remote's mirror


def test_the_local_mirror_of_a_remote_run_lies_in_the_designs_target(tmp_path, monkeypatch):
    """`--remote` mirrors into `<design>/<target>/<flow>_<hash>`: two targets, two mirrors.
    (Nothing is shipped: the remote never sees the target; only where the mirror is changes.)"""
    from xeda.flow_runner.remote import RemoteRunner

    class _Named(Exception):
        pass

    def named(self, design_name, flow_name, identity, *, target=None):
        raise _Named((design_name, flow_name, identity, target))

    monkeypatch.setattr(RemoteRunner, "get_flow_run_path", named)
    path = write_design(tmp_path, {"a": {}, "b": {}})
    seen = {}
    for target in "ab":
        with pytest.raises(_Named) as named_as:
            RemoteRunner(tmp_path / "mirror").run_remote(
                Design.from_file(path, target=target), _Taker.name, "fake"
            )
        seen[target] = named_as.value.args[0]
    assert seen["a"][:2] == seen["b"][:2] == ("d", "__taker")
    assert seen["a"][2] == seen["b"][2], "one identity: the target is no part of it"
    assert (seen["a"][3], seen["b"][3]) == ("a", "b")


# --------------------------------------------------------- a removed flow's name is no target's

REMOVED = ["open_xc7", "openxc7", "open-xc7", "OpenXc7", "OpenXC7", "OPEN_XC7"]


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_flow_s_name_is_refused_as_a_target_name(tmp_path, name):
    """`<design>/open_xc7` may be a legacy run directory: a target may not take its place."""
    from xeda.design import DesignValidationError

    with pytest.raises(DesignValidationError, match="name of a flow"):
        Design.from_file(write_design(tmp_path, {name: {}}))
    with pytest.raises(RunDirectoryError, match="name of a flow"):
        runner(tmp_path).run_path_of("d", "nextpnr", target=name)
