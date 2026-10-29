"""The workspace: where runs live and the options of one session.

A `Workspace` holds a run root on this machine, the options of the session (the layout of the run
directories, where the output of the tools goes, how a question is answered, the remote host) and
the private settings layers. It holds no declaration and no state of a launch. Several
workspaces, threads and processes may use one run root.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from .errors import SpecError, StalePlanError
from .outcomes import Result
from .flow_runner.launch_options import LaunchOptions, WorkspaceOptions
from .flow_runner.plan_check import workspace_difference
from .local_settings import LocalSettings
from .plan import Plan, dependency_commits, plan_of
from .run_root import DEFAULT_RUN_ROOT, ensure_run_root
from .spec import Spec
from .types import UNSET, PathLike, Unset

__all__ = ["Workspace"]

TOOL_OUTPUTS = ("log", "terminal")

if TYPE_CHECKING:
    from .deliver import Conflict
    from .flow_runner.scrub import ScrubCandidate


def _overwrite_question(conflicts: "Sequence[Conflict]") -> str:
    lines = [f"Xeda would replace {len(conflicts)} file(s) at the output paths you named:"]
    lines += [f"  {c.destination}   {c.why}" for c in conflicts]
    return "\n".join([*lines, "Replace them?"])


def _prune_question(candidates: "Sequence[ScrubCandidate]") -> str:
    lines = [f"Xeda would remove these {len(candidates)} run directories:"]
    for candidate in candidates:
        lines.append(f"  {candidate.path}")
        lines += [f"  {alias} (another name of the same directory)" for alias in candidate.aliases]
    return "\n".join([*lines, "Remove them?"])


class Workspace:
    """The context that plans and runs a `Spec`.

    `run_root` is the directory of the run directories. Without it, the run root comes from
    `local_settings.run_root`, else it is `xeda_run` in the current directory at this call. A
    relative `run_root` resolves against the current directory at this call, once. A directory
    named `xeda_run` in the current directory is adopted by the rule of the run root
    (`xeda.run_root`). The attribute holds the resolved path. The workspace creates nothing: the
    run root is made when a launch first needs it.

    `hashed_run_dirs` adds the hash of the run to the name of a run directory. `tool_output` is
    `"log"` (a tool writes only to the log files of its run directory) or `"terminal"` (it also
    writes to the output and error streams of this process). `confirm` answers the questions that
    a launch asks, and `None` answers "no". `remote` is the host that runs the launches. This
    class does not use it yet, and refuses to plan with one. `local_settings` are the private
    settings layers, as `local_settings()` reads them."""

    run_root: Path
    hashed_run_dirs: bool
    tool_output: Literal["log", "terminal"]
    confirm: Callable[[str], bool] | None
    # `Remote` is not part of this package yet.
    remote: Any | None
    local_settings: LocalSettings | None

    def __init__(
        self,
        run_root: PathLike | Unset = UNSET,
        *,
        hashed_run_dirs: bool = False,
        tool_output: Literal["log", "terminal"] = "log",
        confirm: Callable[[str], bool] | None = None,
        remote: Any | None = None,
        local_settings: LocalSettings | None = None,
    ) -> None:
        if tool_output not in TOOL_OUTPUTS:
            raise ValueError(f"`tool_output` is 'log' or 'terminal', not {tool_output!r}")
        start = Path.cwd()
        if not isinstance(run_root, Unset):
            chosen, source = Path(run_root), "given"
        elif local_settings is not None and local_settings.run_root is not None:
            chosen, source = local_settings.run_root, "local_settings"
        else:
            chosen, source = start / DEFAULT_RUN_ROOT, "default"
        self.run_root = chosen.resolve()
        # Refuse a directory that Xeda cannot take for its own, now and before anything is made.
        ensure_run_root(self.run_root, start=start, create=False)
        self._run_root_source: Literal["given", "local_settings", "default"] = source  # type: ignore[assignment]
        self.hashed_run_dirs = hashed_run_dirs
        self.tool_output = tool_output
        self.confirm = confirm
        self.remote = remote
        self.local_settings = local_settings
        self._start = start

    def __repr__(self) -> str:
        return f"Workspace({str(self.run_root)!r})"

    def _options(self) -> WorkspaceOptions:
        """Return the options of this workspace as a value that a plan keeps and compares."""
        return WorkspaceOptions(
            run_root=self.run_root,
            hashed_run_dirs=self.hashed_run_dirs,
            tool_output=self.tool_output,
            confirm=self.confirm,
            remote=self.remote,
            local_settings=self.local_settings,
        )

    def _refuse_project_run_root(self, spec: Spec) -> None:
        """Refuse a design of a project that names a run root, in the default run root.

        The default run root is the one thing that the workspace takes from the current
        directory. A design that a project selected, in a project that names another run root,
        is most likely meant for that run root. Two nouns, the project and the workspace, need one
        explicit step between them, and this check catches the script that forgot it. A run root
        that was given, or that came from the private settings, is used as it is."""
        layer = spec.design._project_layer  # pylint: disable=protected-access
        if layer is None or self._run_root_source != "default":
            return
        from .project import Project

        project = Project.load(layer[0])
        if project.run_root.resolve() != self.run_root:
            raise SpecError(
                f"the design comes from the project {project.path}, which keeps its runs in "
                f"{project.run_root}, and this workspace uses the default run root "
                f"{self.run_root}. Use `Workspace(project.run_root)`, or "
                "`Workspace(local_settings=xeda.local_settings(project))` for the run root of "
                "the private settings files too.",
                code="run_root_mismatch",
            )

    def plan(
        self,
        spec: Spec,
        /,
        *,
        rebuild_all: bool = False,
        clean: bool = False,
        prune_variants: bool = False,
        outputs_to: PathLike | None = None,
        overwrite_outputs: bool = False,
        post_cleanup: bool = False,
        post_cleanup_purge: bool = False,
    ) -> Plan:
        """Return what a launch of `spec` would do, and run nothing.

        The plan has the states and the deliveries that a launch with the same options would
        meet. A relative `outputs_to` resolves against the current directory now. Planning writes
        nothing but the storage of the Git dependencies of the design, and the lock file of that
        storage, under the run root.

        Raises a `SpecError` for a request that a launch refuses before anything runs (a setting,
        a design, a chain, a delivery or a run root), `run_root_mismatch` for a design of a
        project that names another run root than this workspace's default one."""
        from .flow_runner import DefaultRunner
        from .flow_runner.error_codes import public_error

        if self.remote is not None:
            raise NotImplementedError("planning on a remote host is not supported yet")
        options = LaunchOptions(
            rebuild_all=rebuild_all,
            clean=clean,
            prune_variants=prune_variants,
            outputs_to=None if outputs_to is None else Path(os.path.abspath(outputs_to)),
            overwrite_outputs=overwrite_outputs,
            post_cleanup=post_cleanup,
            post_cleanup_purge=post_cleanup_purge,
        )
        self._refuse_project_run_root(spec)
        try:
            runner = self._runner(DefaultRunner)
            planned = runner.plan_spec(spec, local_settings=self.local_settings, options=options)
        except Exception as error:
            public = public_error(error)
            if public is error:
                raise
            raise public from error
        return plan_of(
            planned.plan,
            planned.states,
            planned.deliveries,
            spec=spec,
            options=options,
            workspace=self._options(),
            dependencies=dependency_commits(planned.request.design),
        )

    def _runner(self, runner_class: Any) -> Any:
        return runner_class(
            self.run_root,
            hashed_run_dirs=self.hashed_run_dirs,
            display_results=False,
            run_root_start=self._start,
        )

    def run(
        self,
        what: Spec | Plan,
        /,
        *,
        rebuild_all: bool | Unset = UNSET,
        clean: bool | Unset = UNSET,
        prune_variants: bool | Unset = UNSET,
        outputs_to: PathLike | None | Unset = UNSET,
        overwrite_outputs: bool | Unset = UNSET,
        post_cleanup: bool | Unset = UNSET,
        post_cleanup_purge: bool | Unset = UNSET,
        cancel: Any | None = None,
        on_event: Any | None = None,
    ) -> Result:
        """Run a `Spec` or a `Plan`, and return what the launch did to every node.

        A launch option that is not given has the default of `plan`. A `Plan` brings its own
        options, so giving any option with a plan is an error (`plan_options`), also one equal to
        the plan's. Running a plan resolves its spec again under the locks of the launch. It
        raises `StalePlanError` when the graph, an identity, a run directory, a delivery, the
        settings, the commit of a Git dependency or an option of this workspace differs from the
        plan. A source edit is no difference: the launch checks freshness again.

        A node that fails, is rejected or is blocked is an outcome in the `Result`, not an
        exception. `prune_variants` and the questions about a delivery ask `confirm`. `post_cleanup`
        and `post_cleanup_purge` act on the nodes with the disposition `ran` only."""
        from .flow_runner import DefaultRunner
        from .flow_runner.error_codes import public_error
        from .proc_utils import TOOL_OUTPUT

        given = {
            "rebuild_all": rebuild_all,
            "clean": clean,
            "prune_variants": prune_variants,
            "outputs_to": outputs_to,
            "overwrite_outputs": overwrite_outputs,
            "post_cleanup": post_cleanup,
            "post_cleanup_purge": post_cleanup_purge,
        }
        named: dict[str, Any] = {
            name: value for name, value in given.items() if not isinstance(value, Unset)
        }
        expected: Plan | None = None
        if isinstance(what, Plan):
            if named:
                raise SpecError(
                    f"{', '.join(named)} given with a plan: a plan has its own launch options. "
                    "Pass them to `plan`.",
                    code="plan_options",
                )
            difference = workspace_difference(what._workspace, self._options())
            if difference is not None:
                raise StalePlanError(
                    f"The plan is stale: {difference}. Plan again.",
                    code="stale_plan",
                    difference=difference,
                )
            expected, spec = what, what.spec
            options = LaunchOptions(**what.options)
        else:
            spec = what
            options = LaunchOptions(**named)
        if self.remote is not None:
            raise NotImplementedError("running on a remote host is not supported yet")
        self._refuse_project_run_root(spec)
        runner = self._runner(DefaultRunner)
        if self.confirm is not None:
            answer = self.confirm
            runner.confirm_overwrite = lambda conflicts: answer(_overwrite_question(conflicts))
            runner.confirm_scrub = lambda candidates: answer(_prune_question(candidates))
        token = TOOL_OUTPUT.set(self.tool_output)
        try:
            return runner.run_spec(
                spec, local_settings=self.local_settings, options=options, expected=expected
            )
        except Exception as error:
            public = public_error(error)
            if public is error:
                raise
            raise public from error
        finally:
            TOOL_OUTPUT.reset(token)
