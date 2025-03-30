{% for clock_name, clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{"%.03f" % clock.period}} -name {{clock_name}} [get_ports {{clock.port}}]

set_output_delay -clock {{clock_name}} -max {{"%.03f" % (clock.period / 2.0)}} [get_ports  -filter {@port_direction == out}]
set_output_delay -clock {{clock_name}} -min 0 [get_ports  -filter {@port_direction == out}]
{% endif -%}
{% endfor -%}

# set_dont_touch_network [find port $CLOCK_PORT]




###################################################################
#                 Area Constraints
###################################################################
set MAX_AREA 0.0

