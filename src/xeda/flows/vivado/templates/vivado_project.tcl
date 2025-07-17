set project_name {{design.name}}
set project_dir ""

set project_file [file normalize [file join $project_dir $project_name.xpr]]

create_project {% if settings.fpga and settings.fpga.part -%} -part {{settings.fpga.part}} {%- endif %} -force -verbose "$project_name"


{%- if settings.nthreads is not none %}
set_param general.maxThreads {{settings.nthreads}}
{%- endif %}
{%- for msg in settings.suppress_msgs %}
set_msg_config -id "\[{{msg}}\]" -suppress
{%- endfor %}

{%- if design.rtl.sources %}
add_files -fileset sources_1 -norecurse {{ design.rtl.sources | join(" ") }}
{%- endif %}

{%- if design.tb and design.tb.sources %}
add_files -fileset sim_1 -norecurse {{ design.tb.sources | join(" ") }}
{%- endif %}

{%- for xdc_file in xdc_files %}
add_files -fileset constrs_1 -norecurse  {{xdc_file}}
{%- endfor %}

update_compile_order -fileset sources_1
update_compile_order -fileset sim_1

{% if design.rtl.top %}
set_property top {{design.rtl.top}} [get_fileset sources_1]
{% endif %}

{% if design.tb and design.tb.top %}
set_property top {{design.tb.top[0]}} [get_fileset sim_1]
{% endif %}

# set avail_synth_strategies [join [list_property_value strategy [get_runs synth_1] ] " "]
# puts "\n Available synthesis strategies:\n  $avail_synth_strategies\n"

{%- if settings.synth.strategy %}
puts "Using {{settings.synth.strategy}} strategy for synthesis."
set_property strategy {{settings.synth.strategy}} [get_runs synth_1]
{%- endif %}

# set avail_impl_strategies [join [list_property_value strategy [get_runs impl_1] ] " "]
# puts "\n Available implementation strategies:\n  $avail_impl_strategies\n"

{%- if settings.impl.strategy %}
puts "Using {{settings.impl.strategy}} strategy for implementation."
set_property strategy {{settings.impl.strategy}} [get_runs impl_1]
{%- endif %}

{%- if generics %}
set_property generic {% raw -%} { {%- endraw -%} {{generics}} {%- raw -%} } {%- endraw %} [current_fileset]
{%- endif %}

{# see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug912-vivado-properties.pdf #}
{# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug835-vivado-tcl-commands.pdf #}
{%- for step,options in settings.synth.steps.items() %}
{%- for name,value in options.items() %}
{% if value is mapping %}
{%- for k,v in value.items() %}
set_property STEPS.{{step}}.{{name}}.{{k}} {{v}} [get_runs synth_1]
{%- endfor %}
{%- else %}
set_property STEPS.{{step}}.{{name}} {{value}} [get_runs synth_1]
{%- endif %}
{%- endfor %}
{%- endfor %}

{%- for step,options in settings.impl.steps.items() %}
{%- for name,value in options.items() %}
{% if value is mapping %}
{%- for k,v in value.items() %}
set_property STEPS.{{step}}.{{name}}.{{k}} {{v}} [get_runs impl_1]
{%- endfor %}
{%- else %}
set_property STEPS.{{step}}.{{name}} {{value}} [get_runs impl_1]
{%- endif %}
{%- endfor %}
{%- endfor %}

add_files -fileset utils_1 -norecurse [pwd]/{{reports_tcl}}
set_property STEPS.OPT_DESIGN.TCL.POST [pwd]/vivado_report_helper.tcl [get_runs impl_1]
set_property STEPS.PLACE_DESIGN.TCL.POST [pwd]/vivado_report_helper.tcl [get_runs impl_1]
set_property STEPS.ROUTE_DESIGN.TCL.POST [pwd]/{{reports_tcl}} [get_runs impl_1]

#-----

set script_mode $rdi::mode

set the_current_project [current_project]


if { $script_mode ne "gui" } {
  if { $the_current_project ne "" } {
    close_project
  }
  open_project "$project_file"

  

  start_gui
}

if { $the_current_project ne "" } {
  close_project
}
open_project "$project_file"

close_project