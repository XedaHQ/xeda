- A `flows` table that is not a mapping of flow names to mappings (`-s flows=3`,
  `-s flows.verilator=3`, `flows: []` in a file) is now an error that names the key. It was a
  traceback on the command line and was ignored in some files.
