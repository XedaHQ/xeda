# Owned execution: return from run/step is a checkpoint, never proof of HDL finish.
set events [open bluesim_events.jsonl {WRONLY CREAT EXCL}]
close $events
# Retain the native kernel's default unit while making the observation unit explicit.
sim timescale "1 us / 1 us"
{% if settings.max_cycles is not none %}
sim step {{ settings.max_cycles }}
{% else %}
sim run
{% endif %}
set time [sim time]
set unit 1us
# bsc 2026.07.1 src/comp/bluetcl.hs/getClockInfo: cycle count is field 7 (max of native rising/falling counts).
set clock [lindex [sim clock] 0]
set cycles [lindex $clock 7]
if {![string is entier -strict $time] || ![string is entier -strict $cycles]} {
    error "Invalid Bluesim time/clock checkpoint"
}
set ending unknown
if {[sim isfatal]} {set ending fatal}
{% if settings.max_cycles is not none %}
if {$ending eq "unknown" && $cycles == {{ settings.max_cycles }}} {set ending max_cycles}
{% endif %}
set fd [open bluesim_end.json {WRONLY CREAT EXCL}]
puts $fd [format {{ '{"ended_by":"%s","time":%s,"time_unit":"%s","cycles":%s,"events":[]}' | tcl_word }} $ending $time $unit $cycles]
close $fd
