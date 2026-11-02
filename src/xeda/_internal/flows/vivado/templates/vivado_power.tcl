set_param tclapp.enableGitAccess 0

puts "\n================================( Opening routed design from checkpoint )================================="
open_checkpoint {{checkpoint|tcl_word}}

puts "\n================================( Reporting power from {{saif_file|tcl_quote}} )================================="
reset_switching_activity -all
read_saif -verbose {{saif_file|tcl_word}}
report_power -hier all -format xml -verbose -file {{settings.power_report_xml|tcl_word}}
