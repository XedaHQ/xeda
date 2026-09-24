"""What a model writes out must describe what it actually holds.

pydantic v2 changed two defaults that silently narrow serialized output, and both feed things
xeda depends on: `settings.json`, the run hashes that drive `--cached-dependencies`, and the
design document the remote runner ships over SSH.
"""

import json
import zipfile
from pathlib import Path

import pytest

from xeda.dataclass import model_with_allow_extra
from xeda.design import Design, DesignReference
from xeda.utils import dump_json, semantic_hash

RTL = {"sources": [], "top": "t"}
GIT_URI = "git+https://github.com/u/r.git?branch=main#sub/d.toml"


# ---------------------------------------------------------------------------------------------
# Polymorphic nested models: v2 serializes by the *annotated* type unless told otherwise.
# ---------------------------------------------------------------------------------------------


def test_git_dependency_keeps_its_own_fields_when_dumped():
    """`dependencies` is `List[DesignReference]`, but the entries are `GitReference`s.

    Without `SerializeAsAny`, `model_dump()` emitted only `{"uri": ...}` and `send_design()`
    shipped a dependency the far side could not clone.
    """
    reference = DesignReference.from_data(GIT_URI)
    design = Design.model_construct(name="d", dependencies=[reference])

    dumped = design.model_dump()["dependencies"][0]

    assert dumped["repo_url"] == "https://github.com/u/r.git"
    assert dumped["design_file"] == "sub/d.toml"
    assert dumped["branch"] == "main"


def test_git_dependency_survives_json_round_trip():
    reference = DesignReference.from_data(GIT_URI)
    design = Design.model_construct(name="d", dependencies=[reference])

    payload = json.loads(design.model_dump_json())

    assert payload["dependencies"][0]["repo_url"] == "https://github.com/u/r.git"


# ---------------------------------------------------------------------------------------------
# Extras: v2 keeps them in __pydantic_extra__, which `__dict__` does not see.
# ---------------------------------------------------------------------------------------------


def _with_extra(value):
    return model_with_allow_extra(Design)(name="d", rtl=RTL, extra_key=value)


def test_extras_reach_the_persisted_settings_json(tmp_path):
    design = _with_extra("SENTINEL")
    path = tmp_path / "settings.json"

    dump_json({"design": design}, path, backup=False)

    assert "SENTINEL" in path.read_text()


def test_extras_change_the_semantic_hash():
    """Two designs differing only in an extra key hashed identically, so a cached run
    directory could be reused for a different design."""
    assert semantic_hash(_with_extra("A")) != semantic_hash(_with_extra("B"))


def test_semantic_hash_is_stable_for_equal_models():
    assert semantic_hash(_with_extra("A")) == semantic_hash(_with_extra("A"))


def test_model_state_exposes_both_fields_and_extras():
    from xeda.utils import model_state

    state = model_state(_with_extra("A"))
    assert state["name"] == "d"
    assert state["extra_key"] == "A"


def test_remote_design_archive_preserves_source_compilation_metadata(tmp_path):
    from fabric import Connection

    from xeda.flow_runner.remote import send_design

    for directory, content in (("one", "-- first\n"), ("two", "-- second\n")):
        source_dir = tmp_path / directory
        source_dir.mkdir()
        (source_dir / "shared.src").write_text(content)
    (tmp_path / "test.py").write_text("def test_dut(): pass\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={
            "sources": [
                {"file": "one/shared.src", "type": "Vhdl", "standard": "2008"},
                {
                    "file": "two/shared.src",
                    "type": "Vhdl",
                    "standard": "1993",
                    "variant": "legacy",
                },
            ],
            "top": "top",
        },
        tb={"sources": ["test.py"], "cocotb": {"module": "test"}},
    )
    captured = {}
    connection = Connection("unused")

    def capture_archive(local, remote):
        with zipfile.ZipFile(local) as archive:
            captured["names"] = archive.namelist()
            design_name = next(name for name in archive.namelist() if name.endswith(".xeda.json"))
            captured["design"] = json.loads(archive.read(design_name))

    connection.put = capture_archive
    send_design(
        design,
        connection,
        "/unused",
        all_flows_settings={"yosys_fpga": {"flatten": False}},
    )

    rtl_sources = captured["design"]["rtl"]["sources"]
    assert [source["standard"] for source in rtl_sources] == ["2008", "1993"]
    assert [source["type"] for source in rtl_sources] == ["Vhdl", "Vhdl"]
    assert rtl_sources[1]["variant"] == "legacy"
    assert rtl_sources[0]["file"] != rtl_sources[1]["file"]
    assert all(source["file"] in captured["names"] for source in rtl_sources)
    assert captured["design"]["tb"]["cocotb"]["module"] == "test"
    assert captured["design"]["flow"] == {"yosys_fpga": {"flatten": False}}


# ---------------------------------------------------------------------------------------------
# DesignReference input forms
# ---------------------------------------------------------------------------------------------


def test_uri_form_is_parsed_into_its_parts():
    reference = DesignReference.from_data(GIT_URI)
    assert (reference.repo_url, reference.design_file, reference.branch) == (
        "https://github.com/u/r.git",
        "sub/d.toml",
        "main",
    )


def test_git_uri_mapping_is_not_modified_in_place():
    data = {"uri": GIT_URI}

    DesignReference.from_data(data)

    assert data == {"uri": GIT_URI}


def test_existing_design_reference_is_accepted():
    reference = DesignReference(uri="some/local.toml")

    assert DesignReference.from_data(reference) is reference


def test_non_string_git_dependency_uri_is_a_validation_error():
    with pytest.raises(ValueError, match="URI must be a string"):
        DesignReference.from_data(
            {"uri": 123, "repo_url": "https://example.com/x.git", "design_file": "x.toml"}
        )


def test_mapping_form_with_repo_url_is_accepted():
    """`from_data()` routes a `repo_url` mapping to `GitReference`, but `validate_repo` built
    `dict(repo_url=..., **values)` and died with a duplicate-keyword `TypeError`."""
    reference = DesignReference.from_data(
        {"repo_url": "https://example.com/x.git", "design_file": "x.toml"}
    )
    assert reference.repo_url == "https://example.com/x.git"
    assert reference.design_file == "x.toml"


def test_mapping_form_keeps_explicit_branch_and_derives_a_uri():
    reference = DesignReference.from_data(
        {"repo_url": "https://example.com/x.git", "design_file": "x.toml", "branch": "dev"}
    )
    assert reference.branch == "dev"
    assert reference.uri == "https://example.com/x.git?branch=dev#x.toml"


def test_git_dependency_without_a_locator_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DesignReference.from_data({"repo_url": None, "design_file": "x.toml"})


def test_plain_local_reference_is_not_a_git_reference():
    reference = DesignReference.from_data("some/local.toml")
    assert type(reference) is DesignReference
    assert reference.uri == "some/local.toml"


# ---------------------------------------------------------------------------------------------
# Arbitrary types: a source serializes through the schema its field declares.
# ---------------------------------------------------------------------------------------------


def test_json_keeps_what_a_source_cannot_be_rebuilt_without(tmp_path):
    """A source written to JSON has to load back as the same source.

    The serializer emitted a bare path, which drops a stated `type`/`standard`/`variant` -- all
    part of the design's identity -- and turns an unchecked `{ path = ... }` source into a
    checked one, so reloading failed on a file the design never promised was there. Worse, the
    dump asked for duck-typed serialization, which skips the schema a field declares, and an
    arbitrary type like `DesignSource` declares its serializer nowhere else: every
    `model_dump_json()` of a design with sources raised `PydanticSerializationError`.
    """
    for name in ("top.vhd", "legacy.v", "old.vhd"):
        (tmp_path / name).write_text(f"-- {name}\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={
            "sources": [
                "top.vhd",
                {"file": "legacy.v", "type": "SystemVerilog"},
                {"file": "old.vhd", "standard": "93"},
                {"path": "generated/later.vhd"},
            ],
            "top": "top",
        },
    )

    sources = json.loads(design.model_dump_json())["rtl"]["sources"]

    assert isinstance(sources[0], str), "a plain source needs no table"
    assert sources[1]["type"] == "SystemVerilog"
    assert sources[2]["standard"] == "93"
    assert set(sources[3]) == {"path"}, "an unchecked source must not come back as a checked one"

    again = Design(**json.loads(design.model_dump_json()))
    assert [str(s.type) for s in again.rtl.sources] == [str(s.type) for s in design.rtl.sources]
    assert [s.standard for s in again.rtl.sources] == [s.standard for s in design.rtl.sources]


def test_recording_a_design_is_a_fixed_point(tmp_path):
    """`settings.json` records a design so it can be rebuilt: reloading the record gives the
    same design (the same hash), and recording *that* gives the same document back.

    None of the example designs uses what this depends on most -- stated compile metadata, an
    unchecked `{ path = ... }` source, file-valued parameters in `rtl` and `tb` -- so they are
    all here, written in the spellings a design file uses.
    """
    (tmp_path / "rtl").mkdir()
    (tmp_path / "gen").mkdir()  # `gen/later.v`: unchecked, but written by the time it is hashed
    for name in (
        "rtl/top.vhd",
        "rtl/legacy.v",
        "rtl/core.bsv",
        "gen/later.v",
        "tb.py",
        "rom.mem",
        "init.mem",
    ):
        (tmp_path / name).write_text(f"-- {name}\n")
    design = Design(
        name="sink",
        design_root=tmp_path,
        language={"vhdl": {"standard": "2008"}},
        rtl={
            "sources": [
                "$DESIGN_ROOT/rtl/top.vhd",
                {"file": "rtl/legacy.v", "type": "SystemVerilog"},
                {"file": "rtl/core.bsv", "variant": "bsv"},
                {"path": "gen/later.v", "type": "Verilog"},
            ],
            "top": "top",
            "clock": {"port": "clk"},
            "parameters": {"ROM": {"file": "rom.mem"}, "LOG": {"path": "out/log.txt"}, "W": 8},
            "defines": {"SYNTH": True},
        },
        tb={
            "sources": ["tb.py"],
            "cocotb": True,
            "parameters": {"INIT": {"file": "$DESIGN_ROOT/init.mem"}},
        },
    )

    first = tmp_path / "first.json"
    dump_json({"design": design}, first, backup=False)
    reloaded = Design(**json.loads(first.read_text())["design"])
    second = tmp_path / "second.json"
    dump_json({"design": reloaded}, second, backup=False)

    assert (reloaded.rtl_hash, reloaded.tb_hash) == (design.rtl_hash, design.tb_hash)
    assert json.loads(second.read_text()) == json.loads(first.read_text())
    recorded_sources = json.loads(first.read_text())["design"]["rtl"]["sources"]
    assert set(recorded_sources[-1]) == {"path", "type"}, "still unchecked, still Verilog"
    # A file-valued parameter is recorded as the one value the tool sees, and counted relative
    # to the design root, however it was spelled.
    assert reloaded.rtl.parameters == design.rtl.parameters
    assert reloaded.rtl_fingerprint["parameters"] == {
        "ROM": "$DESIGN_ROOT/rom.mem",
        "LOG": "$DESIGN_ROOT/out/log.txt",
        "W": 8,
    }
    assert reloaded.tb_fingerprint["parameters"] == {"INIT": "$DESIGN_ROOT/init.mem"}


def test_remote_design_archive_carries_file_valued_parameters(tmp_path):
    """A parameter given as a file has to travel like a source.

    Its value is an absolute path on the *sending* machine, and it used to be shipped as that,
    so the remote handed its tool a path to nothing. An input (`{ file = ... }`) is archived at
    its place under the design root and repointed there; an output (`{ path = ... }`) is no file
    to ship,
    only a place for the remote to write. Both travel as tables, so the remote resolves them
    against its own design root rather than against whatever directory it runs in.

    Repointing must not change what the design *is*: a file under the design root keeps its
    place relative to the root, so the remote hashes the run exactly as this side did.
    """
    from fabric import Connection

    from xeda.flow_runner.remote import send_design

    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.vhd").write_text("-- top\n")
    (tmp_path / "rom.mem").write_text("00 11\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={
            "sources": ["rtl/top.vhd"],
            "top": "top",
            "parameters": {
                "ROM": {"file": "rom.mem"},
                "DUMP": {"path": "out/trace.vcd"},
                "WIDTH": 8,
            },
        },
    )

    archive_bytes = {}
    connection = Connection("unused")
    connection.put = lambda local, remote: archive_bytes.setdefault("b", Path(local).read_bytes())
    send_design(design, connection, "/unused")

    # What the remote does with what it received.
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    (remote_root / "design.zip").write_bytes(archive_bytes["b"])
    with zipfile.ZipFile(remote_root / "design.zip") as archive:
        archive.extractall(remote_root)
    remote = Design.from_file(remote_root / "d.xeda.json")

    rom = Path(remote.rtl.parameters["ROM"])
    assert rom.is_absolute() and rom.read_text() == "00 11\n", "the file travelled with the design"
    assert rom.is_relative_to(remote_root), "and landed inside the remote's own tree"

    dump = Path(remote.rtl.parameters["DUMP"])
    assert dump == remote_root / "out" / "trace.vcd", "an output is a place, not a file to ship"
    assert not dump.exists()

    assert remote.rtl.parameters["WIDTH"] == 8
    assert (remote.rtl_hash, remote.tb_hash) == (design.rtl_hash, design.tb_hash)


def test_remote_design_archive_ships_a_parameter_file_from_outside_the_root(tmp_path):
    """A parameter is re-rooted from its value alone: a file outside the design root travels
    among the sources, while text that names no file on this side -- a plain value, or an
    absolute path elsewhere with nothing there -- travels exactly as it is."""
    from fabric import Connection

    from xeda.flow_runner.remote import send_design

    root = tmp_path / "design"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.vhd").write_text("-- top\n")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "rom.mem").write_text("22 33\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["rtl/top.vhd"],
            "top": "top",
            "parameters": {
                "ROM": {"file": "../shared/rom.mem"},
                "PREFIX": "/nonexistent/prefix",
                "NAME": "abc",
            },
        },
    )

    archive_bytes = {}
    connection = Connection("unused")
    connection.put = lambda local, remote: archive_bytes.setdefault("b", Path(local).read_bytes())
    send_design(design, connection, "/unused")
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    (remote_root / "design.zip").write_bytes(archive_bytes["b"])
    with zipfile.ZipFile(remote_root / "design.zip") as archive:
        archive.extractall(remote_root)
    remote = Design.from_file(remote_root / "d.xeda.json")

    rom = Path(remote.rtl.parameters["ROM"])
    assert rom.is_relative_to(remote_root) and rom.read_text() == "22 33\n"
    assert remote.rtl.parameters["PREFIX"] == "/nonexistent/prefix"
    assert remote.rtl.parameters["NAME"] == "abc"


# ---------------------------------------------------------------------------------------------
# An object's attributes are not a serialization format
# ---------------------------------------------------------------------------------------------


def test_nothing_is_written_by_reading_its_dict():
    """Writing `__dict__` is what put a private `_specified_path` into `settings.json`, and on a
    plain `Enum` -- whose `__dict__` carries `__objclass__` -- descending into it does not even
    terminate. An enum is written as its value, as pydantic writes one; an object with a JSON
    form says so with `as_json_value()`; anything else is written as its text, which is legible
    and cannot recurse."""
    from enum import Enum

    from xeda.utils import json_encodable

    class Plain(Enum):
        RED = 1

    class Opaque:
        def __init__(self):
            self.private_ = "not for settings.json"

        def __str__(self):
            return "opaque"

    assert json_encodable(Plain.RED) == 1
    assert json_encodable(Opaque()) == "opaque"
    assert "private_" not in json.dumps(Opaque(), default=json_encodable)


def test_a_value_printed_under_json_is_the_value_xeda_writes_to_a_file(tmp_path):
    """`--json` documents are built by `introspect.json_safe`, files by `utils.json_encodable`.
    Two encoders with their own rules agreed only on values that carry no serializer of their
    own: `json_safe` dumped a model in python mode, which leaves a source as the object for
    `str()` to flatten -- dropping the `{ path = ... }` table and every stated `type` -- where
    the file kept them. So the oracle is built from exactly such values."""
    from enum import Enum

    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import FlowOutcome
    from xeda.introspect import json_safe
    from xeda.utils import json_encodable

    class Plain(Enum):
        RED = 1

    (tmp_path / "legacy.v").write_text("module legacy; endmodule\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={
            "sources": [{"file": "legacy.v", "type": "SystemVerilog"}, {"path": "gen/later.v"}],
            "top": "legacy",
        },
    )
    values = {
        "design": design,
        "source": design.rtl.sources[0],
        "unchecked_source": design.rtl.sources[1],
        "settings": Flow.Settings(verbose=2),
        "outcome": FlowOutcome(
            settings=Flow.Settings(),
            results=Flow.Results(success=True),
            timestamp=None,
            run_path=tmp_path,
        ),
        "path": tmp_path / "x",
        "enum": Plain.RED,
        "nested": [(1, {"k": Plain.RED}), {tmp_path}],
    }
    for name, value in values.items():
        written = json.loads(json.dumps(value, default=json_encodable))
        assert json_safe(value) == written, name
    assert json_safe(design.rtl.sources[1]) == {"path": str(tmp_path / "gen" / "later.v")}
    # ... and a library caller's `model_dump_json()` is that same document
    assert json.loads(design.model_dump_json()) == json_safe(design)


def test_a_dse_outcome_is_one_view_wherever_it_is_written(tmp_path):
    """A search writes its best outcome to `best.json` as it improves, and the CLI prints the
    same outcome in its `--json` document. Both used to build that view by hand -- one of them
    by dumping the outcome's `__dict__`, which left `run_path` as a `Path` for the encoder to
    stringify by luck rather than by rule."""
    from xeda.cli import _dse_best_document
    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import FlowOutcome

    outcome = FlowOutcome(
        settings=Flow.Settings(),
        results=Flow.Results(success=True, Fmax=123.4),
        timestamp="2026-01-01",
        run_path=tmp_path / "run",
    )

    written = tmp_path / "best.json"
    dump_json({"best": outcome}, written, backup=False)
    from_file = json.loads(written.read_text())["best"]

    assert from_file == _dse_best_document(outcome)
    assert from_file["run_path"] == str(tmp_path / "run"), "a path is written as text"
    assert from_file["results"]["Fmax"] == 123.4
    assert from_file["settings"]["timeout_seconds"] == Flow.Settings().timeout_seconds


# ---------------------------------------------------------------------------------------------
# A DSE outcome is built in a worker process and has to arrive intact
# ---------------------------------------------------------------------------------------------


def _outcome_built_in_a_worker(_):
    """Built in the child, exactly as the pool-mapped `Optimizer.__call__` builds it."""
    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import FlowOutcome

    return FlowOutcome(
        settings=Flow.Settings(verbose=3),
        results=Flow.Results(success=True, Fmax=456.7),
        timestamp="from-the-worker",
        run_path=Path("/tmp/child-run"),
    )


def test_a_dse_outcome_survives_the_process_it_was_built_in():
    """`Dse` runs its flows in a `pebble.ProcessPool`: the worker builds the `FlowOutcome` and
    `pool.map` carries it back, so one that cannot be pickled -- or that loses a field on the
    way -- breaks every design-space exploration.

    `FlowOutcome` was declared `@define(slots=False)` to leave an instance `__dict__` behind for
    a serializer to read. It needs neither: `attrs` generates the pickle protocol for a slotted
    class, and JSON goes through `as_json_value()`. Both halves are pinned here, because the
    `__dict__` is what a JSON encoder silently fell back on.
    """
    from pebble.pool.process import ProcessPool

    from xeda.flow_runner.dse.dse_runner import FlowOutcome

    with ProcessPool(max_workers=1) as pool:
        (outcome,) = list(pool.map(_outcome_built_in_a_worker, [0]).result())

    assert isinstance(outcome, FlowOutcome)
    assert not hasattr(outcome, "__dict__"), "an outcome carries no instance dict to serialize"
    assert outcome.timestamp == "from-the-worker"
    assert outcome.run_path == Path("/tmp/child-run")
    assert (outcome.results.Fmax, outcome.results.success) == (456.7, True)
    assert outcome.settings.verbose == 3, "a pydantic settings model crossed with it"


def test_a_dse_outcome_pickles_without_losing_anything(tmp_path):
    """The transport above reduces to pickle; this is the same requirement without the pool, so
    a failure says whether the object or the pool is at fault."""
    import pickle

    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import FlowOutcome

    outcome = FlowOutcome(
        settings=Flow.Settings(verbose=2),
        results=Flow.Results(success=True, Fmax=123.4),
        timestamp="2026-01-01",
        run_path=tmp_path / "run",
    )

    assert pickle.loads(pickle.dumps(outcome)).as_json_value() == outcome.as_json_value()


def test_a_printed_mapping_has_the_keys_xeda_writes_to_a_file():
    """`json` writes a key by its own rules -- `True` as `true`, `None` as `null`, a float by
    its repr, a `str` enum member as its text -- while `json_safe` wrote `str(key)`: `"True"`,
    `"None"`, `"E.A"`. So a mapping printed under `--json` and the same mapping in a file
    disagreed on every such key."""
    from enum import Enum, IntEnum

    import yaml

    from xeda.introspect import json_safe
    from xeda.utils import json_encodable

    class Letter(str, Enum):
        A = "a"

    class Number(IntEnum):
        ONE = 1

    values = {
        "bool/None/float keys": {True: 1, False: 2, None: 3, 1.5: 4, 2: 5},
        "str-enum key": {Letter.A: 1},
        "int-enum key": {Number.ONE: 1},
        "nested": [{"k": {None: {True: Letter.A}}}],
        "int-enum value": {"n": Number.ONE},
    }
    for name, value in values.items():
        written = json.loads(json.dumps(value, default=json_encodable))
        assert json_safe(value) == written, name
        # plain data, as `json` reads it back: `--format yaml` renders it without knowing enums
        yaml.safe_dump(json_safe(value))


def test_a_generator_keeps_its_own_fields_when_dumped(tmp_path):
    """`RtlSettings.generator` may hold a `ChiselGenerator`, and v2 dumps a nested model by its
    *annotated* type unless told otherwise (`SerializeAsAny`): its `main`, `project` and
    `build_system` were silently dropped. A `Design` runs its generator and drops it while it is
    built, so this is the one place that holds one."""
    from xeda.design import ChiselGenerator, RtlSettings

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    rtl = RtlSettings(
        sources=[str(tmp_path / "top.v")],
        top="top",
        generator=ChiselGenerator(main="Top", project="core", build_system="mill"),
    )
    dumped = rtl.model_dump(mode="json")["generator"]
    assert (dumped["main"], dumped["project"], dumped["build_system"]) == ("Top", "core", "mill")


def test_no_key_turns_a_document_into_an_error(tmp_path):
    """`json` accepts only `str`, numbers, `bool` and `None` as keys, and raises on anything
    else before any encoder hook sees it. A `Path` or tuple key -- one a flow's results or a
    tool report could well hold -- made writing `results.json` raise, and once the printed
    document was made to agree with the file, `--json` gave an error document instead of the
    results. A key is written by the rule a value is: its text, an enum its value."""
    from enum import Enum

    from xeda.flow import Flow
    from xeda.introspect import json_safe

    class Mode(Enum):
        FAST = 1

    results = Flow.Results(success=True)
    results["paths"] = {Path("/run/a.v"): 1}
    value = {
        "path key": {Path("/run/a.v"): 1},
        "tuple key": {("clk", "rise"): 1.5},
        "enum key": {Mode.FAST: "f"},
        "in a list": [{("a", 1): None}],
        "in results": results,
        "text and path alike": {"/x": 1, Path("/x"): 2},
        "path then text": {Path("/x"): 2, "/x": 1},
        "number and text": {1: "numeric", "1": "text"},
    }
    written = tmp_path / "doc.json"
    dump_json(value, written, backup=False)
    from_file = json.loads(written.read_text())

    assert json_safe(value) == from_file
    assert from_file["path key"] == {"/run/a.v": 1}
    assert from_file["tuple key"] == {"('clk', 'rise')": 1.5}
    assert from_file["enum key"] == {"1": "f"}
    assert from_file["in results"]["paths"] == {"/run/a.v": 1}
    assert sorted(from_file["text and path alike"].values()) == [1, 2], "neither value is lost"
    assert sorted(from_file["path then text"].values()) == [1, 2]
    assert sorted(from_file["number and text"].values()) == ["numeric", "text"]


def test_a_run_whose_results_have_unusual_keys_still_reports_them(tmp_path, monkeypatch):
    """What an agent sees: `xeda run --json` prints the results, and `results.json` holds them,
    whatever keys a flow reports them under."""
    from typing import ClassVar

    from click.testing import CliRunner

    from xeda.cli import cli
    from xeda.flow import Flow, registered_flows

    class OddKeys(Flow):
        """Reports timing by (clock, edge)."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            self.results["timing"] = {("clk", "rise"): 1.5, Path("/run/x.v"): 2}

        def parse_reports(self) -> bool:
            return True

    try:
        (tmp_path / "top.vhd").write_text("entity top is end;\n")
        (tmp_path / "d.toml").write_text('name = "d"\n[rtl]\nsources = ["top.vhd"]\ntop = "top"\n')
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["run", OddKeys.name, "d.toml", "--json"])
        document = json.loads(result.stdout)
        assert document["success"] is True, document
        assert document["results"]["timing"] == {"('clk', 'rise')": 1.5, "/run/x.v": 2}
        recorded = json.loads((Path(document["run_path"]) / "results.json").read_text())
        assert recorded["timing"] == document["results"]["timing"]
    finally:
        for name in (OddKeys.name, OddKeys.__name__):
            registered_flows.pop(name, None)
