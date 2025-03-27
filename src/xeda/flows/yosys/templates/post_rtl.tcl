{%- if settings.rtl_json %}
yosys log -stdout "Writing JSON {{settings.rtl_json}}"
yosys write_json {{settings.rtl_json}}
{%- endif %}
{%- if settings.rtl_verilog %}
yosys log -stdout "Writing Verilog {{settings.rtl_verilog}}"
yosys write_verilog {{settings.rtl_verilog}}
{%- endif %}
{%- if settings.rtl_vhdl %}
yosys log -stdout " Writing VHDL {{settings.rtl_vhdl}}"
yosys write_vhdl {{settings.rtl_vhdl}}
{%- endif %}
{%- if settings.rtl_graph %}
yosys log -stdout "Writing RTL graph to {{settings.rtl_graph.with_suffix('dot')}}"
yosys show -prefix {{settings.rtl_graph.with_suffix("")}} -format dot {{settings.rtl_graph_flags|join(" ")}}
{%- endif %}

{%- if settings.stop_after == "rtl" %}
exit
{%- endif %}