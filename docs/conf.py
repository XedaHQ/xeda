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

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named "sphinx.ext.*") or your custom
# ones.
extensions = [
    # "sphinxcontrib.bibtex",
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_panels",
    "sphinxext.rediraffe",
    "sphinxcontrib.mermaid",
    "sphinxext.opengraph",
    "sphinxcontrib.autodoc_pydantic",
    "sphinx.ext.autosummary",
    "sphinx.ext.autodoc"
]

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
html_logo = "../logo.svg"
# html_favicon = "../logo.svg"
html_title = ""
html_theme_options = {
    "github_url": "https://github.com/XedaHQ/xeda",
    "repository_url": "https://github.com/XedaHQ/xeda",
    "use_edit_page_button": True,
    "repository_branch": "dev",
    "path_to_docs": "docs",
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ["_static"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3.9", None),
    "pydantic": ("https://www.sphinx-doc.org/en/master", None),
}


autosummary_generate = True
# autodoc_member_order = "bysource"
