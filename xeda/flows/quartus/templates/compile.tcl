set design_name           {{design.name}}
set clock_port            {{design.clock_port}}
set clock_period          {{flow.clock_period}}
set top                   {{design.top}}
set tb_top                {{design.tb_top}}
# set strategy              {{flow.strategy}}

set debug                 {{debug}}
# set run_synth_flow        {{run_synth_flow}}
# set run_postsynth_sim     {{run_postsynth_sim}}
# set optimize_power        {{flow.optimize_power}}

package require ::quartus::project
package require ::quartus::flow

project_open ${design_name}

load_package flow

puts "\n===========================( Running flow )==========================="
# runs: quartus_map, quartus_fit, quartus_asm, and quartus_sta
if {[catch {execute_flow -compile} result]} {
    puts "ERROR: Compilation failed. Result: $result. See report files.\n"
    exit 1
}


# TODO set up: verilog include-dirs, VHDL generics, verilog params,


load_package report
load_report

set panel_names [get_report_panel_names]

puts "panel_names=${panel_names}"

set reports_dir {{reports_dir}}

file mkdir $reports_dir

foreach panel_name $panel_names {

    set csv_file [string trim $panel_name]

    set csv_file [regsub -all {_*\s+_*} $csv_file _ ]
    set csv_file [regsub -all {_*\|+_*} $csv_file . ]
    set csv_file [regsub -all {_*/+_*} $csv_file {} ]
    set csv_file [regsub -all "_*\\+_*" $csv_file _ ]
    set csv_file [regsub -all {_*&+_*} $csv_file _ ]
    set csv_file [regsub -all {_*\-+_*} $csv_file {} ]
    set csv_file [regsub -all {_*`+_*} $csv_file {} ]
    set csv_file [regsub -all {_*'+_*} $csv_file {} ]
    set csv_file [regsub -all {_*"+_*} $csv_file {} ]

    set csv_file $reports_dir/$csv_file.csv

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

unload_report

project_close