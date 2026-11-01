import re
from pathlib import Path

import pytest

import xeda

pytestmark = pytest.mark.python_compat

DOCS = Path(__file__).resolve().parent.parent / "docs"


def test_the_version_is_pep_440():
    assert re.fullmatch(r"\d+\.\d+\.\d+([.+].*)?", xeda.__version__)


def test_the_stability_page_is_in_the_toctree():
    assert "stability" in (DOCS / "index.rst").read_text().split(":caption: Contents:")[1]
    assert (DOCS / "stability.rst").is_file()
