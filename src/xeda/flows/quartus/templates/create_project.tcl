set design_name           {{design.name|tcl_word}}
set top                   {{design.rtl.top|tcl_word}}
set fpga_part             {{settings.fpga.part|tcl_word}}
{%- if settings.debug %}
foreach key [array names quartus] {
    puts "${key}=$quartus($key)"
}
{%- endif %}

package require ::quartus::project

puts "\n===========================( Setting up project and settings )==========================="
project_new ${design_name} -overwrite
{% if settings.nthreads is not none %}
set_global_assignment -name NUM_PARALLEL_PROCESSORS {{settings.nthreads}}
{% endif %}
puts "supported FPGA families: [get_family_list]"

set fpga_part_report [report_part_info $fpga_part]
puts $fpga_part_report

{%- if settings.fpga.family %}
set_global_assignment -name FAMILY {{settings.fpga.family|tcl_word}}
{%- endif %}
# Use get_part_list to get a list of supported part numbers
set_global_assignment -name DEVICE $fpga_part

set_global_assignment -name TOP_LEVEL_ENTITY ${top}

{%- if design.language.vhdl.standard %}
set_global_assignment -name VHDL_INPUT_VERSION {{("VHDL_" ~ design.language.vhdl.standard)|tcl_word}}
{%- endif %}

{%- for src in sources_read() %}
{%- if src.type.name == "Verilog" %}
set_global_assignment -name VERILOG_FILE {{src.file|tcl_word}}
{%- elif src.type.name == "SystemVerilog" %}
set_global_assignment -name SYSTEMVERILOG_FILE {{src.file|tcl_word}}
{%- elif src.type.name == "Vhdl" %}
set_global_assignment -name VHDL_FILE {{src.file|tcl_word}}
{%- elif src.type.name in ("VerilogHeader", "SVHeader") %}
set_global_assignment -name SEARCH_PATH {{src.file.parent|tcl_word}}
{%- elif src.type.name == "Sdc" %}
set_global_assignment -name SDC_FILE {{src.file|tcl_word}}
{%- endif %}
{%- endfor %}

{%- for k,v in design.rtl.parameters.items() %}
set_parameter -name {{k|tcl_word}} {%if v is boolean -%} {{"true" if v else "false"}} {% elif v is number -%} {{v}} {%else-%} {{v|tcl_word}} {%endif%}
{%- endfor %}

{%- for sdc_file in sdc_files %}
set_global_assignment -name SDC_FILE {{sdc_file|tcl_word}}
{%- endfor %}

{%- for k,v in project_settings.items() %}
{%- if v is not none %}
set_global_assignment -name {{k|tcl_word}} {%if v is boolean -%} {{"ON" if v else "OFF"}}  {% elif v is number -%} {{v}} {%- else -%} {{v|tcl_word}} {%- endif %}
{%- endif %}
{%- endfor %}

project_close