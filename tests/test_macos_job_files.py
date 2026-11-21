"""A test file that creates symbolic links runs in the `macos` job.

Links, locks, process groups and the file system of macOS differ from Linux. The job named
"Run the operating-system-sensitive tests" in `.github/workflows/ci.yml` lists the test files that
run there. A file that calls `symlink_to` (or `os.symlink`) is in that list, or in
`NOT_IN_THE_MACOS_JOB`. That table lists the files that were not moved yet, and it only shrinks.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).parent
WORKFLOW = TESTS.parent / ".github" / "workflows" / "ci.yml"
STEP = "Run the operating-system-sensitive tests"
LINKS = re.compile(r"\bsymlink_to\(|\bos\.symlink\(")

#: The test files that create links and are not in the `macos` job yet. Remove a row when the file
#: joins the job. Never add one: a new file that creates links goes into the job.
NOT_IN_THE_MACOS_JOB = {
    "test_artifacts.py",
    "test_checkout_guard.py",
    "test_cocotb.py",
    "test_code_digest.py",
    "test_declared_launch.py",
    "test_delivery_source_entries.py",
    "test_docker_input_mounts.py",
    "test_dry_run.py",
    "test_fpga_fakes.py",
    "test_fpga_pack.py",
    "test_launch_run_dir_policy.py",
    "test_module_verbs.py",
    "test_openroad_final.py",
    "test_output_records.py",
    "test_plan_deliveries.py",
    "test_project.py",
    "test_quiet_library.py",
    "test_remote_environment.py",
    "test_remote_generation.py",
    "test_remote_packaging.py",
    "test_run_identity.py",
    "test_sby.py",
    "test_sim_evidence.py",
    "test_target_paths.py",
    "test_tool_cwd.py",
    "test_tool_siblings.py",
    "test_trace.py",
    "test_vcs_sim_evidence.py",
    "test_verilator.py",
    "test_verilator_run_directory.py",
    "test_xilinx_chipdb.py",
    "test_yosys_asic_tcl.py",
    "test_yosys_recipe.py",
    "test_yosys_temporary_files.py",
}


def _macos_job_files() -> set[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text[text.index(STEP) :]
    end = step.find("\n      - name:", 1)
    return set(re.findall(r"tests/(test_\w+\.py)", step if end < 0 else step[:end]))


def _files_with_links() -> set[str]:
    return {
        path.name
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name and LINKS.search(path.read_text(encoding="utf-8"))
    }


def test_the_macos_job_lists_existing_files():
    listed = _macos_job_files()
    assert listed, "the step of the macos job was not found"
    assert not [name for name in listed if not (TESTS / name).is_file()]


def test_every_test_file_that_creates_links_is_in_the_macos_job():
    missing = _files_with_links() - _macos_job_files() - NOT_IN_THE_MACOS_JOB
    assert not missing, f"add these files to the `macos` job in ci.yml: {sorted(missing)}"


def test_the_table_of_files_not_in_the_macos_job_only_shrinks():
    stale = NOT_IN_THE_MACOS_JOB - _files_with_links()
    joined = NOT_IN_THE_MACOS_JOB & _macos_job_files()
    assert not stale, f"these files create no links any more: remove the rows {sorted(stale)}"
    assert not joined, f"these files are in the macos job now: remove the rows {sorted(joined)}"
