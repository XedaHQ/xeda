# Notebooks

Each numbered notebook shows one part of the Python API. The test suite runs them
(`tests/test_notebooks.py`), so they stay correct.

## The notebooks

- `01-quickstart.ipynb`: load a design, simulate, synthesize, read the results and the files of a
  run, and run again to reuse them.
- `02-simulation.ipynb`: the ways a simulation can end: pass, reject, fail, time limit and cancel.
- `03-sweep.ipynb`: vary a design parameter and a setting in one call, and read past runs.
- `04-plan.ipynb`: see what a launch will do, inspect a node, run the plan, and get a stale plan
  and settings errors.
- `05-generated.ipynb`: compare variants of a design that a generator writes.
- `06-qor-history.ipynb`: read the records of past runs, build a table of settings and metrics, and
  plot a metric over time.

## Run a notebook

The `notebooks` dependency group has the packages that the notebooks import. It does not have
JupyterLab, so add it for the run. Start JupyterLab from this directory:

```bash
uv run --group notebooks --with jupyterlab jupyter lab
```

You can also open a notebook in an editor that runs Jupyter notebooks, such as VS Code. Select
the Python environment of `uv sync --group notebooks` as its kernel.

The first cell of a notebook finds the examples. It reads the `XEDA_EXAMPLES` environment
variable. When the variable is not set, it uses the `examples` directory next to this one. Set it
to run a notebook from a copy of the examples:

```bash
XEDA_EXAMPLES=/path/to/examples uv run --group notebooks --with jupyterlab jupyter lab
```

A notebook writes only under `notebook-output/` in its working directory: the run root, the
tables it saves and the files it delivers. Git ignores that directory. Delete it to start over.

## Commit a notebook

Commit a notebook without outputs and without execution counts. The test suite fails on a tracked
notebook that has them. To clear them, run:

```bash
uv run --with nbconvert jupyter nbconvert --clear-output --inplace notebooks/NN-name.ipynb
```

The tools that a notebook needs (for example `ghdl` or `yosys`) must be installed to run it. The
test for a notebook is skipped when a tool is missing.
