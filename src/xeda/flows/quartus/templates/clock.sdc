{% for clock_name,clock in settings.clocks.items() -%}
{% if clock.port -%}
# create_clock -period {{ "%.3f"|format(clock.period) }} -name {{clock_name|tcl_word}} [get_ports {{clock.port|tcl_word}}]
create_clock -period {{clock.period|round(3,'floor')}} -name {{clock_name|tcl_word}} [get_ports {{clock.port|tcl_word}}]
{% endif -%}
{% endfor -%}

derive_pll_clocks
derive_clock_uncertainty