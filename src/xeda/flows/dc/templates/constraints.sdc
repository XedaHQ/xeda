{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]

{% if settings.default_max_input_delay is not none -%}
set max_input_delay {{settings.default_max_input_delay}}
set input_ports [all_inputs -exclude_clock_ports]
foreach_in_collection port $input_ports {
    set port_name [get_object_name $port]
    puts "Settings MAX input delay of port $port_name to $max_input_delay"
    set_input_delay -clock {{clock_name}} -max $max_input_delay $input_ports
}
{% endif -%}

{% if settings.default_max_output_delay is not none -%}
# Ensuring valid output can be captured on the clock edge
set max_output_delay {{settings.default_max_output_delay}}
set output_ports [all_outputs]
foreach_in_collection port $output_ports {
    puts "Settings MAX output delay of port $port_name to $max_output_delay"
    set_output_delay -clock {{clock_name}} -max $max_output_delay $output_ports
}
{% endif -%}
# set_dont_touch_network [find port {{clock.port}}]
{% endif -%}
{% endfor -%}
