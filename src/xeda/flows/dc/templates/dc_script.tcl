set_app_var sh_new_variable_message false

set SCRIPTS_DIR [file dirname [info script]]

set OUTPUTS_DIR {{settings.outputs_dir}}
set REPORTS_DIR {{settings.reports_dir}}

set TOP_MODULE {{design.rtl.top}}
set DESIGN_NAME {{design.name}}


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

set_message_info -id LINT-1   -limit 5 ;# cell does not drive any nets
set_message_info -id LINT-2   -limit 5 ;# net has no loads
set_message_info -id LINT-8   -limit 5 ;# input port is unloaded
set_message_info -id LINT-28  -limit 5 ;# port is not connected to any nets
set_message_info -id VHDL-290 -limit 1 ;# VHDL: a dummy net is created
set_message_info -id OPT-1206 -limit 1 ;# Register is a constant and will be removed


{%- for k,v in settings.hdlin.items() %}
set_app_var hdlin_{{k}} {{v}}
{%- endfor %}

puts "Optimization: $OPTIMIZATION"

set_app_var spg_enable_via_resistance_support true

set_app_var hdlin_infer_multibit default_all

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


remove_design -all

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
        ".sdc" {
            puts "Loading design SDC file: $src"
            source -echo $src
            continue
        }
        ".tcl" {
            puts "Loading design TCL file: $src"
            source -echo $src
            continue
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



if { [catch {current_design ${TOP_MODULE} } $err] } {
    puts "\[ERROR]\ Setting current design to '${TOP_MODULE}' failed!\n$err"
    exit 1
}

set TOP_MODULE [current_design]

set_verification_top


# To prevent assign statements in the netlist
set_app_var verilogout_no_tri true
set_fix_multiple_port_nets -all -buffer_constants
saif_map -start

puts "\n===================( Elaboration completed! )==================="
check_design -summary
puts "list of designs: [list_designs]"

redirect -tee $REPORTS_DIR/elab.check_design.rpt {check_design}
redirect -tee $REPORTS_DIR/elab.design.rpt {report_design -nosplit}
redirect -tee $REPORTS_DIR/elab.list_designs.rpt {list_designs}
redirect -file $REPORTS_DIR/elab.port.rpt {report_port -nosplit}

write_file -hierarchy -format ddc -output ${OUTPUTS_DIR}/${DESIGN_NAME}.elab.ddc
change_names -rules verilog -hierarchy
write_file -hierarchy -format verilog -output ${OUTPUTS_DIR}/${DESIGN_NAME}.elab.v

# change_names -rules vhdl -hierarchy
# set_app_var vhdlout_dont_create_dummy_nets true
write_file -hierarchy -format vhdl -output ${OUTPUTS_DIR}/${DESIGN_NAME}.elab.vhd
# change_names -rules verilog -hierarchy
# set_app_var vhdlout_dont_create_dummy_nets false

puts "\n===================( Linking design )==================="
if { [link] != 1 } {
    puts "\[ERROR]\ Linking design failed!\n"
    exit 1
}
check_design -summary

{% if settings.flatten -%}
ungroup -flatten -all
{%- endif %}


if { $OPTIMIZATION == "area" } {
    set_max_area 0.0
}

puts "\n=========( Loading the constraints )========="
{% for constraint_file in settings.sdc_files -%}
puts "Loading constraints file: {{constraint_file}}"
if { [catch {source -echo {{constraint_file}}} err] } {
    puts "\[ERROR]\ Loading constraints file {{constraint_file}} failed!\n$err"
    exit 1
}
{% endfor -%}

redirect -tee ${REPORTS_DIR}/linked.check_library.rpt {check_library}
redirect -tee ${REPORTS_DIR}/linked.check_design.rpt {check_design}
redirect -file ${REPORTS_DIR}/linked.check_timing.rpt {check_timing}
redirect -file ${REPORTS_DIR}/linked.constraints.rpt {report_constraint -nosplit}

write_file -hierarchy -format ddc -output ${OUTPUTS_DIR}/${DESIGN_NAME}.linked.ddc

set compile_command {{settings.compile_command}}

set compile_options [list {{settings.compile_args | join(' ')}}]

if {[shell_is_in_topographical_mode]} {
    {%if settings.max_tluplus or settings.min_tluplus -%}
    set_tlu_plus_files {%if settings.min_tluplus -%} -max_tluplus {{settings.max_tluplus}} {%endif-%} {%if settings.min_tluplus -%} -min_tluplus {{settings.min_tluplus}} {%endif-%} {%if settings.tluplus_map -%} -tech2itf_map {{settings.tluplus_map}} {%endif-%}
    check_tlu_plus_files
    {% endif -%}

    {% if settings.min_routing_layer -%}
    set_ignored_layers -min_routing_layer {{settings.min_routing_layer}}
    {% endif -%}
    {% if settings.max_routing_layer -%}
    set_ignored_layers -max_routing_layer {{settings.max_routing_layer}}
    {% endif -%}
    report_ignored_layers
    report_physical_constraints > ${REPORTS_DIR}/physical_constraints.rpt
}

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
    if {[shell_is_in_topographical_mode]} {
        set compile_options [list {*}$compile_options -spg]
    }
    if { $OPTIMIZATION == "area" } {
    } elseif { $OPTIMIZATION == "speed" } {
        set compile_options [list {*}$compile_options -retime]
    } elseif { $OPTIMIZATION == "power" } {
        set compile_options [list {*}$compile_options -gate_clock]
        if {[shell_is_in_topographical_mode]} {
            set compile_options [list {*}$compile_options -self_gating]
        }
    }
} else {
    puts "Unknown compile command: $compile_command"
}

puts "\n=========( Synthesizing the design using '${compile_command}' )========="
if { [catch {${compile_command} {*}$compile_options} err] } {
    puts "\[ERROR]\ Compile failed!\n$err"
    exit 1
}

redirect -file $REPORTS_DIR/synth.timing.max.rpt {report_timing -delay max -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}
redirect -file $REPORTS_DIR/synth.timing.min.rpt {report_timing -delay min -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}
redirect -file $REPORTS_DIR/synth.area.rpt {report_area -nosplit}
redirect -file $REPORTS_DIR/synth.area.physical.rpt {report_area -nosplit  -physical}
redirect -file $REPORTS_DIR/synth.area.designware.rpt {report_area -nosplit -designware}
redirect -file $REPORTS_DIR/synth.area.hierarchy.rpt {report_area -nosplit -hierarchy}
redirect -file $REPORTS_DIR/synth.area.hierarchy.physical.rpt {report_area -nosplit -hierarchy -physical}


if { $OPTIMIZATION != "none" } {
    puts "\n========= Optimizing Netlist for Area ========="
    if { [catch {optimize_netlist -area} -errorinfo err] } {
        puts "\[ERROR]\ Netlist area optimization failed!\n$err"
        exit 1
    }
}

set_app_var uniquify_naming_style "${DESIGN_NAME}_%s_%d"

if { [catch {uniquify -force} -errorinfo err] } {
    puts "\[ERROR]\ Uniquify failed!\n$err"
    exit 1
}

if { [catch {change_names -rules verilog -hierarchy} -errorinfo err] } {
    puts "\[ERROR]\ Failed in change_names!\n$err"
    exit 1
}

puts "==========================( Generating Reports )=========================="

if { [catch {check_design -summary -unmapped -cells -ports -designs -nets -tristates} err] } {
    puts "\[ERROR]\ check_design failed!\n$err"
    exit 1
}
redirect -file $REPORTS_DIR/mapped.checkdesign.rpt {check_design -unmapped -cells -ports -designs -nets -tristates}

update_timing

if { [catch {redirect -tee $REPORTS_DIR/mapped.checktiming.rpt {check_timing}} err] } {
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

redirect -file $REPORTS_DIR/mapped.timing.max.rpt {report_timing -delay max -path full -nosplit -transition_time -nets -attributes -nworst 8 -max_paths 16 -significant_digits 3 -sort_by group}
redirect -file $REPORTS_DIR/mapped.timing.min.rpt {report_timing -delay min -path full -nosplit -transition_time -nets -attributes -nworst 8 -max_paths 16 -significant_digits 3 -sort_by group}
redirect -file $REPORTS_DIR/mapped.area.rpt {report_area -nosplit}

redirect -file $REPORTS_DIR/mapped.power.rpt {report_power -nosplit -net -cell -analysis_effort medium}
redirect -file $REPORTS_DIR/mapped.power.hier.rpt {report_power -nosplit -hierarchy -levels 3 -analysis_effort medium}

report_names -rules verilog > $REPORTS_DIR/mapped.naming.verilog.rpt
print_variable_group all > $REPORTS_DIR/mapped.vars.rpt


puts "==========================( Writing Generated Netlist )=========================="

write -hierarchy -format ddc -compress gzip -output $OUTPUTS_DIR/${DESIGN_NAME}.mapped.ddc
write -hierarchy -format verilog -output $OUTPUTS_DIR/${DESIGN_NAME}.mapped.v

write_sdf -version {{settings.sdf_version}} {%if settings.sdf_inst_name is not none-%} -instance {{settings.sdf_inst_name}} {%endif-%} $OUTPUTS_DIR/${DESIGN_NAME}.mapped.sdf

set_app_var write_sdc_output_lumped_net_capacitance false
set_app_var write_sdc_output_net_resistance false
write_sdc -nosplit $OUTPUTS_DIR/${DESIGN_NAME}.mapped.sdc

write_icc2_files -force -output $OUTPUTS_DIR/icc2_files

saif_map -type ptpx -essential -write_map ${OUTPUTS_DIR}/${TOP_MODULE}.mapped.saif.ptpx.map
saif_map -write_map ${OUTPUTS_DIR}/mapped.saif.dc.map

if {[shell_is_in_topographical_mode]} {
    write_floorplan -all ${OUTPUTS_DIR}/${TOP_MODULE}.mapped.fp
    save_lib
}

change_names -rules vhdl -hierarchy
set_app_var vhdlout_dont_create_dummy_nets true
write -hierarchy -format vhdl -output $OUTPUTS_DIR/${DESIGN_NAME}.mapped.vhd
# change_names -rules verilog -hierarchy

puts "==========================( Synthesis flow completed. )=========================="

exit