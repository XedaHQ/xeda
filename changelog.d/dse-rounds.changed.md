- `max_failed_rounds` and `max_failed_rounds_with_best` now stop an exploration after exactly
  that many rounds without a passed point, not one more. `max_luts: 0` allows no LUT. The option
  `--optimizer` takes only a built-in optimizer name.
