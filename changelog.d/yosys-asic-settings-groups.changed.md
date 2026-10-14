- `yosys` maps to a standard-cell library in the order of OpenROAD-flow-scripts, passes
  `dont_use_cells` as `-dont_use`, and passes `-D` to `abc` only with `abc.delay_target` (`auto`
  gives the clock period). Its ASIC settings moved into groups such as `abc.strategy`; the old
  names say where they went.
