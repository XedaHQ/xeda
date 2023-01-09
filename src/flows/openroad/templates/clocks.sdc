{% set all_clock_ports = settings.clocks.values()|map(attribute="port") %}

set all_input_ports [all_inputs]
set all_output_ports [all_outputs]
set all_clock_ports [list {{all_clock_ports|join(" ")}}]

{% for clock_name, clock in settings.clocks.items() %}
{% if clock.port %}
set clk_port [get_ports {{clock.port}}]
create_clock -period {{ "%.3f"|format(clock.period) }} -name {{clock_name}} $clk_port

## NOTE: includes other clock ports if multiple clocks are present
set non_clock_inputs [lsearch -inline -all -not -exact $all_input_ports $clk_port]

set_input_delay  [expr {{"%.3f"|format(settings.input_delay)}} * {{"%.3f"|format(clock.period)}}] -clock {{clock_name}} $non_clock_inputs 
set_output_delay [expr {{"%.3f"|format(settings.output_delay)}} * {{"%.3f"|format(clock.period)}}] -clock {{clock_name}} $all_output_ports
{% endif %}
{% endfor %}