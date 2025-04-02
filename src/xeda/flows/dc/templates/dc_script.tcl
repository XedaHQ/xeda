set_app_var sh_new_variable_message false

set SCRIPTS_DIR [file dirname [info script]]

set OUTPUTS_DIR {{settings.outputs_dir}}
set REPORTS_DIR {{settings.reports_dir}}

set TOP_MODULE {{design.rtl.top}}

if { $TOP_MODULE == "" } {
    puts "\[ERROR]\ No top module specified."
    exit 1
}

set OPTIMIZATION {{settings.optimization}}

set TARGET_LIBRARY_FILES "{{settings.target_libraries | join(' ')}}"

{% if settings.nthreads is not none %}
set_host_options -max_cores {{nthreads}}
{% endif %}

if { ![file exists $OUTPUTS_DIR] } {
    file mkdir $OUTPUTS_DIR
}
if { ![file exists $REPORTS_DIR] } {
    file mkdir $REPORTS_DIR
}

{%- if design.language.vhdl.standard in ("02", "2002", "08", "2008") %}
set_app_var hdlin_vhdl_std 2008
{% elif design.language.vhdl.standard in ("93", "1993") %}
set_app_var hdlin_vhdl_std 1993
{% elif design.language.vhdl.standard %}
set_app_var hdlin_vhdl_std {{design.language.vhdl.standard}}
{%- endif %}


# improve the SAIF annotation
set_app_var hdlin_enable_upf_compatible_naming true

set_app_var vhdlout_dont_create_dummy_nets true

{%- for k,v in settings.hdlin.items() %}
set_app_var hdlin_{{k}} {{v}}
{%- endfor %}

puts "Optimization: $OPTIMIZATION"

set_app_var spg_enable_via_resistance_support true

set_app_var hdlin_infer_multibit default_all

saif_map -start

if { $OPTIMIZATION == "area" } {
    set_max_area 0.0
}
if { [shell_is_dcnxt_shell] } {
    if { $OPTIMIZATION == "area" } {
        set_app_var compile_high_effort_area true
        set_app_var compile_optimize_netlist_area true
    } elseif { $OPTIMIZATION == "speed" } {
        set_app_var compile_timing_high_effort true
    }
}
set_app_var search_path ". $search_path"
set_app_var target_library ${TARGET_LIBRARY_FILES}
set_app_var synthetic_library {dw_foundation.sldb}
set_app_var link_library "* $synthetic_library $target_library"

redirect -file ${REPORTS_DIR}/check_library.rpt {check_library}

# puts "\n==========( Removing existing design(s) )=========="
remove_design -all

define_design_lib WORK -path ./WORK

set_app_var hdlin_enable_hier_map true

set SOURCE_FILES { {{- design.rtl.sources | join(' ') -}} }

foreach src $SOURCE_FILES {
    set ext [file extension $src]
    switch -- $ext {
        ".v" {
            set format verilog
        }
        ".verilog" {
            set format verilog
        }
        ".vhd" {
            set format vhdl
        }
        ".vhdl" {
            set format vhdl
        }
        ".sv" {
            set format sverilog
        }
        default {
            puts "Unknown file extension: $ext"
            exit 1
        }
    }
    puts "===================( Analysing $src ($format) )==================="
    if  { [ analyze -format $format $src ] != 1 } {
        puts "\[ERROR]\ Analysing $format file $src failed!\n"
        exit 1
    }
}

puts "\n===================( Elaborating design ${TOP_MODULE} )==================="
# if { [elaborate -update -ref ${TOP_MODULE}] != 1 } {
if { [elaborate ${TOP_MODULE}] != 1 } {
    puts "\[ERROR]\ Elaborating design ${TOP_MODULE} failed!\n"
    exit 1
}

set_verification_top

if { [catch {current_design ${TOP_MODULE} } $err] } {
    puts "\[ERROR]\ Setting current design to ${TOP_MODULE} failed!\n$err"
    exit 1
}

puts "\n===================( Linking design )==================="
if { [link] != 1 } {
    puts "\[ERROR]\ Linking design failed!\n"
    exit 1
}


puts "list of designs: [list_designs]"

write_file -hierarchy -format ddc -output ${OUTPUTS_DIR}/elab.ddc
write_file -hierarchy -format verilog -output ${OUTPUTS_DIR}/elab.v

{% if settings.flatten -%}
ungroup -flatten -all
{%- endif %}

list_designs -show_file
check_design -summary

puts "\n=========( Loading the constraints )========="
{% for constraint_file in settings.sdc_files -%}
puts "Loading constraints file: {{constraint_file}}"
if { [catch {source -echo {{constraint_file}}} err] } {
    puts "\[ERROR]\ Loading constraints file {{constraint_file}} failed!\n$err"
    exit 1
}
{% endfor -%}

write_file -hierarchy -format ddc -output ${OUTPUTS_DIR}/elab.ddc

redirect -tee $REPORTS_DIR/elab.check_design.rpt {check_design}
redirect -tee $REPORTS_DIR/elab.check_timing.rpt {check_timing}


set compile_command {{settings.compile_command}}

set compile_options [list {{settings.compile_args | join(' ')}}]

if { $compile_command == "compile" } {
    if { $OPTIMIZATION == "area" } {
        set compile_options [list {*}$compile_options -area_effort high -map_effort high -auto_ungroup area -boundary_optimization]
    } elseif { $OPTIMIZATION == "speed" } {
        set compile_options [list {*}$compile_options -area_effort medium -map_effort high -auto_ungroup area -boundary_optimization]
    } elseif { $OPTIMIZATION == "power" } {
        set compile_options [list {*}$compile_options -area_effort high -map_effort high -auto_ungroup area -boundary_optimization -gate_clock -power_effort high]
    } elseif { $OPTIMIZATION == "non" } {
        set compile_options [list {*}$compile_options -exact_map]
    }
} elseif { $compile_command == "compile_ultra" } {
    if [shell_is_in_topographical_mode] then {
        set compile_options [list {*}$compile_options -spg]
    }
    if { $OPTIMIZATION == "area" } {
    } elseif { $OPTIMIZATION == "speed" } {
        set compile_options [list {*}$compile_options -retime]
    } elseif { $OPTIMIZATION == "power" } {
        set compile_options [list {*}$compile_options -gate_clock]
        if [shell_is_in_topographical_mode] then {
            set compile_options [list {*}$compile_options -self_gating]
        }
    }
} else {
    puts "Unknown compile command: $compile_command"
}

puts "\n=========( Synthesize the design: ${compile_command} )========="
if { [catch {${compile_command} {*}$compile_options} err] } {
    puts "\[ERROR]\ Compile failed!\n$err"
    exit 1
}

redirect -tee $REPORTS_DIR/synth.timing.max.rpt {report_timing -delay max -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}
redirect -tee $REPORTS_DIR/synth.timing.min.rpt {report_timing -delay min -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}
redirect -tee $REPORTS_DIR/synth.area.rpt {report_area -nosplit -designware}
redirect -tee $REPORTS_DIR/synth.area.hierarchy.rpt {report_area -nosplit -hierarchy -physical -designware}


if { $OPTIMIZATION == "area" } {
    puts "\n========= Optimizing Netlist ========="
    optimize_netlist -area
} elseif { $OPTIMIZATION == "power" } {
    puts "\n========= Optimizing Netlist ========="
    optimize_netlist -area
}

check_design -unmapped -cells -ports -designs -nets -tristates -html_file_name $REPORTS_DIR/mapped.design_summary.html

if { [catch {check_design -summary -unmapped -cells -ports -designs -nets -tristates} err] } {
    puts "\[ERROR]\ check_design failed!\n$err"
    exit 1
}

if { [catch {check_timing} err] } {
    puts "\[ERROR]\ check_timing failed!\n$err"
    exit 1
}

puts "==========================( Generating Reports )=========================="

update_timing

redirect -tee $REPORTS_DIR/mapped.checkdesign.rpt {check_design}

redirect -tee $REPORTS_DIR/mapped.design.rpt {report_design -nosplit}

# library units
redirect -tee $REPORTS_DIR/mapped.units.rpt {report_units}

redirect $REPORTS_DIR/mapped.vars.rpt {report_app_var}

report_constraint -nosplit -all_vio -significant_digits 3 > $REPORTS_DIR/mapped.constraints.rpt
report_constraint -nosplit  -verbose -all_violators -significant_digits 3 > $REPORTS_DIR/mapped.constraints.violators.rpt
report_timing_requirements > $REPORTS_DIR/mapped.timing.requirements.rpt

# report cell usage
report_reference -nosplit > $REPORTS_DIR/mapped.reference.rpt

report_cell > $REPORTS_DIR/mapped.cell.rpt

report_clock > $REPORTS_DIR/mapped.clocks.rpt
if {[sizeof_collection [all_clocks]]>0} {
    report_net [all_clocks] > $REPORTS_DIR/clock_nets.rpt
}

report_qor -nosplit > $REPORTS_DIR/mapped.qor.rpt
report_auto_ungroup -nosplit -nosplit > $REPORTS_DIR/mapped.auto_ungroup.rpt

redirect -tee $REPORTS_DIR/mapped.timing.max.rpt {report_timing -delay max -path full -nosplit -transition_time -nets -attributes -nworst 8 -max_paths 16 -significant_digits 3 -sort_by group}
redirect -tee $REPORTS_DIR/mapped.timing.min.rpt {report_timing -delay min -path full -nosplit -transition_time -nets -attributes -nworst 8 -max_paths 16 -significant_digits 3 -sort_by group}
redirect -tee $REPORTS_DIR/mapped.area.rpt {report_area -nosplit}

redirect -tee $REPORTS_DIR/mapped.power.rpt {report_power -nosplit -net -cell -analysis_effort medium}
redirect -tee $REPORTS_DIR/mapped.power.hier.rpt {report_power -nosplit -hierarchy -levels 3 -analysis_effort medium}

define_name_rules verilog -preserve_struct_ports
report_names -rules verilog > $REPORTS_DIR/mapped.naming.rpt


puts "==========================( Writing Generated Netlist )=========================="

# To prevent assign statements in the netlist
set_app_var verilogout_no_tri true
set_fix_multiple_port_nets -all -buffer_constants

change_names -rules verilog -hierarchy

write -hierarchy -format ddc -compress gzip -output $OUTPUTS_DIR/mapped.ddc
write -hierarchy -format verilog -output $OUTPUTS_DIR/${TOP_MODULE}.mapped.v
write -format svsim -output $OUTPUTS_DIR/${TOP_MODULE}.mapped.svwrapper.v
write -hierarchy -format vhdl -output $OUTPUTS_DIR/${TOP_MODULE}.mapped.vhd

write_sdf -version 2.1 $OUTPUTS_DIR/${TOP_MODULE}.mapped.sdf

set_app_var write_sdc_output_lumped_net_capacitance false
set_app_var write_sdc_output_net_resistance false
write_sdc -nosplit $OUTPUTS_DIR/mapped.sdc

print_variable_group all > $OUTPUTS_DIR/vars.rpt

write_icc2_files -force -output $OUTPUTS_DIR/icc2_files

saif_map -type ptpx -essential -write_map ${OUTPUTS_DIR}/mapped.saif.ptpx.map
saif_map -write_map ${OUTPUTS_DIR}/mapped.saif.dc.map

if {[shell_is_in_topographical_mode]} {
    write_floorplan -all ${OUTPUTS_DIR}/mapped.fp
    save_lib
}

puts "==========================( DONE )=========================="

exit