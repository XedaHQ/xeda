set project_name          {{design.name}}
set top                   {{design.rtl.top}}

package require ::quartus::project
package require ::quartus::flow

project_open ${project_name}

load_package flow

puts "\n===========================( Running compile flow )==========================="
# runs: quartus_map, quartus_fit, quartus_asm, and quartus_sta
if {[catch {execute_flow -compile} result]} {
    puts "ERROR: Compilation failed. Result: $result. See report files.\n"
    qexit -error
}

puts "clocks: [get_clocks]"

# TODO set up: verilog include-dirs, VHDL generics, verilog params,

load_package report
load_report


set panel "Timing Analyzer||Setup Summary"
set panel_id [get_report_panel_id $panel]

# negative if panel not found
if {$panel_id > 0} {   
    set setup_slack [get_report_panel_data -col_name Slack -row 1 -id $panel_id]
    puts ""
    puts "-----------------------------------------------------"
    puts "Setup slack: $setup_slack"
    puts "-----------------------------------------------------"
    puts ""
    if {$setup_slack<0} {
        puts "\[ERROR\] Timing not met!"
        # qexit -error
    }
}


set panel_names [get_report_panel_names]

puts "panel_names=${panel_names}"

foreach panel_name $panel_names {

    set csv_file [string trim $panel_name]

    set csv_file [regsub -all {_*\s+_*} $csv_file _ ]
    set csv_file [regsub -all {_*/+_*} $csv_file {} ]
    set csv_file [regsub -all {_*\|+_*} $csv_file / ]
    set csv_file [regsub -all "_*\\+_*" $csv_file _ ]
    set csv_file [regsub -all {_*&+_*} $csv_file _ ]
    set csv_file [regsub -all {_*\-+_*} $csv_file {} ]
    set csv_file [regsub -all {_*`+_*} $csv_file {} ]
    set csv_file [regsub -all {_*'+_*} $csv_file {} ]
    set csv_file [regsub -all {_*"+_*} $csv_file {} ]

    set csv_file {{settings.reports_dir}}/$csv_file.csv

    set csv_file_dir [file dirname ${csv_file}]

    file mkdir $csv_file_dir

    puts "Saving $panel_name to $csv_file"

    set fh [open $csv_file w]
    set num_rows [get_number_of_rows -name $panel_name]
    # Go through all the rows in the report file, including the
    # row with headings, and write out the comma-separated data
    for { set i 0 } { $i < $num_rows } { incr i } {
        set row_data [get_report_panel_row -name $panel_name -row $i]
        puts $fh [join $row_data ","]
    }
    close $fh
}


# if {[catch {execute_flow -check_netlist} result]} {
#     puts "ERROR: Check netlist failed. Result: $result. See report files.\n"
#     exit 1
# }


# if {[catch {execute_flow -generate_functional_sim_netlist} result]} {
#     puts "ERROR: Compilation failed. Result: $result. See report files.\n"
#     exit 1
# }

# if {[catch {execute_flow -compile_and_simulate} result]} {
#     puts "ERROR: Compilation failed. Result: $result. See report files.\n"
#     exit 1
# }


unload_report

project_close