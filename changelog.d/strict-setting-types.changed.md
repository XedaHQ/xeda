- A setting takes only the type it declares from Python and from files: an `int` setting refuses
  `"4"`, `4.0` and `True`, and a boolean refuses `"true"`. The command line still writes
  `-s seed=4`.
