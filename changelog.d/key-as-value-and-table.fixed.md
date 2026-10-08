- A key given as a value and as a table (`-s timing=true timing.x=1`, in either order, or in a
  design file, a target or `--design-overrides`) is now an error that names both keys. It was a
  traceback, or silently lost the table.
