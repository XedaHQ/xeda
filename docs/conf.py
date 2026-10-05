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
extensions: list[str] = ["sphinxext.opengraph"]

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
