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
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, **options)
    before = launcher.settings.model_dump()
    launcher.launch_flow(top, design, {})
    assert launcher.settings.model_dump() == before


def test_post_cleanup_applies_to_a_flow_with_a_dependency(tmp_path, toy_flows, design):
    _, top = toy_flows
    launcher = DefaultRunner(tmp_path / "run", display_results=False, post_cleanup=True)
    flow = launcher.launch_flow(top, design, {})
    assert flow.succeeded
    kept = sorted(p.name for p in flow.run_path.iterdir())
    # the depender's scratch file and its dependency's nested run directory are cleaned up
    assert kept == ["results.json", "settings.json"], kept


def test_post_cleanup_purge_applies_to_a_flow_with_a_dependency(tmp_path, toy_flows, design):
    _, top = toy_flows
    launcher = DefaultRunner(
        tmp_path / "run", display_results=False, post_cleanup=True, post_cleanup_purge=True
    )
    flow = launcher.launch_flow(top, design, {})
    assert flow.succeeded
    assert not flow.run_path.exists()


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
