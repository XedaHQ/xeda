puts "\n================================( Opening routed design from checkpoint )================================="
open_checkpoint {{checkpoint}}

puts "\n================================( Reporting power from {{settings.saif}} )================================="
reset_switching_activity -all
eval read_saif -no_strip -strip_path {{self.design.tb.top[0]}}/{{design.tb.uut}} -verbose {{settings.saif}}
report_power -hier all -format xml -verbose -file {{settings.power_report_xml}}
