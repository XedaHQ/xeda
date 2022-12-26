{# yosys -import#}
set log_prefix "yosys> "

{# handle errors?? remove nonexisting plugins? #}
{%- for plugin in settings.plugins %}
yosys plugin -i {{plugin}}
{%- endfor %}

{%- set sv_files = design.rtl.sources | selectattr("type.name", "==", "SystemVerilog") | list %}

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

{%- set vhdl_files = design.rtl.sources | selectattr("type.name", "==", "Vhdl") | list %}
{%- if vhdl_files %}
    puts "$log_prefix Reading VHDL files: {{vhdl_files|join(" ")}}"
    yosys plugin -i ghdl
    yosys ghdl {{ghdl_args|join(" ")}}
{%- endif %}
{%- if settings.tech and settings.tech.liberty %}
yosys read_liberty {{settings.read_liberty_flags|join(" ")}} {{settings.tech.liberty}}
{%- endif %}

{%- for src in settings.verilog_lib %}
yosys read_verilog -lib {{src}}
{%- endfor %}

yosys hierarchy -nodefaults -check {%- if design.rtl.top %} -top {{design.rtl.top}} {%- else %} -auto-top {%- endif %}

yosys check -initdrv -assert
{%- for attr,attr_dict in settings.set_attributes.items() %}
    {%- for path,value in attr_dict.items() %}
    yosys setattr -set {{attr}} {{value}} {{path}}
    {%- endfor %}
{%- endfor %}

{%- if settings.prep is not none %}
    yosys prep {%- if settings.flatten %} -flatten {%- endif %} {%-if design.rtl.top %} -top {{design.rtl.top}} {% else %} -auto-top {%- endif %} {{settings.prep|join(" ")}}
{%- else %}
    yosys proc
    {%- if settings.flatten %}
        yosys flatten
    {%- endif %}
{%- endif %}

{%- if settings.rtl_json %}
puts "$log_prefix Writing JSON {{settings.rtl_json}}"
yosys write_json {{settings.rtl_json}}
{%- endif %}
{%- if settings.rtl_verilog %}
puts "$log_prefix Writing Verilog {{settings.rtl_verilog}}"
yosys write_verilog {{settings.rtl_verilog}}
{%- endif %}
{%- if settings.rtl_vhdl %}
puts "$log_prefix  Writing VHDL {{settings.rtl_vhdl}}"
yosys write_vhdl {{settings.rtl_vhdl}}
{%- endif %}
{%- if settings.show_rtl %}
yosys show -prefix rtl_show -format dot {{settings.show_rtl_flags|join(" ")}}
{%- endif %}

{%- if settings.stop_after != "rtl" %}
{%- if settings.fpga %}
    puts "$log_prefix Running FPGA synthesis for device {{settings.fpga}}"
    {%- if settings.fpga.vendor == "xilinx" %}
        puts "$log_prefix Target: Xilinx {%if settings.fpga.part%} {{settings.fpga.part}} {%else%} {{settings.fpga.device}} {%endif%}"
        yosys synth_xilinx {%- if settings.fpga.family %} -family {{settings.fpga.family}} {%- endif %} {{settings.synth_flags|join(" ")}}
    {%- elif settings.fpga.family %}
        puts "$log_prefix  Target: {{settings.fpga.family}}"
        yosys synth_{{settings.fpga.family}} {{settings.synth_flags|join(" ")}} {%- if design.rtl.top %} -top {{design.rtl.top}}{%- endif %}
    {%- else %}
        puts "$log_prefix Unknown FPGA vendor, family, or device"
    {%- endif %}
{%- else %}
    puts "$log_prefix  Running synthesis"
    yosys synth {{settings.synth_flags|join(" ")}} {%- if design.rtl.top %} -top {{design.rtl.top}}{%- endif %}
    {%- if settings.tech %}
        {%- if settings.tech.liberty %}
            puts "$log_prefix  Mapping FFs to technology library"
            yosys dfflibmap -liberty {{settings.tech.liberty}}
        {%- endif %}
            puts "$log_prefix  Running ABC"
            yosys abc {{settings.abc_flags|join(" ")}}
    {%- endif %}
{%- endif %}

{%- if settings.post_synth_opt %}
    puts "$log_prefix Post-synth optimization"
    yosys opt -full -purge -sat
{%- endif %}

{% if settings.splitnets is not none %}
    yosys splitnets {{settings.splitnets|join(" ")}}
{%- endif %}

puts "$log_prefix Writing stat to {{artifacts["report"]["utilization"]}}"
yosys tee -9 -q -o {{artifacts["report"]["utilization"]}} stat {%- if artifacts["report"]["utilization"].endswith(".json") %} -json {%- endif %} {%- if settings.fpga and settings.fpga.vendor == "xilinx" %} -tech xilinx {%-elif settings.tech and settings.tech.liberty%} -cmos -liberty {{settings.tech.liberty}} {%- endif %}

yosys check {% if settings.check_assert %} -assert {%- endif %}

{%- if settings.netlist_json %}
    puts "$log_prefix Writing netlist {{settings.netlist_json}}"
    yosys write_json {{settings.netlist_json}}
{%- endif %}
{%- if settings.netlist_verilog %}
    puts "$log_prefix Writing netlist {{settings.netlist_verilog}}"
    yosys write_verilog {{settings.write_verilog_flags|join(" ")}} {{settings.netlist_verilog}}
{%- endif %}
{%- if settings.netlist_vhdl %}
    puts "$log_prefix Writing netlist {{settings.netlist_vhdl}}"
    yosys write_vhdl {{settings.write_vhdl_flags|join(" ")}} {{settings.netlist_vhdl}}
{%- endif %}

{%- if settings.sta %}
    puts "$log_prefix Writing timing report to {{artifacts["report"]["timing"]}}"
    yosys tee -o {{artifacts["report"]["timing"]}} ltp
    yosys tee -a {{artifacts["report"]["timing"]}} sta
{%- endif %}

{% if settings.show_netlist %}
    puts "$log_prefix Writing netlist diagram to {{settings.show_netlist}}"
    yosys show -prefix netlist_show -format dot {{settings.show_netlist_flags|join(" ")}}
{%- endif %}

{%- endif %}
