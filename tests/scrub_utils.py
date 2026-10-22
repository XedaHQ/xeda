"""Scrub in the tests of the run-directory rules: the answer comes from the prompt of the command.

The library asks nobody. `xeda scrub` asks through `console.input`, so a test that patches
`console.input` answers the same question the user answers, and these helpers pass that prompt as
the confirmation.
"""

from pathlib import Path
from typing import Optional

from xeda.cli import _prompt_scrub
from xeda.flow_runner import scrub as scrub_module


def ask_scrub_design(
    flow_name: str,
    design_dir: Path,
    *,
    run_root: Path,
    target: Optional[str] = None,
) -> scrub_module.ScrubResult:
    """Call `scrub_design` with the prompt of the command as its confirmation."""
    return scrub_module.scrub_design(
        flow_name, design_dir, run_root=run_root, target=target, confirm=_prompt_scrub
    )


def ask_scrub_runs(
    flow_name: str,
    directory: Path,
    exclude=(),
    run_root: Optional[Path] = None,
) -> bool:
    """Call `scrub_runs` with the prompt of the command as its confirmation."""
    return scrub_module.scrub_runs(flow_name, directory, exclude, run_root, confirm=_prompt_scrub)
