"""What a model writes out must describe what it actually holds.

pydantic v2 changed two defaults that silently narrow serialized output, and both feed things
xeda depends on: `settings.json`, the run hashes that drive `--cached-dependencies`, and the
design document the remote runner ships over SSH.
"""

import json

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

    dumped = design.model_dump(exclude_unset=False)["dependencies"][0]

    assert dumped["repo_url"] == "https://github.com/u/r.git"
    assert dumped["design_file"] == "sub/d.toml"
    assert dumped["branch"] == "main"


def test_git_dependency_survives_json_round_trip():
    reference = DesignReference.from_data(GIT_URI)
    design = Design.model_construct(name="d", dependencies=[reference])

    payload = json.loads(design.model_dump_json(exclude_unset=False))

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
