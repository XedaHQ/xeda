- `examples/requirements.txt` was removed: the Python packages of the example testbenches are the
  dependency group `examples` of `pyproject.toml`, which `uv sync` or `pip install --group examples`
  installs.
