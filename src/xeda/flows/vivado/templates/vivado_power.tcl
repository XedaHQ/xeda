puts "\n================================( Opening routed design from checkpoint )================================="
open_checkpoint {{checkpoint}}

puts "\n================================( Reporting power from {{settings.saif}} )================================="
reset_switching_activity -all
eval read_saif -verbose {{saif_file}}
report_power -hier all -format xml -verbose -file {{settings.power_report_xml}}
