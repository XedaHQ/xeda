import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from xml.etree import ElementTree

from .dataclass import Field, XedaBaseModel, deliverable, field_validator
from .design import Design, SourceType
from .tool import Tool

log = logging.getLogger(__name__)

#: Children of a `<testcase>` that describe its outcome. Everything else there (the
#: `<properties>` block, captured `<system-out>` / `<system-err>`) is metadata.
_OUTCOME_TAGS = ("FAILURE", "ERROR", "SKIPPED")


def _property_value(element: ElementTree.Element, name: str) -> Optional[str]:
    """Value of a `<property name=...>` directly under `element`, or nested in `<properties>`."""
    for prop in (*element.findall("property"), *element.findall("properties/property")):
        if prop.get("name") == name:
            return prop.get("value")
    return None


#: Nanoseconds per unit of cocotb's `sim_time_unit`. Spelled out rather than run through `pint`,
#: which resolves "ns" to both nanosecond and nanosiemens.
_SIM_TIME_UNIT_NS = {
    "fs": 1e-6,
    "ps": 1e-3,
    "ns": 1.0,
    "us": 1e3,
    "ms": 1e6,
    "sec": 1e9,
    "s": 1e9,
}


class CocotbSettings(XedaBaseModel):
    coverage: bool = Field(
        False, description="Collect coverage data if supported by simulation tool."
    )
    reduced_log_fmt: bool = Field(True, description="Display shorter log lines in the terminal.")
    results_xml: Path = Field(
        Path("results.xml"),
        description="xUnit-compatible cocotb result file.",
        json_schema_extra={"hidden_from_schema": True, **deliverable()},
    )
    resolve_x: Literal["VALUE_ERROR", "ZEROS", "ONES", "RANDOM"] = Field(
        "VALUE_ERROR",
        description="how to resolve bits with a value of X, Z, U or W when being converted to integer.",
    )
    testcase: List[str] = Field(
        [],
        description="Names of the cocotb tests to run; all of them if empty. A name selects exactly "
        "the test of that name (`check` never selects `foo_check`); on cocotb 2.x it may be "
        "qualified by its module (`tb.check`). Can also be given as a comma-separated string.",
    )
    random_seed: Union[int, Literal["random"]] = Field(
        1,
        description="Seed of the Python random module (`COCOTB_RANDOM_SEED`): the same seed "
        'recreates the same test stimulus. "random" lets cocotb draw a new seed on every run, '
        "so the flow then always runs and is never reused.",
    )
    gpi_extra: List[str] = Field(
        [],
        description="A comma-separated list of extra libraries that are dynamically loaded at runtime.",
    )

    @field_validator("random_seed", mode="before")
    @classmethod
    def reject_null_random_seed(cls, value):
        if value is None:
            raise ValueError(
                '`random_seed: null` was removed: use "random" for a new seed on every run, '
                "or an integer"
            )
        return value

    @field_validator("testcase", "gpi_extra", mode="before")
    @classmethod
    def str_to_list(cls, value):
        if isinstance(value, str):
            value = [s.strip() for s in value.split(",")]
        return value


@dataclass
class TestCase:
    name: str
    classname: str
    file: str
    lineno: str
    time: float
    sim_time_ns: Optional[float]
    ratio_time: float
    status: str


@dataclass
class TestSuite:
    random_seed: int
    test_cases: list["TestCase"]
    errors: int
    failures: int
    skipped: int
    total_sim_time_ns: float


@dataclass
class TestResults:
    tests: int
    errors: int
    failures: int
    skipped: int
    time: float
    total_sim_time_ns: float
    test_suites: List[TestSuite]

    @property
    def success(self):
        return not self.errors and not self.failures

    @staticmethod
    def from_test_suites(test_suites: List[TestSuite]) -> "TestResults":
        tests = 0
        errors = 0
        failures = 0
        skipped = 0
        time = 0.0
        total_sim_time_ns = 0.0
        for ts in test_suites:
            tests += len(ts.test_cases)
            errors += ts.errors
            failures += ts.failures
            skipped += ts.skipped
            time += sum(tc.time for tc in ts.test_cases)
            total_sim_time_ns += ts.total_sim_time_ns
        return TestResults(
            tests, errors, failures, skipped, time, total_sim_time_ns, test_suites=test_suites
        )

    @staticmethod
    def _properties(testcase: ElementTree.Element) -> Dict[str, str]:
        """`<property name= value=>` pairs of one testcase.

        From cocotb 2.0 the per-test metadata (seed, source location, simulated time) moved from
        attributes on `<testcase>` into this standard JUnit block. Reading only the attributes,
        as this parser used to, silently yielded defaults for every one of them.
        """
        return {
            prop.get("name", ""): prop.get("value", "")
            for prop in testcase.findall("properties/property")
        }

    @staticmethod
    def _sim_time_ns(testcase: ElementTree.Element, properties: Dict[str, str]) -> Optional[float]:
        """Simulated time of one testcase, in nanoseconds, or `None` if it was not reported.

        cocotb 2.x records `sim_time_duration` alongside a `sim_time_unit`; 1.x wrote a
        `sim_time_ns` (or `sim_time_ps`) attribute. Both are read so a results file from either
        generation is understood.
        """
        duration = properties.get("sim_time_duration")
        if duration is not None:
            scale = _SIM_TIME_UNIT_NS.get(properties.get("sim_time_unit", "ns").strip().lower())
            if scale is not None:
                try:
                    return float(duration) * scale
                except ValueError:
                    return None
        for attribute, scale in (("sim_time_ns", 1.0), ("sim_time_ps", 1e-3)):
            value = testcase.get(attribute)
            if value is not None:
                try:
                    return float(value) * scale
                except ValueError:
                    return None
        return None

    @staticmethod
    def parse_results(results_xml_file) -> "TestResults":
        tree = ElementTree.parse(results_xml_file)
        results = []
        for ts in tree.iter("testsuite"):
            # The seed has lived in three places across cocotb versions: a `<testsuite>`
            # attribute, a `<property>` directly under the suite, and (from 2.0) a property of
            # each testcase. Try them in that order rather than reporting -1.
            random_seed = ts.get("random_seed") or _property_value(ts, "random_seed")
            test_cases = []
            num_errors = 0
            num_failures = 0
            num_skipped = 0
            total_sim_time_ns = 0.0
            for tc in ts.iter("testcase"):
                properties = TestResults._properties(tc)
                if random_seed is None:
                    random_seed = properties.get("random_seed")
                sim_time_ns = TestResults._sim_time_ns(tc, properties)
                time_s = float(tc.get("time") or 0)
                # Only these children say anything about the outcome: cocotb 2.x also writes a
                # `<properties>` block and may attach captured output, and counting every child
                # tag made a passing test report its status as "PROPERTIES".
                outcome = [e.tag.upper() for e in tc if e.tag.upper() in _OUTCOME_TAGS]
                test_cases.append(
                    TestCase(
                        name=tc.get("name", "???"),
                        classname=tc.get("classname", "???"),
                        file=tc.get("file") or properties.get("file", "???"),
                        lineno=tc.get("lineno") or properties.get("line", "???"),
                        time=time_s,
                        sim_time_ns=sim_time_ns,
                        ratio_time=(sim_time_ns / time_s) if sim_time_ns and time_s > 0 else 0,
                        status=", ".join(outcome) or "PASSED",
                    )
                )
                num_errors += outcome.count("ERROR")
                num_failures += outcome.count("FAILURE")
                num_skipped += outcome.count("SKIPPED")
                total_sim_time_ns += sim_time_ns or 0.0
            results.append(
                TestSuite(
                    random_seed=int(random_seed or "-1"),
                    test_cases=test_cases,
                    errors=num_errors,
                    failures=num_failures,
                    skipped=num_skipped,
                    total_sim_time_ns=total_sim_time_ns,
                )
            )
        return TestResults.from_test_suites(results)


def cocotb_toplevel(design: Design) -> str | None:
    """The HDL module cocotb drives (its `TOPLEVEL`): the testbench's `tb.cocotb.toplevel`, or
    else the design's `rtl.top`. A simulator that names its top makes it this module."""
    return (design.tb.cocotb.toplevel if design.tb.cocotb else None) or design.rtl.top


def cocotb_verdict(
    results: "TestResults", flow_results: dict[str, Any], prefix: str = "cocotb."
) -> bool:
    """Record cocotb's counts in `flow_results`; pass only if at least one test ran (skipped
    tests do not count) and none failed or errored."""
    flow_results[prefix + "tests"] = results.tests
    flow_results[prefix + "errors"] = results.errors
    flow_results[prefix + "failures"] = results.failures
    flow_results[prefix + "skipped"] = results.skipped
    flow_results[prefix + "time"] = results.time
    flow_results[prefix + "sim_time_ns"] = results.total_sim_time_ns
    if results.errors:
        log.error("Cocotb: %d error(s)", results.errors)
        return False
    if results.failures:
        log.critical("Cocotb: %d failure(s)", results.failures)
        return False
    if results.tests - results.skipped <= 0:
        log.error(
            "Cocotb ran no test: %d found, %d skipped (does `testcase` name one?).",
            results.tests,
            results.skipped,
        )
        return False
    return True


class Cocotb(CocotbSettings, Tool):
    """Cocotb support for a SimFlow"""

    executable: str = "cocotb-config"
    sim_name: str

    """Not a stand-alone tool, but is used from a SimFlow"""

    @cached_property
    def prefix(self) -> Optional[str]:
        return self.run_get_stdout("--prefix")

    @cached_property
    def share_dir(self) -> Optional[str]:
        return self.run_get_stdout("--share")

    @cached_property
    def lib_dir(self) -> Optional[str]:
        if self.version_gte(1, 6):
            return self.run_get_stdout("--lib-dir")
        else:
            if self.prefix is None:
                return None
            return os.path.join(
                self.prefix,
                "cocotb",
                "libs",
            )

    @cached_property
    def vpi_lib_name(self) -> Optional[str]:
        return self.get_lib_name(self.sim_name)

    def get_lib_name(self, simulator, interface="vpi") -> Optional[str]:
        return self.run_get_stdout("--lib_name", interface, simulator)

    def lib_path(self, interface: str = "vpi", sim_name=None) -> Optional[str]:
        so_ext = "so"  # TODO windows?
        if sim_name is None:
            sim_name = self.sim_name
        if self.version_gte(1, 6):
            so_path = self.run_get_stdout(
                "--lib-name-path",
                interface,
                sim_name,
            )
            if not so_path:
                log.error("[cocotb] %s failed!", self.executable)
                return None
        else:
            if self.prefix is None:
                so_path = None
            else:
                so_path = os.path.join(
                    self.prefix,
                    "cocotb",
                    "libs",
                    f"libcocotb{interface}_{sim_name}.{so_ext}",
                )
        log.info("cocotb.lib_path: %s", so_path)
        if so_path:
            if not Path(so_path).exists():
                log.error("[cocotb] shared library %s does not exist.", so_path)
                return None
        else:
            log.error("[cocotb] %s library for %s is not available.", interface.upper(), sim_name)
        return so_path

    @cached_property
    def pygpi_entry_point(self) -> Optional[str]:
        """cocotb's PYGPI entry point, or `None` on releases that do not expose one.

        `cocotb-config --pygpi-entry-point` was added in cocotb 2.1; older releases reject the
        flag, which is how this tells the two apart.
        """
        return self.run_get_stdout("--pygpi-entry-point", raise_on_error=False)

    def gpi_users(self) -> Optional[str]:
        """Value for `GPI_USERS`, or `None` when the installed cocotb does not need it.

        Up to cocotb 2.0 the GPI library found its Python entry point by itself. From 2.1 it
        exits with "No GPI_USERS specified" unless told explicitly, so the entry point has to be
        passed in. This mirrors `cocotb_tools.runner`: libpython first, then the PYGPI entry
        point, separated by ";".
        """
        preset = os.environ.get("GPI_USERS")
        if preset:
            return preset
        entry_point = self.pygpi_entry_point
        if not entry_point:
            return None  # cocotb < 2.1: the interface library registers itself
        libpython = os.environ.get("LIBPYTHON_LOC") or self.run_get_stdout(
            "--libpython", raise_on_error=False
        )
        return ";".join(user for user in (libpython, entry_point) if user)

    def discard_results(self) -> None:
        """Remove the results file an earlier simulation left, before a new one starts.

        cocotb writes no results file when it runs no test at all (a test module that fails to
        import), and the simulators still exit 0. A file left by an earlier run in a reused run
        directory would then be read as this run's results.
        """
        if self._run_directory is not None:
            # in the run directory of the flow that simulates, whatever the current directory:
            # only inside it (`RunDirectory.remove` names a path that leads out), and nothing in
            # one no launcher chose
            self._run_directory.remove(self.results_xml)
        self.__dict__.pop("results", None)  # the cached `results` of that file

    def test_selection(self, testcases: List[str]) -> Dict[str, str]:
        """The environment that makes the installed cocotb run exactly the tests named
        `testcases`, or none at all to run every test.

        cocotb 1.x reads their comma-separated names from `TESTCASE` and runs the tests of exactly
        those names. From 2.0 cocotb ignores `TESTCASE` and reads a regular expression from
        `COCOTB_TEST_FILTER` (or names from `COCOTB_TESTCASE`, deprecated in the same release),
        which it searches in each test's `<module>.<name>`. The expression built here matches a
        whole name, optionally qualified by its module (`check` or `tb.check`, never
        `foo_check`) -- unlike `cocotb_tools.runner`'s, which also matches every test whose name
        ends with it. The installed cocotb's version, as `cocotb-config` reports it, tells the two
        apart: `--help-vars` cannot, since cocotb 2.0.0 reads `COCOTB_TEST_FILTER` without listing
        it. An unreadable version counts as the newest.
        """
        names = [name.strip() for name in testcases if name.strip()]
        if not names:
            return {}
        if not self.version_gte(2):
            return {"TESTCASE": ",".join(names)}
        alternatives = "|".join(re.escape(name) for name in names)
        return {"COCOTB_TEST_FILTER": rf"(?:^|\.)(?:{alternatives})$"}

    def pycache_prefix(self) -> Path:
        """Where the Python the simulator embeds caches bytecode: in the run directory.

        By default Python writes a module's cache into a `__pycache__` beside it, and the
        testbench is imported from the design's directory (`PYTHONPATH` above; pytest's assertion
        rewriting compiles it), so without this every cocotb run wrote into the user's tree."""
        run_dir = self._run_directory.path if self._run_directory is not None else Path.cwd()
        return run_dir / "__pycache__"

    def env(self, design: Design) -> Dict[str, Any]:
        """The environment a simulator needs to run the design's cocotb testbench.

        Every cocotb flow asks for it right before it simulates, which makes this the one place
        all of them go through: it also discards the results of an earlier run
        (`discard_results`), so that `add_results` reads only what this simulation wrote.
        """
        environ: Dict[str, Any] = dict()
        if design.tb.cocotb:
            self.discard_results()
            if design.tb is None or not design.tb.sources:
                raise ValueError("'design.tb.cocotb' is set, but 'design.tb.sources' is empty.")
            if not design.tb.top:
                if not design.rtl.top:
                    raise ValueError(
                        f"[cocotb] In design {design.name}: Either `tb.top` or `rtl.top` must be specified."
                    )
                design.tb.top = (design.rtl.top,)
            cocotb_sources = design.sources_of_type(SourceType.Cocotb, rtl=False, tb=True)
            if cocotb_sources:
                top_cocotb_source = cocotb_sources[-1].file
            else:
                top_cocotb_source = None

            py_path = []
            current_py_path = os.environ.get("PYTHONPATH")
            if current_py_path:
                py_path += current_py_path.split(os.pathsep)
            coco_module = None
            if design.tb.cocotb.module:
                design_module_split = design.tb.cocotb.module.split("/")
                coco_module = design_module_split[-1]
                if len(design_module_split) > 1:
                    module_path = Path(os.sep.join(design_module_split[:-1]))
                    if not os.path.isabs(module_path):
                        module_path = design.root_path / module_path
                else:
                    module_path = design.root_path
                py_path.append(str(module_path))
            elif top_cocotb_source:
                coco_module = top_cocotb_source.stem
            if top_cocotb_source:
                py_path.append(str(top_cocotb_source.parent))
            toplevel = cocotb_toplevel(design)
            environ = {
                "MODULE": coco_module,
                "COCOTB_TEST_MODULES": coco_module,
                "TOPLEVEL": toplevel,
                "COCOTB_TOPLEVEL": toplevel,
                "COCOTB_REDUCED_LOG_FMT": os.environ.get(
                    "COCOTB_REDUCED_LOG_FMT", "1" if self.reduced_log_fmt else "0"
                ),
                "PYTHONPATH": os.pathsep.join(py_path),
                "PYTHONPYCACHEPREFIX": str(self.pycache_prefix()),
                "COCOTB_RESULTS_FILE": str(self.results_xml),
                "COCOTB_RESOLVE_X": self.resolve_x,
                "PYGPI_PYTHON_BIN": os.environ.get(
                    # Use the current Python executable if not set in the environment
                    "PYGPI_PYTHON_BIN",
                    os.path.abspath(sys.executable),
                ),
            }
            if self.coverage:
                environ["COVERAGE"] = 1
            environ.update(self.test_selection(self.testcase or design.tb.cocotb.testcase))
            if self.random_seed != "random":
                environ["COCOTB_RANDOM_SEED"] = str(self.random_seed)
            if self.gpi_extra:
                environ["GPI_EXTRA"] = ",".join(self.gpi_extra)
            gpi_users = self.gpi_users()
            if gpi_users:
                environ["GPI_USERS"] = gpi_users
            log.debug("Cocotb env: %s", environ)
        return environ

    @cached_property
    def results(self) -> Optional[TestResults]:
        results_xml = Path(self.results_xml)
        if not results_xml.exists():
            return None
        return TestResults.parse_results(results_xml)

    def add_results(
        self,
        flow_results: Dict[str, Any],
        prefix: str = "cocotb.",
        results_file: Union[Path, None, Literal["current directory"]] = "current directory",
    ) -> bool:
        """adds cocotb results to parent flow's results. returns success status

        `results_file` is the results file to read -- a flow passes the one its run wrote
        (`Flow.report_file`), None if there is none; by default, `results_xml` in the current
        directory, whoever wrote it."""
        flow_results["success"] = False
        try:
            if results_file == "current directory":
                results = self.results
            elif results_file is None:
                results = None
            else:
                results = TestResults.parse_results(results_file)
        except (OSError, ElementTree.ParseError, UnicodeError, ValueError) as exc:
            where = self.results_xml if results_file == "current directory" else results_file
            log.error("Could not read cocotb test results from %s: %s", where, exc)
            return False
        if results is not None:
            flow_results["success"] = cocotb_verdict(results, flow_results, prefix)
            return flow_results["success"]
        else:
            log.error("No tests results were found.")
            return False
