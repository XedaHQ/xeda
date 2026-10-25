- Simulation times, delays and other timing settings now take a value with a unit, for example
  `timeout: 2 min`, `stop_time: 100 us` or `freq: 6 MHz`. A bare number is refused, and the message
  names the setting and suggests your number in its unit (`180` gives `180 s` for `timeout`).
