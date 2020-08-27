
# © 2020 [Kamyar Mohajerani](malto:kamyar@ieee.org)

# These settings are set by SASSYN
set design_name         {{design.name}}
set vhdl_std            {{design.vhdl_std}}
set vhdl_files          "{{design.sources|join(' ')}}"
set sim_vhdl_files      "{{design.tb_sources|join(' ')}}"
set clock_port          {{design.clock_port}}
set top                 {{design.top}}

# Area/Balanced/Timing
set base_strategy       {{flow.strategy}}
set synth_engine        {{flow.synthesis_engine}}
set clock_period        {{flow.clock_period}}
set fpga_part           "{{flow.fpga_part}}"
set implementation_name "{{flow.impl_name}}"
set impl_dir            "{{flow.impl_folder}}"


#######################################

set sdc_file             "${top}.sdc"
set ldc_file             "${top}.ldc"



file delete -force ${impl_dir}

eval prj_project new -name ${design_name} -dev ${fpga_part} -impl ${implementation_name} -impl_dir ${impl_dir}

eval prj_src add -format VHDL ${vhdl_files}
eval prj_src add -format VHDL ${sim_vhdl_files} -simulate_only



set file [open ${sdc_file} w]
puts $file "create_clock -period ${clock_period} -name clock \[get_ports ${clock_port}\]"
close $file
set file [open ${ldc_file} w]
puts $file "create_clock -period ${clock_period} -name clock \[get_ports ${clock_port}\]"
close $file

eval prj_src add ${sdc_file}
eval prj_src add ${ldc_file}

##strategy
prj_strgy copy -from ${base_strategy} -name custom_strategy -file diamond_strategy.sty

if {${vhdl_std} == "08"} {
  prj_strgy set_value -strategy custom_strategy syn_vhdl2008=True
  prj_strgy set_value -strategy custom_strategy lse_vhdl2008=True
}


# Synplify options
##Prioritize area over timing 
prj_strgy set_value -strategy custom_strategy syn_area=True
prj_strgy set_value -strategy custom_strategy syn_frequency= 

if {${base_strategy} == "Timing"} {
  prj_strgy set_value -strategy custom_strategy {syn_pipelining_retiming=Pipelining and Retiming}
}

if {${base_strategy} == "Area"} {
  prj_strgy set_value -strategy custom_strategy syn_area=True
}
#syn_use_clk_for_uncons_io=True

# LSE options
## LWC specific (only LSE supports them)
# lse_disable_distram=False ?
prj_strgy set_value -strategy custom_strategy lse_dsp_style=Logic lse_dsp_util=0 lse_ebr_util=0 lse_rom_style=Logic
#lse_ram_style=Distributed


# prj_strgy set_value -strategy custom_strategy {par_cmdline_args=-exp nbrMaxRunTime=100}


prj_strgy set custom_strategy
###########



prj_impl option top ${top}
prj_impl option synthesis ${synth_engine}
prj_syn set ${synth_engine}

prj_project save
###################

# prj_run PAR

puts "\n====================( Synthesize Design )===================="
eval prj_run Synthesis -impl ${implementation_name} -forceAll

puts "\n====================( Translate Design )===================="
eval prj_run Translate -impl ${implementation_name}
puts "\n====================( Map Design )===================="
eval prj_run Map -impl ${implementation_name} -forceAll

puts "\n====================( Place & Route Design )===================="
eval prj_run PAR -impl ${implementation_name} -forceAll
# eval prj_run PAR -impl ${implementation_name} -task IOTiming -forceOne

if {false} {
  puts "\n====================( Export Files )===================="
  eval prj_run Export -impl ${implementation_name}
}

prj_project close
