{% for clock_name,clock in settings.clocks.items() -%}
{% if clock.port -%}
create_clock -period {{ "%.3f"|format(clock.period) }} -name {{clock_name}} [get_ports {{clock.port}}]
{% endif -%}
{% endfor -%}

derive_pll_clocks
derive_clock_uncertainty