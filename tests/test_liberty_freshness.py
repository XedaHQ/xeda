"""The Liberty files of `liberty` are inputs of the OpenROAD chain.

A file-system clock decides whether a recorded run is up to date, so this file is in the list of
the `macos` job.
"""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowDependencyFailure

from .test_openroad_flows import NAMES, chain, launch, openroad_flows  # noqa: F401
from .tiny_platform import tiny_store


@pytest.mark.usefixtures("chain")
def test_a_changed_library_makes_the_chain_stale_and_a_dry_run_agrees(tmp_path, monkeypatch):
    """The files of `liberty` are inputs of every stage: a run that read them before they changed
    is stale, for the launch and for the dry run that never initializes a flow."""
    original = tiny_store(tmp_path / "origin") / "tiny" / "tiny.lib"
    library = tmp_path / "libs" / "a.lib"
    library.parent.mkdir()
    library.write_text(original.read_text())
    runner, design, settings, first = launch(
        tmp_path, monkeypatch, "fail_timing=false", f"liberty={library}"
    )
    assert first is not None and first.succeeded and not first.reused
    given = dict(text.split("=", 1) for text in settings)

    def states_now() -> dict[str, str]:
        _, found = runner.plan_with_freshness("openroad", Design.load(design), flow_settings=given)
        return {name: state.state for name, state in found.items()}

    assert states_now() == dict.fromkeys(NAMES, "fresh")
    library.write_text(library.read_text() + "/* edited */\n")
    assert set(states_now().values()) == {"stale"}
    again = runner.run("openroad", design, flow_settings=settings)
    assert again is not None and again.succeeded and not again.reused


def with_included_unit(library: Path, units: Path) -> None:
    """Move the time unit of `library` into `units`, which `library` includes by its absolute name,
    as OpenSTA reads `include_file`."""
    line = '  time_unit : "1ns";\n'
    assert line in library.read_text()
    units.write_text('time_unit : "1ns";\n')
    library.write_text(library.read_text().replace(line, f"  include_file({units});\n"))


def settings_with_included_unit(tmp_path: Path, where: str) -> tuple[Path, list[str]]:
    """Return the included file, and the settings for a chain whose library includes it: the
    library of `liberty` (`where` is `setting`), or the library of the platform (`platform`)."""
    store = tiny_store(tmp_path)
    units = tmp_path / "parts" / "units.lib"
    units.parent.mkdir()
    if where == "platform":
        with_included_unit(store / "tiny" / "tiny.lib", units)
        return units, []
    library = tmp_path / "libs" / "a.lib"
    library.parent.mkdir()
    library.write_text((store / "tiny" / "tiny.lib").read_text())
    with_included_unit(library, units)
    return units, [f"liberty={library}"]


@pytest.mark.usefixtures("chain")
@pytest.mark.parametrize("where", ["setting", "platform"])
def test_a_file_that_a_library_includes_is_an_input_of_the_chain(tmp_path, monkeypatch, where):
    """Editing only the included file makes every stage stale, for the dry run (which initializes
    no flow) and for the launch."""
    units, given = settings_with_included_unit(tmp_path, where)
    runner, design, settings, first = launch(tmp_path, monkeypatch, "fail_timing=false", *given)
    assert first is not None and first.succeeded and not first.reused
    named = dict(text.split("=", 1) for text in settings)

    def states_now() -> dict[str, str]:
        _, found = runner.plan_with_freshness("openroad", Design.load(design), flow_settings=named)
        return {name: state.state for name, state in found.items()}

    assert states_now() == dict.fromkeys(NAMES, "fresh")
    units.write_text(units.read_text() + "/* edited */\n")
    assert set(states_now().values()) == {"stale"}
    again = runner.run("openroad", design, flow_settings=settings)
    assert again is not None and again.succeeded and not again.reused


@pytest.mark.usefixtures("chain")
@pytest.mark.parametrize("where", ["setting", "platform"])
def test_a_missing_included_file_is_refused_like_a_missing_library(tmp_path, monkeypatch, where):
    """Both stop `openroad_place` when it prepares its inputs, and the message names the file."""
    units, given = settings_with_included_unit(tmp_path, where)
    units.unlink()
    with pytest.raises(FlowDependencyFailure) as refused:
        launch(tmp_path, monkeypatch, "fail_timing=false", *given)
    message = " ".join(str(refused.value).split())
    assert "openroad_place failed" in message
    assert f"includes {units}, which is not a file" in message
    if where == "setting":
        # the library itself, missing, is refused by the same stage
        library = Path(given[0].split("=", 1)[1])
        library.unlink()
        with pytest.raises(FlowDependencyFailure) as missing:
            launch(tmp_path, monkeypatch, "fail_timing=false", *given)
        assert "openroad_place failed" in str(missing.value)
        assert f"`liberty` names {library}, which is not a file" in " ".join(
            str(missing.value).split()
        )


@pytest.mark.usefixtures("chain")
@pytest.mark.parametrize("where", ["setting", "platform"])
def test_a_dry_run_after_an_included_file_is_deleted_judges_the_chain_unknown(
    tmp_path, monkeypatch, where
):
    """The prepared inputs cannot be located, and the dry run says so for the node. It does not
    stop."""
    units, given = settings_with_included_unit(tmp_path, where)
    runner, design, settings, first = launch(tmp_path, monkeypatch, "fail_timing=false", *given)
    assert first is not None and first.succeeded
    units.unlink()
    _, found = runner.plan_with_freshness(
        "openroad",
        Design.load(design),
        flow_settings=dict(text.split("=", 1) for text in settings),
    )
    assert found["openroad_place"].state == "unknown"
    assert "units.lib" in found["openroad_place"].reason
