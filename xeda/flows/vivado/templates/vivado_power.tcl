
open_checkpoint {{checkpoint}}

reset_switching_activity -all

eval read_saif  -verbose  -out_file power_saif_read.log {{saif_file}}

report_power -hier all -advisory -format xml -verbose -file {{power_report_file}}
