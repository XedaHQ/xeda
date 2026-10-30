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

from .cancel import CancelToken
from .deliver import overwrite_question
from .errors import RecordError, RunRootError, SpecError, StalePlanError
from .events import Event
from .outcomes import Result
from .flow_runner.launch_options import LaunchOptions, WorkspaceOptions
from .flow_runner.plan_check import workspace_difference
from .local_settings import LocalSettings
from .plan import Plan, dependency_entries, plan_of
from .remote_value import Remote
from .records import RecordProblem, RunListing, RunRecord, read_record, read_stored_record
from .run_root import DEFAULT_RUN_ROOT, ensure_run_root
from .spec import Spec
from .types import UNSET, PathLike, Unset

__all__ = ["Workspace"]

TOOL_OUTPUTS = ("log", "terminal")

if TYPE_CHECKING:
    from .flow_runner.scrub import ScrubCandidate, ScrubResult


def _prune_question(candidates: "Sequence[ScrubCandidate]") -> str:
    lines = [f"Xeda would remove these {len(candidates)} run directories:"]
    for candidate in candidates:
        lines.append(f"  {candidate.path}")
        lines += [f"  {alias} (another name of the same directory)" for alias in candidate.aliases]
    return "\n".join([*lines, "Remove them?"])


def _removal_question(candidates: "Sequence[ScrubCandidate]") -> str:
    lines = [f"Xeda would remove these {len(candidates)} directories:"]
    for candidate in candidates:
        lines.append(f"  {candidate.path}")
        lines += [f"  {alias} (another name of the same directory)" for alias in candidate.aliases]
    return "\n".join([*lines, "Remove them?"])


def _checked_design_name(name: str) -> str:
    """Return `name`, or raise `RunDirectoryError` (the CLI's refusal) if it is no design name."""
    from .design import DESIGN_NAME
    from .run_dir import RunDirectoryError

    if not isinstance(name, str) or not DESIGN_NAME.fullmatch(name):
        raise RunDirectoryError(f"{name!r} is not a design name")
    return name


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
    a launch asks, and `None` answers "no". `remote` is the host that runs the launches (a
    `Remote`). Planning and running then happen on that host, and the run directories of a launch
    are in the mirror `<run root>/@<host>` here. `local_settings` are the private settings layers,
    as `local_settings()` reads them."""

    run_root: Path
    hashed_run_dirs: bool
    tool_output: Literal["log", "terminal"]
    confirm: Callable[[str], bool] | None
    remote: Remote | None
    local_settings: LocalSettings | None

    def __init__(
        self,
        run_root: PathLike | Unset = UNSET,
        *,
        hashed_run_dirs: bool = False,
        tool_output: Literal["log", "terminal"] = "log",
        confirm: Callable[[str], bool] | None = None,
        remote: Remote | None = None,
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
        if self.remote is not None:
            from .remote import client

            try:
                return client.plan_remote(spec, workspace=self._options(), options=options)
            except Exception as error:
                public = public_error(error)
                if public is error:
                    raise
                raise public from error
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
            dependencies=dependency_entries(planned.request.design),
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
        cancel: CancelToken | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> Result:
        """Run a `Spec` or a `Plan`, and return what the launch did to every node.

        A launch option that is not given has the default of `plan`. A `Plan` brings its own
        options, so giving any option with a plan is an error (`plan_options`), also one equal to
        the plan's. Running a plan takes the locks of its graph and resolves its spec again
        under them. It raises `StalePlanError` when the graph, an identity, a run directory, a
        delivery, the settings, the commit of a Git dependency or an option of this workspace
        differs from the plan. A stale plan prunes nothing, asks nothing, generates nothing and
        runs nothing. A source edit is no difference: the launch checks freshness again. With a
        `remote`, the host plans the spec again before the launch, and the same rule compares the
        two plans.

        A node that fails, is rejected or is blocked is an outcome in the `Result`, not an
        exception. `prune_variants` removes the other run directories of every node of the plan,
        producers included: those of the same design and target that the launch does not use.
        It and the questions about a delivery ask `confirm`. `post_cleanup` and
        `post_cleanup_purge` act on the nodes with the disposition `ran` only.

        `cancel` stops the launch when `cancel.canceled` becomes true: the launch stops what it
        waits for, generates or runs, releases its locks and returns a `Result` with the outcome
        `canceled`. `on_event` gets an `Event` for the start and the end of the launch and of each
        node, in the calling thread. It may reuse the locks that the launch holds and no other
        (`LeaseHeldError`). An exception that it raises cancels the launch, and `run` raises it
        again, unchanged, once the launch has released everything. A `KeyboardInterrupt` stops the
        tool's whole process group, releases the locks and propagates.

        A thread that holds a lock of Xeda (inside `on_event` or `confirm`) cannot start a launch:
        it raises `LeaseHeldError` before it plans."""
        from .flow_runner import DefaultRunner
        from .flow_runner.error_codes import public_error
        from .flow_runner.run_lock import refuse_if_held
        from .proc_utils import TOOL_OUTPUT

        refuse_if_held()

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
        self._refuse_project_run_root(spec)
        if self.remote is not None:
            return self._run_remote(spec, options, expected, cancel, on_event)
        runner = self._runner(DefaultRunner)
        if self.confirm is not None:
            answer = self.confirm
            runner.confirm_overwrite = lambda conflicts: answer(overwrite_question(conflicts))
            runner.confirm_scrub = lambda candidates: answer(_prune_question(candidates))
        from_callback: list[Exception] = []

        def relay(event: Event) -> None:
            assert on_event is not None
            try:
                on_event(event)
            except Exception as error:
                from_callback.append(error)
                raise

        token = TOOL_OUTPUT.set(self.tool_output)
        try:
            return runner.run_spec(
                spec,
                local_settings=self.local_settings,
                options=options,
                expected=expected,
                cancel=cancel,
                on_event=None if on_event is None else relay,
            )
        except Exception as error:
            public = public_error(error)
            if public is error or error in from_callback:
                raise
            raise public from error
        finally:
            TOOL_OUTPUT.reset(token)

    def _run_remote(
        self,
        spec: Spec,
        options: LaunchOptions,
        expected: Plan | None,
        cancel: CancelToken | None,
        on_event: Callable[[Event], None] | None,
    ) -> Result:
        """Run `spec` on the remote host. With the plan `expected`, the host plans `spec` again
        first, and a difference raises `StalePlanError` before any launch starts."""
        from .flow_runner.error_codes import plain_message, public_error
        from .flow_runner.plan_check import plan_difference
        from .proc_utils import TOOL_OUTPUT
        from .remote import client

        from_callback: list[Exception] = []

        def relay(event: Event) -> None:
            assert on_event is not None
            try:
                on_event(event)
            except Exception as error:
                from_callback.append(error)
                raise

        token = TOOL_OUTPUT.set(self.tool_output)
        try:
            if expected is not None:
                cause: Exception | None = None
                difference: str | None
                try:
                    fresh = client.plan_remote(spec, workspace=self._options(), options=options)
                except Exception as error:
                    # A request that the host refuses now is a difference. Any other failure (a
                    # host that cannot be reached) is no statement about the plan.
                    if not isinstance(public_error(error), SpecError):
                        raise
                    difference = f"planning is refused now: {plain_message(error)}"
                    cause = error
                else:
                    difference = plan_difference(expected, fresh)
                if difference is not None:
                    raise StalePlanError(
                        f"The plan is stale: {difference}. Plan again.",
                        code="stale_plan",
                        difference=difference,
                    ) from cause
            return client.run_remote(
                spec,
                workspace=self._options(),
                options=options,
                cancel=cancel,
                on_event=None if on_event is None else relay,
            )
        except Exception as error:
            public = public_error(error)
            if public is error or error in from_callback:
                raise
            raise public from error
        finally:
            TOOL_OUTPUT.reset(token)

    def load(self, run_dir: PathLike, /) -> RunRecord:
        """Return the record that the run directory `run_dir` holds now.

        The run root of the record follows from where the directory is: a run directory is
        `<run root>/<design>[/<target>]/<flow>`, and the record names its design and target. It
        reads one file and takes no lock. It may return a later run than the one that you had
        in mind: compare `run_id`. Raise `RecordError` with the code `missing_record`,
        `corrupt_record` or `unsupported_schema`."""
        return read_stored_record(Path(run_dir))

    def records(
        self,
        *,
        design_name: str | Unset = UNSET,
        flow: str | Unset = UNSET,
        target: str | None | Unset = UNSET,
    ) -> RunListing:
        """Return the records of the run directories in the run root, sorted by run directory.

        A selection that is not given matches everything: every design, every flow, every target.
        `target=None` selects the run directories that no target holds. A run directory whose
        record cannot be read is no error: it is a `RecordProblem` of the listing, with the code
        `missing_record`, `corrupt_record` or `unsupported_schema`. The listing reads the disk,
        takes no lock and imports no flow, so a `flow` that is no flow selects nothing. Raise
        `RunRootError` when the run root cannot be read."""
        from .flow_runner.error_codes import public_error
        from .flow_runner.scrub import list_run_directories

        try:
            directories = list_run_directories(
                self.run_root,
                design_name=(
                    None if isinstance(design_name, Unset) else _checked_design_name(design_name)
                ),
                flow=None if isinstance(flow, Unset) else flow,
                target=target,
            )
        except OSError as error:
            raise RunRootError(
                f"the run root {self.run_root} cannot be read: {error}",
                code="invalid_run_root",
                path=self.run_root,
            ) from error
        except Exception as error:
            raise public_error(error) from error
        found: list[RunRecord] = []
        problems: list[RecordProblem] = []
        for directory in directories:
            try:
                found.append(read_record(directory, run_root=self.run_root))
            except RecordError as error:
                problems.append(RecordProblem(error.path, error.code, error.message))
        return RunListing(tuple(found), tuple(problems))

    def scrub(
        self,
        *,
        design_name: str,
        flow: str | Unset = UNSET,
        target: str | None | Unset = UNSET,
    ) -> "ScrubResult":
        """Remove the run directories of a design, after `confirm` said yes.

        Without `flow`, the run directories of every flow go. Without `flow` and `target`, so do
        the generation storages of the design (`<run root>/.generated/<design>/`), which every
        target reads. With `flow`, only the run directories of that flow go. `target` selects as
        in `records`. The disk decides what exists: no design
        file is read. One question lists every directory. Without `confirm` the question answers
        "no": nothing is removed, the log says so, and the result lists what was scanned. Each
        directory goes under its exclusive lock, which waits for every launch and every open
        record that holds it. A directory whose records changed after the listing is `kept`, and
        one that is gone when its turn comes is `gone`. A link that is named like a run directory
        but leads elsewhere is `skipped`, and is not followed.

        A `flow` that was removed from Xeda is a name too: the run directories that it left are
        removed. Raise `FlowNotFoundError` for a `flow` that is no flow, `RunRootError` for a link where the
        run directories or the storages lie, and `LeaseHeldError` in a thread that holds a lock of
        Xeda (a callback of `run`)."""
        from .flow_runner import default_runner
        from .flow_runner.error_codes import public_error
        from .flow_runner.run_lock import refuse_if_held
        from .flow_runner.scrub import scrub_design
        from .flow_runner.settings_layers import FlowRemovedError

        refuse_if_held()
        if not isinstance(flow, Unset):
            try:
                default_runner.get_flow_class(flow)
            except FlowRemovedError as removed:
                # The flow is gone and its run directories are not, and a scrub removes directories.
                flow = removed.flow_name or flow
        answer = self.confirm
        try:
            return scrub_design(
                None if isinstance(flow, Unset) else flow,
                self.run_root / _checked_design_name(design_name),
                run_root=self.run_root,
                target=target if isinstance(target, str) else None,
                design_only=target is None,
                confirm=None if answer is None else lambda found: answer(_removal_question(found)),
            )
        except Exception as error:
            public = public_error(error)
            if public is error:
                raise
            raise public from error
