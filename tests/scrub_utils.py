"""Scrub in the tests of the run-directory rules: the answer comes from the prompt of the command.

The library asks nobody. `xeda scrub` asks through `console.input`, so a test that patches
`console.input` answers the same question the user answers, and these helpers pass that prompt as
the confirmation. The prompt takes the question of a workspace, so the helpers word it as
`Workspace.scrub` does.
"""

from pathlib import Path
from typing import Optional

import xeda.cli as cli_module
from xeda.cli import _print_scrub_result, _prompt_removal
from xeda.flow_runner import scrub as scrub_module
from xeda.workspace import _removal_question


def on_a_terminal(monkeypatch) -> None:
    """Let `xeda scrub` ask its question, as it does on a terminal, also with `--json`."""
    monkeypatch.setattr(cli_module, "_interactive", lambda json_flag: True)


def _ask(candidates) -> bool:
    return _prompt_removal(_removal_question(candidates))


def ask_scrub_design(
    flow_name: str,
    design_dir: Path,
    *,
    run_root: Path,
    target: Optional[str] = None,
) -> scrub_module.ScrubResult:
    """Call `scrub_design` with the prompt of the command, and show the result as it does."""
    result = scrub_module.scrub_design(
        flow_name, design_dir, run_root=run_root, target=target, confirm=_ask
    )
    _print_scrub_result(result)
    return result


def ask_scrub_runs(
    flow_name: str,
    directory: Path,
    exclude=(),
    run_root: Optional[Path] = None,
) -> bool:
    """Call `scrub_runs` with the prompt and the report of the command."""
    return scrub_module.scrub_runs(
        flow_name,
        directory,
        exclude,
        run_root,
        confirm=_ask,
        on_result=_print_scrub_result,
    )
