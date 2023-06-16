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
{%- if settings.show_rtl %}
yosys show -prefix rtl_show -format dot {{settings.show_rtl_flags|join(" ")}}
{%- endif %}

{%- if settings.stop_after == "rtl" %}
exit
{%- endif %}