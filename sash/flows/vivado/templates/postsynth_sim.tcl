{% include 'interface.tcl' %}

{% include 'run_synth.tcl' %}

set post_synth_sim "true"

{% include 'run_sim.tcl' %}

if {${gen_saif} && false} {
    if {!$run_synth_flow} {
        open_checkpoint ${checkpoints_dir}/post_route.dcp
    }

    eval read_saif ${saif_file}

    report_power -file ${reports_dir}/post_route/post_synth_power.rpt
}