# Notebooks

Each numbered notebook shows one part of the Python API. The test suite runs them
(`tests/test_notebooks.py`), so they stay correct.

## Run a notebook

Install the notebook dependencies and start Jupyter from this directory:

```bash
uv sync --group notebooks
uv run jupyter lab
```

The first cell of a notebook finds the examples. It reads the `XEDA_EXAMPLES` environment
variable. When the variable is not set, it uses the `examples` directory next to this one. Set it
to run a notebook from a copy of the examples:

```bash
XEDA_EXAMPLES=/path/to/examples uv run jupyter lab
```

A notebook writes only under `notebook-output/` in its working directory: the run root, the
tables it saves and the files it delivers. Git ignores that directory. Delete it to start over.

## Commit a notebook

Commit a notebook without outputs and without execution counts. The test suite fails on a tracked
notebook that has them. To clear them, run:

```bash
uv run jupyter nbconvert --clear-output --inplace notebooks/NN-name.ipynb
```

The tools that a notebook needs (for example `ghdl` or `yosys`) must be installed to run it. The
test for a notebook is skipped when a tool is missing.
