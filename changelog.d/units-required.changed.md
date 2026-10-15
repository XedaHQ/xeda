- Clock times and frequencies, and the `yosys` options `abc.delay_target`, `abc.max_slew` and
  `abc.load`, need a unit, such as `clock.period: 5 ns`, `abc.load: 3.9 fF` or
  `-s clock.freq=200MHz`. A bare number is refused, and `xeda list-settings` shows the kind of such
  a setting (`time`) as its type.
