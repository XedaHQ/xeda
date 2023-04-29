estimate_parasitics -placement
report_metrics "resizer pre" false false

print_banner "instance_count"
puts [sta::network_leaf_instance_count]

print_banner "pin_count"
puts [sta::network_leaf_pin_count]

set_dont_use {{settings.dont_use_cells|join(" ")|embrace}}

# Do not buffer chip-level designs
{% if settings.footprint %}
puts "Perform port buffering..."
buffer_ports
{% endif %}

puts "Perform buffer insertion..."
repair_design

# Repair tie lo fanout
puts "Repair tie lo fanout..."
set tielo_cell_name {{platform.tielo_cell}}
set tielo_lib_name [get_name [get_property [lindex [get_lib_cell $tielo_cell_name] 0] library]]
set tielo_pin $tielo_lib_name/$tielo_cell_name/{{platform.tielo_port}}
repair_tie_fanout -separation {{settings.tie_separation}} $tielo_pin

# Repair tie hi fanout
puts "Repair tie hi fanout..."
set tiehi_cell_name {{platform.tiehi_cell}}
set tiehi_lib_name [get_name [get_property [lindex [get_lib_cell $tiehi_cell_name] 0] library]]
set tiehi_pin $tiehi_lib_name/$tiehi_cell_name/{{platform.tiehi_port}}
repair_tie_fanout -separation {{settings.tie_separation}} $tiehi_pin

# hold violations are not repaired until after CTS

print_banner "report_floating_nets"
report_floating_nets

report_metrics "resizer" true false

print_banner "instance_count"
puts [sta::network_leaf_instance_count]

print_banner "pin_count"
puts [sta::network_leaf_pin_count]

{{ write_checkpoint(step) }}
