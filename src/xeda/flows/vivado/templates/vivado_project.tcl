set project_name {{design.name|tcl_word}}
set project_dir ""

set project_file [file normalize [file join $project_dir $project_name.xpr]]

create_project {% if settings.fpga and settings.fpga.part -%} -part {{settings.fpga.part|tcl_word}} {%- endif %} -force -verbose "$project_name"


{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}
{%- for msg in settings.suppress_msgs %}
set_msg_config -id {{("[" ~ msg ~ "]")|tcl_word}} -suppress
{%- endfor %}

{%- if sources %}
add_files -fileset sources_1 -norecurse {{ sources | tcl_list }}
{%- endif %}

{%- set tb_sources = sources_read(rtl=false, tb=true) %}
{%- if tb_sources %}
add_files -fileset sim_1 -norecurse {{ tb_sources | tcl_list }}
{%- endif %}

{%- for xdc_file in xdc_files %}
add_files -fileset constrs_1 -norecurse {{xdc_file|tcl_list}}
{%- endfor %}

{%- for tcl_file in tcl_files %}
add_files -fileset utils_1 -norecurse {{tcl_file|tcl_list}}
{%- endfor %}

{%- for src in sources_read(rtl=true, tb=true) %}
set_property FILE_TYPE {{vivado_file_type(src)|tcl_word}} [get_files {{src|tcl_list}}]
{%- endfor %}

update_compile_order -fileset sources_1
update_compile_order -fileset sim_1

{% if design.rtl.top %}
set_property top {{design.rtl.top|tcl_word}} [get_fileset sources_1]
{% endif %}

{% if design.tb and design.tb.top %}
set_property top {{design.tb.top[0]|tcl_word}} [get_fileset sim_1]
{% endif %}

# set avail_synth_strategies [join [list_property_value strategy [get_runs synth_1] ] " "]
# puts "\n Available synthesis strategies:\n  $avail_synth_strategies\n"

{%- if settings.synth.strategy %}
puts "Using {{settings.synth.strategy|tcl_quote}} strategy for synthesis."
set_property strategy {{settings.synth.strategy|tcl_word}} [get_runs synth_1]
{%- endif %}

# set avail_impl_strategies [join [list_property_value strategy [get_runs impl_1] ] " "]
# puts "\n Available implementation strategies:\n  $avail_impl_strategies\n"

{%- if settings.impl.strategy %}
puts "Using {{settings.impl.strategy|tcl_quote}} strategy for implementation."
set_property strategy {{settings.impl.strategy|tcl_word}} [get_runs impl_1]
{%- endif %}

{%- if generics %}
set_property generic {{generics|tcl_word}} [current_fileset]
{%- endif %}

{# see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug912-vivado-properties.pdf #}
{# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug835-vivado-tcl-commands.pdf #}
{%- for step,options in synth_steps.items() %}
{%- for name,value in options.items() %}
{% if value is mapping %}
{%- for k,v in value.items() %}
set_property {{("STEPS." ~ step ~ "." ~ name ~ "." ~ k)|tcl_word}} {{v|tcl_word}} [get_runs synth_1]
{%- endfor %}
{%- else %}
set_property {{("STEPS." ~ step ~ "." ~ name)|tcl_word}} {{value|tcl_word}} [get_runs synth_1]
{%- endif %}
{%- endfor %}
{%- endfor %}

{%- for step,options in impl_steps.items() %}
{%- for name,value in options.items() %}
{% if value is mapping %}
{%- for k,v in value.items() %}
set_property {{("STEPS." ~ step ~ "." ~ name ~ "." ~ k)|tcl_word}} {{v|tcl_word}} [get_runs impl_1]
{%- endfor %}
{%- else %}
set_property {{("STEPS." ~ step ~ "." ~ name)|tcl_word}} {{value|tcl_word}} [get_runs impl_1]
{%- endif %}
{%- endfor %}
{%- endfor %}


#-----

{%- if not settings.gui %}
close_project
{%- endif %}
