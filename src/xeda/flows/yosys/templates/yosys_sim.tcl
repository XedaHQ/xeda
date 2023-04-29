{# yosys -import#}
set log_prefix "yosys> "

{# handle errors?? remove nonexisting plugins? #}
{%- for plugin in settings.plugins %}
yosys plugin -i {{plugin}}
{%- endfor %}

{%- set sv_files = design.sim_sources_of_type("SystemVerilog", rtl=True, tb=True) | list %}

{# defered loading of systemverilog files #}
{%- set systemverilog_plugin_defered = sv_files and "systemverilog" in settings.plugins %}

{%- for src in design.rtl.sources %}
    {%- if src.type.name == "Verilog" %}
    puts "$log_prefix Reading {{src}}"
    ## -Dname=value -Idir
    yosys read_verilog {{settings.read_verilog_flags|join(" ")}} {{src}}
    {%- elif src.type.name == "SystemVerilog" %}
    puts "$log_prefix Reading {{src}}"
        {%- if systemverilog_plugin_defered %}
        yosys read_systemverilog -defer {{settings.read_systemverilog_flags|join(" ")}} {{src}}
        {% else %}
        yosys read_verilog {{settings.read_verilog_flags|join(" ")}} -sv {{src}}
        {%- endif %}
    {%- endif %}
{%- endfor %}

{%- if systemverilog_plugin_defered %}
yosys read_systemverilog -link
{% endif %}

{%- set vhdl_files = design.sim_sources_of_type("Vhdl", rtl=True, tb=True) | list %}
{%- if vhdl_files %}
    puts "$log_prefix Reading VHDL files: {{vhdl_files|join(" ")}}"
    yosys plugin -i ghdl
    yosys ghdl {{ghdl_args|join(" ")}}
{%- endif %}

{%- for src in settings.verilog_lib %}
yosys read_verilog -lib {{src}}
{%- endfor %}

yosys hierarchy -nodefaults -check {%- if design.tb.top %} -top {{design.tb.top}} {%- else %} -auto-top {%- endif %}

yosys check -initdrv -assert
{% for attr, value in settings.set_attribute.items() %}
{% if value is mapping %}
{% for path, v in value %}
yosys setattr -set {{attr}} {{v}} {{path}}
{% endfor %}
{% else %}
yosys setattr -set {{attr}} {{value}}
{% endif %}
{% endfor %}

{%- if settings.prep is not none %}
    yosys prep {%- if settings.flatten %} -flatten {%- endif %} {{settings.prep|join(" ")}}
{%- else %}
    yosys proc
    {%- if settings.flatten %}
        yosys flatten
    {%- endif %}
{%- endif %}

yosys check {% if settings.check_assert %} -assert {%- endif %}

{%- if settings.rtl_json %}
puts "$log_prefix Writing JSON output to: {{settings.rtl_json}}"
yosys write_json {{settings.rtl_json}}
{%- endif %}
{%- if settings.rtl_verilog %}
puts "$log_prefix Writing Verilog output to: {{settings.rtl_verilog}}"
yosys write_verilog {{settings.rtl_verilog}}
{%- endif %}
{%- if settings.rtl_vhdl %}
puts "$log_prefix Writing VHDL output to: {{settings.rtl_vhdl}}"
yosys write_vhdl {{settings.rtl_vhdl}}
{%- endif %}
{%- if settings.show_rtl %}
yosys show -prefix rtl_show -format dot {{settings.show_rtl_flags|join(" ")}}
{%- endif %}

puts "$log_prefix Writing CXXRTL output to: {{settings.cxxrtl.filename}}"
yosys write_cxxrtl {%- if settings.cxxrtl.header %} -header {%- endif %} {%- if settings.cxxrtl.opt is not none %} -O{{settings.cxxrtl.opt}} {%- endif %} {{settings.cxxrtl.filename}}

