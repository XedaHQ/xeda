set SCRIPTS_DIR [file dirname [info script]]

set RESULTS_DIR {{settings.outputs_dir}}
set REPORTS_DIR {{settings.reports_dir}}

set TOP_MODULE {{design.rtl.top}}

set OPTIMIZATION "area"

set TARGET_LIBRARY_FILES "{{settings.target_libraries | join(' ')}}"


if { [file exists $RESULTS_DIR] } {
    file delete -force $RESULTS_DIR
}
if { [file exists $REPORTS_DIR] } {
    file delete -force $REPORTS_DIR
}
file mkdir $RESULTS_DIR
file mkdir $REPORTS_DIR


puts "Optimization: $OPTIMIZATION"

# if MAX_AREA TCL variable does not exist and  OPTIMIZATION == area set it to 0.0
if { $OPTIMIZATION == "area" && ![info exists MAX_AREA] } {
    set MAX_AREA 0.0
}

set compile_command "compile_ultra"


if { [info exists ::env(COMPILE_ARGS)] } {
    set compile_options $::env(COMPILE_ARGS)
} else {
    set compile_options {}

    if { $compile_command == "compile" } {
        if { $OPTIMIZATION == "area" } {
            set compile_options { {*}$compile_options -area_effort high -map_effort high -auto_ungroup area -boundary_optimization}
        } elseif { $OPTIMIZATION == "timing" } {
            set compile_options { {*}$compile_options -area_effort medium -map_effort high -auto_ungroup area -boundary_optimization}
        } elseif { $OPTIMIZATION == "power" } {
            set compile_options {-area_effort high -map_effort high -auto_ungroup area -boundary_optimization -gate_clock -power_effort high}
        } else {
            puts "Unknown optimization: $OPTIMIZATION"
            exit 1
        }
    } elseif { $compile_command == "compile_ultra" } {
        if [shell_is_in_topographical_mode] then {
            set compile_options { {*}$compile_options -spg}
        }
        if { $OPTIMIZATION == "area" } {
        } elseif { $OPTIMIZATION == "timing" } {
            set compile_options { {*}$compile_options -retime}

        } elseif { $OPTIMIZATION == "power" } {
            set compile_options { {*}$compile_options -gate_clock}
            if [shell_is_in_topographical_mode] then {
                set compile_options { {*}$compile_options -self_gating}
            }
        } else {
            puts "Unknown optimization: $OPTIMIZATION"
            exit 1
        }
    } else {
        puts "Unknown compile command: $compile_command"
    }
}

# set_app_var search_path ". $search_path"

if { [shell_is_dcnxt_shell] } {
    if { $OPTIMIZATION == "area" } {
        set_app_var compile_high_effort_area true
        set_app_var compile_optimize_netlist_area true
    } elseif { $OPTIMIZATION == "timing" } {
        set_app_var compile_timing_high_effort true
    }
}

set_app_var target_library ${TARGET_LIBRARY_FILES}
set_app_var synthetic_library {dw_foundation.sldb}
set_app_var link_library "* $synthetic_library $target_library"

# puts "\n========== Removing existing design(s) =========="
remove_design -all


set SOURCE_FILES { {{- design.rtl.sources | join(' ') -}} }

foreach src $SOURCE_FILES {	;# Now loop and print...
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
    puts "=================== Analysing $src ($format) ==================="
    if  { [ analyze -format $format $src ] != 1 } {
        puts stderr "\[ERROR]\ Analysing $format file $src failed!\n"
        exit 1
    }
}


puts "\n=================== Elaborating design ${TOP_MODULE} ==================="
if { [elaborate -library WORK ${TOP_MODULE}] != 1 } {
    puts stderr "\[ERROR]\ Elaborating design ${TOP_MODULE} failed!\n"
    exit 1
}

if { [catch {current_design ${TOP_MODULE} } $err] } {
    puts stderr "\[ERROR]\ Setting current design to ${TOP_MODULE} failed!\n$err"
    exit 1
}

if { [info exists MAX_AREA] } {
    set_max_area ${MAX_AREA}
}

puts "\n=================== Linking design ==================="
if { [link] != 1 } {
    puts stderr "\[ERROR]\ Linking design failed!\n"
    exit 1
}

puts "\n========= Uniquify design ========="
if { [catch {uniquify} err] } {
    puts stderr "\[ERROR]\ Uniquify failed!\n$err"
    exit 1
}

check_design -summary

puts "\n========= Loading the constraints ========="
{% for constraint_file in settings.sdc_files -%}
    puts "Loading constraints file: {{constraint_file}}"
    if { [catch {source {{constraint_file}}} err] } {
        puts stderr "\[ERROR]\ Loading constraints file {{constraint_file}} failed!\n$err"
        exit 1
    }
{% endfor -%}

if {[info exists MAX_AREA]} {
    set_max_area ${MAX_AREA}
}

puts "\n========= Synthesize the design: ${compile_command} ========="
if { [catch {${compile_command} {*}$compile_options } err] } {
    puts stderr "\[ERROR]\ Compile failed!\n$err"
    exit 1
}

puts "\n========= Optimize Design ========="
if { $OPTIMIZATION == "area" } {
    optimize_netlist -area
} else {
    optimize_netlist
}

check_design -unmapped -cells -ports -designs -nets -tristates -html_file_name $REPORTS_DIR/design_summary.html

if { [catch {check_design -summary -unmapped -cells -ports -designs -nets -tristates} err] } {
    puts stderr "\[ERROR]\ check_design failed!\n$err"
    exit 1
}

if { [catch {check_timing} err] } {
    puts stderr "\[ERROR]\ check_timing failed!\n$err"
    exit 1
}

################################################################################
#                             Generate Reports
################################################################################

redirect -tee $REPORTS_DIR/design.rpt {report_design -nosplit}

# library units
puts "========================== Units =========================="
redirect -tee $REPORTS_DIR/units.rpt {report_units}

redirect $REPORTS_DIR/vars.rpt {report_app_var}

report_constraint -nosplit -all_vio -significant_digits 3 > $REPORTS_DIR/constraints.rpt

# report cell usage
report_reference -nosplit > $REPORTS_DIR/reference.rpt

report_cell > $REPORTS_DIR/cell.rpt

# Quality of Results
report_qor -nosplit > $REPORTS_DIR/qor.rpt

report_auto_ungroup -nosplit -nosplit > $REPORTS_DIR/auto_ungrou.rpt

puts "====================== Timing Reports ======================"
redirect -tee $REPORTS_DIR/timing.max.rpt {report_timing -delay max -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}
redirect -tee $REPORTS_DIR/timing.min.rpt {report_timing -delay min -path full -nosplit -transition_time -nets -attributes -nworst 1 -max_paths 1 -significant_digits 3 -sort_by group}

puts "======================== Area Report ======================="
redirect -tee $REPORTS_DIR/area.rpt {report_area -nosplit}

puts "======================= Power Report ======================="
redirect -tee $REPORTS_DIR/power.rpt {report_power -nosplit -net -cell -analysis_effort medium}

report_clock > $REPORTS_DIR/clocks.rpt
if {[sizeof_collection [all_clocks]]>0} {
   report_net [all_clocks] > $REPORTS_DIR/clock_nets.rpt
}

###################################################################
#                  Write resulting netlist
###################################################################

write -format ddc -hierarchy -compress gzip -output $RESULTS_DIR/${TOP_MODULE}.ddc
write -format verilog -output $RESULTS_DIR/${TOP_MODULE}_netlist.v
write -format vhdl -output $RESULTS_DIR/${TOP_MODULE}_netlist.vhdl

write_sdf -version 2.1 $RESULTS_DIR/${TOP_MODULE}.sdf

