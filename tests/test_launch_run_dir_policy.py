"""What a launch does to its run directory is decided per launch.

A dependency runs in its depender's run directory, so it is always incremental, never scrubs
old runs and never cleans up after itself. That used to be done by writing those values onto
the launcher's *shared* settings whenever a run path was given -- that is, on every dependency
launch. So launching any flow with a dependency switched off the depender's own
`--post-cleanup`/`--post-cleanup-purge` (its `_report` runs after its dependencies), and left a
reused launcher incremental and non-scrubbing for every later launch.
"""

from typing import ClassVar

import pytest

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner

EXAMPLE = "examples/vhdl/sqrt/sqrt.toml"


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
    dict(incremental=False),
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
    kept = sorted(p.name for p in flow.run_path.iterdir())
    # the depender's scratch file and its dependency's nested run directory are cleaned up
    assert kept == ["results.json", "settings.json"], kept


def test_post_cleanup_purge_applies_to_a_flow_with_a_dependency(tmp_path, toy_flows, design):
    """Post cleanup purge applies to a flow with a dependency."""
    _, top = toy_flows
    launcher = DefaultRunner(
        tmp_path / "run", display_results=False, post_cleanup=True, post_cleanup_purge=True
    )
    flow = launcher.launch_flow(top, design, {})
    assert flow.succeeded
    assert not flow.run_path.exists()


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
    """Preserve internal targets, and never clean a run directory outside the run root."""

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
        launcher = DefaultRunner(
            tmp_path / "run",
            display_results=False,
            cached_dependencies=False,
            incremental=True,
            post_cleanup=True,
        )
        alias = launcher.get_flow_run_path(design.name, LinkedFlow.name)
        actual = (launcher.xeda_run_dir if target_inside_run_root else tmp_path) / "actual_flow"
        actual.mkdir()
        alias.parent.mkdir()
        alias.symlink_to(actual, target_is_directory=True)
        flow = launcher.launch_flow(LinkedFlow, design, {})
        assert flow.succeeded
        assert (flow.run_path / "link.txt").read_text() == "keep"
        assert (flow.run_path / "scratch.txt").exists() is not target_inside_run_root
    finally:
        for name in (LinkedFlow.name, LinkedFlow.__name__):
            registered_flows.pop(name, None)


def test_a_reused_launcher_stays_non_incremental(tmp_path, toy_flows, design):
    """`incremental=False` starts every launch from an empty run directory -- including the
    ones after a launch that happened to have dependencies."""
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, incremental=False)
    first = launcher.launch_flow(top, design, {})
    stale = first.run_path / "stale.txt"
    stale.write_text("from the previous run\n")
    second = launcher.launch_flow(top, design, {})
    assert second.run_path == first.run_path
    assert not stale.exists()
    assert second.incremental is False


def test_a_dependency_is_incremental_in_its_depender_run_directory(tmp_path, toy_flows, design):
    """The per-launch rule itself: a dependency runs incrementally inside its depender's run
    directory and does not clean up after itself (its depender decides what is kept)."""
    dep, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, incremental=False)
    flow = launcher.launch_flow(top, design, {})
    (completed,) = flow.completed_dependencies
    assert isinstance(completed, dep)
    assert completed.incremental is True
    assert completed.run_path == flow.run_path / dep.name
    assert (completed.run_path / "dep_scratch.txt").exists()
