- On the command line, a whole number is digits with an optional `-`, and a number must be finite:
  `-s seed=1_0` and `-s ratio=nan` are refused. A setting that also takes text gets a number only
  when the number reads back as the same text, so `010` and `1e3` stay text.
