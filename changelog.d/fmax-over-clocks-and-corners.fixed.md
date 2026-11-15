- Quartus, Vivado and OpenROAD report `fmax` as the lowest value over the clocks and, for
  Quartus, the slow timing models that the reports list. Quartus reports no `fmax` when a clock has
  no value in one model, and keys `fmax_by_clock` by clock name only.
