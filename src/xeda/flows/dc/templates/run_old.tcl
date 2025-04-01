# Remove new variable info messages from the end of the log file
set_app_var sh_new_variable_message false

set dc_design_name                {{design.rtl.top}}
set dc_clock_period               {{settings.clock_period}}
set dc_reports_dir                {{settings.reports_dir}}
set dc_results_dir                {{settings.outputs_dir}}

file mkdir ${dc_reports_dir}
file mkdir ${dc_results_dir}
file delete -force {*}[glob -nocomplain ${dc_reports_dir}/*]
file delete -force {*}[glob -nocomplain ${dc_results_dir}/*]

{% if settings.nthreads is not none %}
set_host_options -max_cores {{nthreads}}
{% endif %}

{% if settings.alib_dir is not none %}
# Set up alib caching for faster consecutive runs
set_app_var alib_library_analysis_path {{settings.alib_dir}}
{% endif %}

# Set up search path for libraries and design files
{-% if settings.additional_search_path is not none %}
set_app_var search_path "{{settings.additional_search_path}} $search_path"
{% endif -%}
set_app_var search_path ". $search_path"

# - target_library    -- DC maps the design to gates in this library (db)
# - synthetic_library -- DesignWare library (sldb)
# - link_library      -- Libraries for any other design references (e.g.,
#                        SRAMs, hierarchical blocks, macros, IO libs) (db)
set_app_var target_library     "{{settings.target_libraries |join(' ')}}"
set_app_var synthetic_library  dw_foundation.sldb
set_app_var link_library       [join "
*
$target_library
{{settings.extra_link_libraries|join(' ')}}
$synthetic_library
"]


# Create a new Milkyway design library
set milkyway_library ${dc_design_name}_lib
# By default, Milkyway libraries only have 180 or so layers available to
# use (255 total, but some are reserved).
# Expand the Milkyway library to accommodate up to 4095 layers.
extend_mw_layers
create_mw_lib {{%-if settings.mw_tf is not none%}} -technology {{settings.mw_tf}} {{%-endif%}} {{%-if settings.mw_ref_lib is not none%}} -mw_reference_library {{settings.mw_ref_lib}} {{%-endif%}} $milkyway_library
open_mw_lib $milkyway_library

## FIXME:
{% if settings.tluplus_map is not none %}
# Set up TLU plus (if the files exist)
# TODO -min_tluplus  {{settings.tluplus_min}}
if {[shell_is_in_topographical_mode]} {
  if {[file exists [which $dc_tluplus_max]]} {
    set_tlu_plus_files {{%-if settings.tluplus_max is not none%}} -max_tluplus {{settings.tluplus_max}}  {{%-endif%}} {{%-if settings.tluplus_min is not none%}} -min_tluplus {{settings.tluplus_min}} {{%-endif%}} -tech2itf_map {{settings.tluplus_map}}
    check_tlu_plus_files
  }
}
{% endif %}

# Set up tracking for Synopsys Formality
set_svf ${dc_results_dir}/${dc_design_name}.mapped.svf

# SAIF mapping
saif_map -start

{%- if design.language.vhdl.standard in ("08", "2008") %}
set hdlin_vhdl_std 2008
{% elif design.language.vhdl.standard in ("93", "1993") %}
set hdlin_vhdl_std 1993
{% elif design.language.vhdl.standard %}
set hdlin_vhdl_std {{design.language.vhdl.standard}}
{%- endif %}

{%- for k,v in settings.hdlin.items() %}
set hdlin_{{k}} {{v}}
{%- endfor %}

{%- for k,v in settings.compile.items() %}
set compile_{{k}} {{v}}
{%- endfor %}


puts "\n===========================( Checking Libraries )==========================="
check_library > $dc_reports_dir/${dc_design_name}.check_library.rpt

query_objects [get_libs -quiet *]

# The first "WORK" is a reserved word for Design Compiler. The value for
# the -path option is customizable.
define_design_lib WORK -path ${dc_results_dir}/WORK


{% for src in design.rtl.sources %}
{%- if src.type.name == "Verilog" %}
puts "\n===========================( Analyzing Verilog file {{src.file}} )==========================="
if { ![analyze -format verilog {{src.file}}] } { exit 1 }
{%- elif src.type.name == "SystemVerilog" %}
puts "\n===========================( Analyzing SystemVerilog file {{src.file}} )==========================="
if { ![analyze -format sverilog {{src.file}}] } { exit 1 }
{%- elif src.type.name == "Vhdl" %}
puts "\n===========================( Analyzing VHDL file {{src.file}} )==========================="
if { ![analyze -format vhdl {{src.file}}] } { exit 1 }
{%- elif src.type.name == "Tcl" %}
puts "\n===========================( Sourcing TCL file {{src.file}} )==========================="
if { ![source -echo {{src.file}}] } { exit 1 }
{% endif %}
{% endfor %}

puts "\n===========================( Elaborating Design )==========================="
# TODO add generics/params using -parameters N=8,M=3
if {[file exists [which setup-design-params.txt]]} {
  elaborate $dc_design_name -file_parameters setup-design-params.txt
  rename_design $dc_design_name* $dc_design_name
} else {
  elaborate $dc_design_name
}

current_design $dc_design_name
puts "\n===========================( Linking Design )==========================="
link

# TODO add hook to drop into interactive dc_shell

# This ddc can be used as a checkpoint to load up to the current state
write_file -hierarchy -format ddc -output ${dc_results_dir}/${dc_design_name}.elab.ddc

# This Verilog is useful to double-check the netlist that dc will use for
# mapping
write_file -hierarchy -format verilog -output ${dc_results_dir}/${dc_design_name}.elab.v

set clock_name ideal_clock
create_clock -name ${clock_name} -period ${dc_clock_period} [get_ports {{design.rtl.clock_port}}]

# This constraint sets the load capacitance in picofarads of the
# output pins of your design.
# set_load -pin_load {{adk.typical_on_chip_load}} [all_outputs]

# drive strength of the input pins from specific standard cell which models what
# would be driving the inputs. This should usually be a small inverter
# which is reasonable if another block of on-chip logic is driving your inputs.
# set_driving_cell -no_design_rule -lib_cell {{adk.driving_cell}} [all_inputs]

# - make this non-zero to avoid hold buffers on input-registered designs
set_input_delay -clock ${clock_name} [expr ${dc_clock_period}/2.0] [filter [all_inputs] {NAME != {{design.rtl.clock_port}} } ]

# set_output_delay constraints for output ports
set_output_delay -clock ${clock_name} 0 [all_outputs]

# Make all signals limit their fanout
set_max_fanout 20 $dc_design_name

# Make all signals meet good slew
set_max_transition [expr 0.25*${dc_clock_period}] $dc_design_name

#set_input_transition 1 [all_inputs]
#set_max_transition 10 [all_outputs]


# Set up common path groups to help the timing engine focus individually
# on different sets of paths.
set ports_clock_root [filter_collection [get_attribute [get_clocks] sources] object_class==port]
group_path -name REGOUT -to   [all_outputs]
group_path -name REGIN -from [remove_from_collection [all_inputs] $ports_clock_root]
group_path -name FEEDTHROUGH -from [remove_from_collection [all_inputs] $ports_clock_root] -to   [all_outputs]


# Flatten effort
# - Effort 0: No auto-ungrouping / boundary optimizations (strict hierarchy)
# - Effort 1: No auto-ungrouping / boundary optimizations
#             DesignWare cells are ungrouped (var compile_ultra_ungroup_dw)
# - Effort 2: Enable auto-ungrouping / boundary optimizations
#             DesignWare cells are ungrouped (var compile_ultra_ungroup_dw)
# - Effort 3: Everything ungrouped + level param for how deep to ungroup
#
# Note that even with boundary optimizations off, DC will still propagate
# constants across the boundary, although this can be disabled with a
# variable if we really wanted to disable it.
set_optimize_registers true
set_compile_spg_mode icc2

set compile_ultra_options " -spg -retime"

{% if settings.get('flatten_effort') == 0 %}
puts "Info: All design hierarchies are preserved unless otherwise specified."
set_app_var compile_ultra_ungroup_dw false
puts "Info: Design Compiler compile_ultra boundary optimization is disabled."
append compile_ultra_options " -no_autoungroup -no_boundary_optimization"
{% elif settings.get('flatten_effort') == 1 %}
puts "Info: Unconditionally ungroup the DesignWare cells."
set_app_var compile_ultra_ungroup_dw true
puts "Info: Design Compiler compile_ultra automatic ungrouping is disabled."
puts "Info: Design Compiler compile_ultra boundary optimization is disabled."
append compile_ultra_options " -no_autoungroup -no_boundary_optimization"
{% elif settings.get('flatten_effort') == 2 %}
puts "Info: Unconditionally ungroup the DesignWare cells."
set_app_var compile_ultra_ungroup_dw true
puts "Info: Design Compiler compile_ultra automatic ungrouping is enabled."
puts "Info: Design Compiler compile_ultra boundary optimization is enabled."
{% else %}
set ungroup_start_level 2
ungroup -start_level $ungroup_start_level -all -flatten
puts "Info: All hierarchical cells starting from level $ungroup_start_level are flattened."
puts "Info: Unconditionally ungroup the DesignWare cells."
puts "Info: Design Compiler compile_ultra automatic ungrouping is enabled."
puts "Info: Design Compiler compile_ultra boundary optimization is enabled."
set_app_var compile_ultra_ungroup_dw true
{% endif %}

{% if settings.get('gate_clock', False) %}
append compile_ultra_options " -gate_clock -self_gating"
{% endif %}

# Three-state nets are declared as Verilog "wire" instead of "tri." This
# is useful in eliminating "assign" primitives and "tran" gates in the
# Verilog output.
set_app_var verilogout_no_tri true

# Prevent assignment statements in the Verilog netlist
set_fix_multiple_port_nets -all -buffer_constants

# Set the minimum and maximum routing layers used in DC topographical mode
if {[shell_is_in_topographical_mode]} {
  # set_ignored_layers -min_routing_layer {{adk.min_routing_layer_dc}}
  # set_ignored_layers -max_routing_layer {{adk.max_routing_layer_dc}}
}
report_ignored_layers

# The check_timing command checks for constraint problems such as
# undefined clocking, undefined input arrival times, and undefined output
# constraints. These constraint problems could cause you to overlook
# timing violations. For this reason, the check_timing command is
# recommended whenever you apply new constraints such as clock
# definitions, I/O delays, or timing exceptions.
redirect -tee ${dc_reports_dir}/${dc_design_name}.premapped.checktiming.rpt {check_timing}

# Check design for consistency
# Most problems with synthesis will be caught in this report
check_design -summary
check_design > ${dc_reports_dir}/${dc_design_name}.premapped.checkdesign.rpt

puts "\n===========================( Compiling Design )==========================="
eval "compile_ultra $compile_ultra_options"

puts "\n===========================( Optimizing Design )==========================="
optimize_netlist -area

puts "\n===========================( Checking Design )==========================="
check_design -summary
check_design > ${dc_reports_dir}/${dc_design_name}.mapped.checkdesign.rpt

# Synopsys Formality
set_svf -off

{% if settings.get('uniquify_with_design_name') %}
# Uniquify by prefixing every module in the design with the design name.
# This is useful for hierarchical LVS when multiple blocks use modules
# with the same name but different definitions.
set uniquify_naming_style "${dc_design_name}_%s_%d"
uniquify -force
{% endif %}

# Use naming rules to preserve structs
define_name_rules verilog -preserve_struct_ports
report_names -rules verilog > ${dc_reports_dir}/${dc_design_name}.mapped.naming.rpt

# Replace special characters with non-special ones before writing out a
# Verilog netlist (e.g., "\bus[5]" -> "bus_5_")
change_names -rules verilog -hierarchy

puts "\n===========================( Writing Results )==========================="

{% if settings.get('saif') -%}
# Write the .namemap file for energy analysis
saif_map -create_map -input {{settings.saif.file}} -source_instance {{settings.saif.instance}}
{%- endif %}

# Write out files
write_file -format ddc -hierarchy -output ${dc_results_dir}/${dc_design_name}.mapped.ddc
write_file -format verilog -hierarchy -output ${dc_results_dir}/${dc_design_name}.mapped.v
write_file -format vhdl -hierarchy -output ${dc_results_dir}/${dc_design_name}.mapped.top.vhd

# write -format svsim \
#       -output ${dc_results_dir}/${dc_design_name}.mapped.svwrapper.v

# Dump the mapped.v and svwrapper.v into one svsim.v file to make it
# easier to include a single file for gate-level simulation. The svwrapper
# matches the interface of the original RTL even if using SystemVerilog
# features (e.g., array of arrays, uses parameters, etc.).

# sh cat ${dc_results_dir}/${dc_design_name}.mapped.v \
#        ${dc_results_dir}/${dc_design_name}.mapped.svwrapper.v \
#        > ${dc_results_dir}/${dc_design_name}.mapped.svsim.v

# Write top-level verilog view needed for block instantiation
write_file -format verilog -output ${dc_results_dir}/${dc_design_name}.mapped.top.v

# Floorplan
if {[shell_is_in_topographical_mode]} {
  write_floorplan -all ${dc_results_dir}/${dc_design_name}.mapped.fp
}

# Parasitics
write_parasitics -output ${dc_results_dir}/${dc_design_name}.mapped.spef

# SDF for back-annotated gate-level simulation
write_sdf ${dc_results_dir}/${dc_design_name}.mapped.sdf

# Do not write out net RC info into SDC
set_app_var write_sdc_output_lumped_net_capacitance false
set_app_var write_sdc_output_net_resistance false

# SDC constraints
write_sdc -nosplit ${dc_results_dir}/${dc_design_name}.mapped.sdc

# Write IC Compiler II scripts
write_icc2_files -force -output ${dc_results_dir}/icc2_files

puts "\n===========================( Writing Reports )==========================="

# Report design
report_design -nosplit > ${dc_reports_dir}/${dc_design_name}.vars.rpt

# Report variables
print_variable_group all > ${dc_reports_dir}/${dc_design_name}.vars.rpt

# Report units
redirect -tee ${dc_reports_dir}/${dc_design_name}.mapped.units.rpt {report_units}

# Report QOR
report_qor > ${dc_reports_dir}/${dc_design_name}.mapped.qor.rpt

# Report timing
report_clock_timing -type summary > ${dc_reports_dir}/${dc_design_name}.mapped.timing.clock.rpt
report_timing -input_pins -capacitance -transition_time -nets -significant_digits 4 -nosplit -path_type full_clock -attributes -nworst 10 -max_paths 30 -delay_type max > ${dc_reports_dir}/${dc_design_name}.mapped.timing.setup.rpt
report_timing -input_pins -capacitance -transition_time -nets -significant_digits 4 -nosplit -path_type full_clock -attributes -nworst 10 -max_paths 30 -delay_type min > ${dc_reports_dir}/${dc_design_name}.mapped.timing.hold.rpt

# Report constraints
report_constraint -nosplit -verbose > ${dc_reports_dir}/${dc_design_name}.mapped.constraints.rpt
report_constraint -nosplit -verbose -all_violators > ${dc_reports_dir}/${dc_design_name}.mapped.constraints.violators.rpt
report_timing_requirements > ${dc_reports_dir}/${dc_design_name}.mapped.timing.requirements.rpt

# Report area
report_area -hierarchy -physical -nosplit -designware > ${dc_reports_dir}/${dc_design_name}.mapped.area.rpt

# Report references and resources
report_reference -nosplit -hierarchy > ${dc_reports_dir}/${dc_design_name}.mapped.reference.rpt
report_resources -nosplit -hierarchy > ${dc_reports_dir}/${dc_design_name}.mapped.resources.rpt

# Report power
{% if settings.get('saif') -%}
read_saif -map_names -input {{settings.saif.file}} -instance_name {{settings.saif.instance}}  -verbose
report_saif -hier -annotated_flag -rtl_saif > ${dc_reports_dir}/${dc_design_name}.mapped.saif.rpt
saif_map -type ptpx -write_map ${dc_reports_dir}/${dc_design_name}.namemap
{%- endif %}

report_power -nosplit -analysis_effort high > ${dc_reports_dir}/${dc_design_name}.mapped.power.rpt
report_power -nosplit -analysis_effort high -hierarchy -levels 3 > ${dc_reports_dir}/${dc_design_name}.mapped.hier.power.rpt

# Report clock gating
report_clock_gating -nosplit > ${dc_reports_dir}/${dc_design_name}.mapped.clock_gating.rpt

# for computing gate-equivalent
# set NAND2_AREA [get_attribute {{adk.lib_name}}/{{adk.nand2_gate}} area]

# set f [open ${dc_reports_dir}/${dc_design_name}.mapped.area.rpt "a"]
# puts $f "Area of cell library's basic NAND2 gate ({{adk.nand2_gate}}) is: $NAND2_AREA\n"
# close $f

puts "\n\n---*****===( DC synthesis successfully completed )===*****---\n"

exit
