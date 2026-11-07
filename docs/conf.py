# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
# import os
# import sys
# sys.path.insert(0, os.path.abspath("."))

import re
from importlib.metadata import version as release_version

# -- Project information -----------------------------------------------------

project = "xeda"
copyright = "2022, Kamyar Mohajerani"
author = "Kamyar Mohajerani"
version = release_version("xeda")

master_doc = "index"
language = "en"


# -- General configuration ---------------------------------------------------

# The pages are hand-written reStructuredText that uses only core Sphinx and the theme, so no
# extension that supplies a directive or a role is enabled. Enable one only together with a page
# that uses it (and its entry in docs/requirements.txt): an extension nothing uses is one more
# thing that can stop the build. opengraph is the exception the rule has to name: it needs no
# markup, adding the link-preview metadata (og:title, og:description, og:url) to every page it
# builds, so dropping it would silently change the published output rather than remove dead weight.
extensions: list[str] = ["sphinxext.opengraph", "sphinx.ext.autodoc"]

# What og:url is made relative to. Read the Docs serves the canonical documentation; a build
# somewhere else (tox -e docs, the CI docs job) renders the same absolute URLs, which no one reads.
ogp_site_url = "https://xeda.readthedocs.io/en/latest/"

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = "sphinx_book_theme"
html_logo = "../xeda.svg"
# html_favicon = "../xeda.svg"
html_title = ""
html_theme_options = {
    "github_url": "https://github.com/XedaHQ/xeda",
    "repository_url": "https://github.com/XedaHQ/xeda",
    "use_edit_page_button": True,
    "repository_branch": "main",
    "path_to_docs": "docs",
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = []

# -- Python reference ------------------------------------------------------------------------

# The reference documents the public names of `xeda`, `xeda.errors` and `xeda.introspect` (see
# docs/api/). Its annotations name standard-library types, and the build has no intersphinx and
# no network, so those names have no link target. Only standard-library names (and the optional
# pandas `DataFrame`) are ignored here: a missing target for a Xeda name must fail the build.
autodoc_typehints = "signature"
autodoc_member_order = "bysource"
# A class has the docstring that it gives, not the one of a base class (pydantic's own text).
autodoc_inherit_docstrings = False
# The type variable of `Design.load`, `Design.with_updates` and `Design.with_target` (`self` and
# the result have the type of the design that the caller has).
autodoc_type_aliases = {"DesignType": "Design"}
nitpick_ignore_regex = [
    ("py:class", r"(pathlib|datetime|os|collections\.abc)\..*"),
    ("py:class", r"(Path|datetime)"),
    ("py:class", r"pandas\.DataFrame"),
    # Sphinx splits `tuple[Path, ...]` at the comma (`LocalSettings.path`).
    ("py:class", r"tuple\[~pathlib\.Path"),
]


# The names of two aliases of the design module, with what they stand for. The attributes of
# `Testbench` and `Rtl` show the right side.
_ALIASES = {"DefineType": "Any", "Tuple012": "Tuple[str, ...]"}


def _top_level_parts(text: str) -> list[str]:
    """Split `text` at the commas that are outside every bracket."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += char in "([{" and 1 or char in ")]}" and -1 or 0
        if char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    return [*parts, text[start:]]


def _without_annotated(text: str) -> str:
    """Replace each `Annotated[T, metadata]` by `T`: the metadata of a field (its freezing, its
    recipe) is machinery, and its text names internal classes and memory addresses."""
    while match := re.search(r"(~typing\.)?Annotated\[", text):
        depth, index = 1, match.end()
        while depth:
            depth += text[index] in "([{" and 1 or text[index] in ")]}" and -1 or 0
            index += 1
        inner = _top_level_parts(text[match.end() : index - 1])[0]
        text = text[: match.start()] + inner.strip() + text[index:]
    return text


def _clean_signature(app, what, name, obj, options, signature, return_annotation):
    """Show a class by its public parameters, and an annotation without its metadata. The event
    comes before autodoc reads the attributes of a class, so the annotations are made plain here
    (this process only builds the documentation)."""
    if what == "class":
        for klass in obj.__mro__[:-1]:
            notes = klass.__dict__.get("__annotations__", {})
            for key, note in notes.items():
                if isinstance(note, str):
                    note = _without_annotated(note)
                    for alias, text in _ALIASES.items():
                        note = re.sub(rf"\b{alias}\b", text, note)
                    notes[key] = note
    if signature is None:
        return None
    parts = _top_level_parts(signature[1:-1])
    kept = [part for part in parts if not part.strip().startswith("_")]
    return "(" + ",".join(_without_annotated(part) for part in kept) + ")", return_annotation


def setup(app):
    app.connect("autodoc-process-signature", _clean_signature)
