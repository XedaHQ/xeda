from pathlib import Path

import pytest
from packaging.version import Version

import xeda

pytestmark = pytest.mark.python_compat

DOCS = Path(__file__).resolve().parent.parent / "docs"


def test_the_version_is_pep_440():
    assert str(Version(xeda.__version__)) == xeda.__version__


def test_the_stability_page_is_in_the_toctree():
    assert (
        "stability"
        in (DOCS / "index.rst").read_text(encoding="utf-8").split(":caption: Contents:")[1]
    )
    assert (DOCS / "stability.rst").is_file()
