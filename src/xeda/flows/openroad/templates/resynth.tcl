{% if settings.resynth_for_timing %}
repair_design
repair_timing
# pre restructure area/timing report (ideal clocks)
puts "Post synth-opt area"
report_design_area
report_worst_slack -min -digits 3
puts "Post synth-opt wns"
report_worst_slack -max -digits 3
puts "Post synth-opt tns"
report_tns -digits 3

if {![info exists save_checkpoint] || $save_checkpoint} {
    write_verilog {{settings.results_dir}}/{{step_id}}_pre_abc_timing.v
}

restructure -target timing -liberty_file {{merged_lib_file}} \
    -work_dir {{settings.results_dir}}

if {![info exists save_checkpoint] || $save_checkpoint} {
    write_verilog {{settings.results_dir}}/{{step_id}}_post_abc_timing.v
}

# post restructure area/timing report (ideal clocks)
remove_buffers
repair_design
repair_timing

puts "Post restructure-opt wns"
report_worst_slack -max -digits 3
puts "Post restructure-opt tns"
report_tns -digits 3

# remove buffers inserted by optimization
remove_buffers
{% endif %}

{% if settings.resynth_for_area %}
set num_instances [llength [get_cells -hier *]]
puts "number instances before restructure is $num_instances"
puts "Design Area before restructure"
report_design_area
report_design_area_metrics

if {![info exists save_checkpoint] || $save_checkpoint} {
    write_verilog {{settings.results_dir}}/{{step_id}}_pre_abc.v
}

set tielo_cell_name {{platform.tielo_cell}}
set tielo_lib_name [get_name [get_property [lindex [get_lib_cell $tielo_cell_name] 0] library]]
set tielo_port $tielo_lib_name/$tielo_cell_name/{{platform.tielo_port}}

set tiehi_cell_name {{platform.tiehi_cell}}
set tiehi_lib_name [get_name [get_property [lindex [get_lib_cell $tiehi_cell_name] 0] library]]
set tiehi_port $tiehi_lib_name/$tiehi_cell_name/{{platform.tiehi_port}}

restructure -liberty_file {{merged_lib_file}} -target "area" \
    -tiehi_port $tiehi_port \
    -tielo_port $tielo_port \
    -work_dir {{settings.results_dir}}

# remove buffers inserted by abc
remove_buffers

if {![info exists save_checkpoint] || $save_checkpoint} {
    write_verilog {{settings.results_dir}}/{{step_id}}_post_abc.v
}
set num_instances [llength [get_cells -hier *]]
puts "number instances after restructure is $num_instances"
puts "Design Area after restructure"
report_design_area
report_design_area_metrics
{% endif %}

report_metrics "after resynth" false false

{{ write_checkpoint(step) }}
