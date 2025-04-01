{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]

set min_input_delay 0
set input_ports [all_inputs -exclude_clock_ports]
set num_input_ports [llength $input_ports]
puts "Settings min input delay of $min_input_delay on $num_input_ports input ports: $input_ports"
if {$num_input_ports == 0} {
    puts "Warning: No input ports found, skipping input delay setting."
} else {
    set_input_delay -clock {{clock_name}} -min $min_input_delay $input_ports
}

set max_output_delay {{"%.03f" % (clock.period / 2.0)}}
set min_output_delay 0
set output_ports [all_outputs]
set num_output_ports [llength $output_ports]
puts "Settings output delay of max:$max_output_delay min:$min_output_delay on $num_output_ports output ports: $output_ports"
if {$num_output_ports == 0} {
    puts "Warning: No output ports found, skipping output delay setting."
} else {
    set_output_delay -clock {{clock_name}} -max $max_output_delay $output_ports 
    set_output_delay -clock {{clock_name}} -min $min_output_delay $output_ports
}

# set_dont_touch_network [find port $CLOCK_PORT]
{% endif -%}
{% endfor -%}

