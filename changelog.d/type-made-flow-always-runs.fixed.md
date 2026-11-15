- A flow class made with `type()` now always runs, as Xeda cannot find its code. Before, its code
  digest covered the file of the `abc` module of Python instead of its own code.
