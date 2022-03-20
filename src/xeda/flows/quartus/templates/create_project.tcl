set design_name           {{design.name}}
set top                   {{design.rtl.primary_top}}
set fpga_part             {{settings.fpga.part}}
{%- if settings.debug %}
foreach key [array names quartus] {
    puts "${key}=$quartus($key)"
}
{%- endif %}

package require ::quartus::project

puts "\n===========================( Setting up project and settings )==========================="
project_new ${design_name} -overwrite

set_global_assignment -name NUM_PARALLEL_PROCESSORS {{settings.ncpus}}

puts "supported FPGA families: [get_family_list]"

set fpga_part_report [report_part_info $fpga_part]
puts $fpga_part_report

{%- if settings.fpga.family %}
set_global_assignment -name FAMILY "{{settings.fpga.family}}"
{%- endif %}
# Use get_part_list to get a list of supported part numbers
set_global_assignment -name DEVICE $fpga_part

set_global_assignment -name TOP_LEVEL_ENTITY ${top}

{%- if design.language.vhdl.standard == "08" %}
set_global_assignment -name VHDL_INPUT_VERSION VHDL_2008
{%- endif %}

{%- for src in design.rtl.sources %}
set_global_assignment -name {% if src.type == "verilog" and src.variant == "systemverilog" -%} SYSTEMVERILOG {%- else -%} {{src.type|upper}} {%- endif -%}_FILE {{src.file}}
{%- endfor %}

{%- for k,v in design.rtl.parameters.items() %}
set_parameter -name {{k}} {%if v is boolean -%} {{"true" if v else "false"}} {% elif v is string -%} "{{v}}" {%else-%} {{v}} {%endif%}
{%- endfor %}

{%- for sdc_file in sdc_files %}
set_global_assignment -name SDC_FILE {{sdc_file}}
{%- endfor %}

{%- for k,v in project_settings.items() %}
{%- if v is not none %}
set_global_assignment -name {{k}} {%if v is boolean -%} {{"ON" if v else "OFF"}}  {% elif v is number -%} {{v}} {%- else -%} "{{v}}" {%- endif %}
{%- endif %}
{%- endfor %}

project_close