create_clock -period {{flow.clock_period|round(3,'floor')}} -name clock [get_ports {{design.rtl.clock_port}}]

set_input_delay  -clock clock {{input_delay}} [filter [all_inputs] {NAME != {{design.rtl.clock_port}} } ]
set_output_delay -clock clock {{output_delay}} [all_outputs]
