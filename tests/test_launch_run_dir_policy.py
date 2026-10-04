"""What a launch does to its run directory is decided per launch.

Every flow, dependencies included, runs in its own directory under `<xeda_run>/<design>/` and
gets the launcher's policy (`clean`, `post_cleanup`, scrubbing); only the directory `--cwd` names
is never cleaned or scrubbed. Post-run clean-ups wait until the requested flow has completed,
since a depender may read any file its dependency wrote. The policy used to be applied by
writing values onto the launcher's *shared* settings whenever a run path was given -- that is,
on every dependency launch -- which switched off the depender's own `--post-cleanup` /
`--post-cleanup-purge` and left a reused launcher changed for every later launch.
"""

from typing import ClassVar

import pytest

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectoryError

EXAMPLE = "examples/vhdl/sqrt/sqrt.yaml"


@pytest.fixture(scope="module")
def toy_flows():
    """A flow with one dependency, both of which leave a file behind in their run directory.

    Defining a `Flow` subclass registers it; the registration is undone afterwards, so the
    flow-wide sweeps of other tests never see these."""

    class ToyDep(Flow):
        """A dependency that writes one scratch file."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "dep_scratch.txt").write_text("scratch\n")

        def parse_reports(self) -> bool:
            return True

    class ToyTop(Flow):
        """A flow with one dependency that writes one scratch file."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyDep, ToyDep.Settings())

        def run(self) -> None:
            (self.run_path / "top_scratch.txt").write_text("scratch\n")

        def parse_reports(self) -> bool:
            return True

    yield ToyDep, ToyTop
    for cls in (ToyDep, ToyTop):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


@pytest.fixture
def design(request):
    """Provide a design for run directory policy tests."""
    return Design.from_file(request.config.rootpath / EXAMPLE)


LAUNCHER_OPTIONS = [
    dict(post_cleanup=True),
    dict(post_cleanup=True, post_cleanup_purge=True),
    dict(clean=True),
    dict(scrub_old_runs=True),
]


@pytest.mark.parametrize("options", LAUNCHER_OPTIONS, ids=lambda o: ",".join(o))
def test_launching_a_flow_with_a_dependency_leaves_the_launcher_settings_alone(
    tmp_path, toy_flows, design, options
):
    """Launching a flow with a dependency leaves the launcher settings alone."""
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, **options)
    before = launcher.settings.model_dump()
    launcher.launch_flow(top, design, {})
    assert launcher.settings.model_dump() == before


def test_post_cleanup_applies_to_a_flow_with_a_dependency(tmp_path, toy_flows, design):
    """Post cleanup applies to a flow with a dependency."""
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, post_cleanup=True)
    flow = launcher.launch_flow(top, design, {})
    assert flow.succeeded
    (dep,) = flow.completed_dependencies
    # each flow's scratch file is cleaned up, in its own run directory -- and its trace: a pruned
    # directory may lack a file a depender reads, so it is never reused
    for run_path in (flow.run_path, dep.run_path):
        kept = sorted(p.name for p in run_path.iterdir())
        assert kept == ["results.json", "settings.json"], kept


def test_post_cleanup_purge_applies_to_a_flow_with_a_dependency(tmp_path, toy_flows, design):
    """Post cleanup purge applies to a flow with a dependency."""
    _, top = toy_flows
    launcher = DefaultRunner(
        tmp_path / "run", display_results=False, post_cleanup=True, post_cleanup_purge=True
    )
    flow = launcher.launch_flow(top, design, {})
    assert flow.succeeded
    (dep,) = flow.completed_dependencies
    assert not flow.run_path.exists() and not dep.run_path.exists()


def test_post_cleanup_keeps_reported_artifacts_and_removes_other_files(tmp_path, design):
    """Artifact values can be nested and can be reported through either results mapping."""
    external = tmp_path / "external.txt"
    external.write_text("outside the run directory")

    class ArtifactFlow(Flow):
        """Write artifacts and scratch files at several depths."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            for name in (
                "top.txt",
                "scratch.txt",
                "outputs/kept.txt",
                "outputs/scratch.txt",
                "reports/result.txt",
                "reports/scratch.txt",
                "bundle/netlist.v",
                "bundle/other.txt",
                "targets/real.txt",
                "unused/scratch.txt",
            ):
                path = self.run_path / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            (self.run_path / "top_link.txt").symlink_to("targets/real.txt")
            (self.run_path / "external_link.txt").symlink_to(external)
            self.artifacts["netlist"] = "top.txt"
            self.artifacts["outputs"] = {"primary": ["outputs/kept.txt", None, ""]}
            self.artifacts["bundle"] = "bundle"
            self.artifacts["links"] = ["top_link.txt", "external_link.txt"]
            self.artifacts["external"] = external
            self.results.artifacts["result"] = "reports/result.txt"

        def parse_reports(self) -> bool:
            return True

    try:
        flow = DefaultRunner(
            tmp_path / "run", display_results=False, post_cleanup=True
        ).launch_flow(ArtifactFlow, design, {})
        assert flow.succeeded
        assert external.read_text() == "outside the run directory"
        assert sorted(str(p.relative_to(flow.run_path)) for p in flow.run_path.rglob("*")) == [
            "bundle",
            "bundle/netlist.v",
            "bundle/other.txt",
            "external_link.txt",
            "outputs",
            "outputs/kept.txt",
            "reports",
            "reports/result.txt",
            "results.json",
            "settings.json",
            "targets",
            "targets/real.txt",
            "top.txt",
            "top_link.txt",
        ]
    finally:
        for name in (ArtifactFlow.name, ArtifactFlow.__name__):
            registered_flows.pop(name, None)


def test_post_cleanup_keeps_a_run_directory_artifact(tmp_path, design):
    """The run directory itself can be a reported directory artifact."""

    class DirectoryFlow(Flow):
        """Report the full run directory."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "whole_run.txt").write_text("keep")
            self.artifacts["directory"] = "."

        def parse_reports(self) -> bool:
            return True

    try:
        flow = DefaultRunner(
            tmp_path / "run", display_results=False, post_cleanup=True
        ).launch_flow(DirectoryFlow, design, {})
        assert flow.succeeded
        assert (flow.run_path / "whole_run.txt").read_text() == "keep"
    finally:
        for name in (DirectoryFlow.name, DirectoryFlow.__name__):
            registered_flows.pop(name, None)


@pytest.mark.parametrize("target_inside_run_root", [True, False])
@pytest.mark.parametrize("absolute_artifact", [True, False])
def test_post_cleanup_through_run_path_alias(
    tmp_path, design, target_inside_run_root, absolute_artifact
):
    """Post-cleanup of a run directory that is itself a link into the run root preserves an
    artifact's internal target; a run directory that a link leads out of the run root is refused
    before anything runs there, so it is never cleaned either."""

    class LinkedFlow(Flow):
        """Report an internal symlink as an artifact."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "target.txt").write_text("keep")
            (self.run_path / "scratch.txt").write_text("remove")
            (self.run_path / "link.txt").symlink_to("target.txt")
            self.artifacts["link"] = self.run_path / "link.txt" if absolute_artifact else "link.txt"

        def parse_reports(self) -> bool:
            return True

    try:
        launcher = DefaultRunner(tmp_path / "run", display_results=False, post_cleanup=True)
        alias = launcher.get_flow_run_path(design.name, LinkedFlow.name)
        actual = (launcher.run_root if target_inside_run_root else tmp_path) / "actual_flow"
        actual.mkdir()
        alias.parent.mkdir()
        alias.symlink_to(actual, target_is_directory=True)
        if not target_inside_run_root:
            with pytest.raises(RunDirectoryError, match="leads out of the run root"):
                launcher.launch_flow(LinkedFlow, design, {})
            assert list(actual.iterdir()) == []
            return
        flow = launcher.launch_flow(LinkedFlow, design, {})
        assert flow.succeeded
        assert (flow.run_path / "link.txt").read_text() == "keep"
        assert not (flow.run_path / "scratch.txt").exists()
    finally:
        for name in (LinkedFlow.name, LinkedFlow.__name__):
            registered_flows.pop(name, None)


def test_a_reused_launcher_keeps_cleaning(tmp_path, toy_flows, design):
    """`clean=True` starts every launch from an empty run directory -- including the ones after
    a launch that happened to have dependencies."""
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, clean=True)
    first = launcher.launch_flow(top, design, {})
    stale = first.run_path / "stale.txt"
    stale.write_text("from the previous run\n")
    second = launcher.launch_flow(top, design, {})
    assert second.run_path == first.run_path
    assert not stale.exists()


def test_a_dependency_runs_in_a_sibling_directory(tmp_path, toy_flows, design):
    """The per-launch rule itself: a dependency runs in its own run directory, beside its
    depender's rather than inside it."""
    dep, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False)
    flow = launcher.launch_flow(top, design, {})
    (completed,) = flow.completed_dependencies
    assert isinstance(completed, dep)
    assert completed.run_path == flow.run_path.parent / dep.name
    assert (completed.run_path / "dep_scratch.txt").exists()
