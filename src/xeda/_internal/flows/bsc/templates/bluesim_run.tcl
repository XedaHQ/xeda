# Xeda's own script. A return from `sim run` or `sim step` is a checkpoint. It does not prove that
# the HDL finished.
set events [open bluesim_events.jsonl {WRONLY CREAT EXCL}]
close $events
# The kernel counts unitless ticks (10 per clock cycle). The end record has no simulated time,
# because the tick has no physical unit. The timescale below only labels the waveform.
sim timescale "1 us / 1 us"
{% if settings.max_cycles is not none %}
sim step {{ settings.max_cycles }}
{% else %}
sim run
{% endif %}
# bsc 2026.07.1 src/comp/bluetcl.hs/getClockInfo: the cycle count is field 7, the maximum of the
# native rising and falling counts.
set clock [lindex [sim clock] 0]
set cycles [lindex $clock 7]
if {![string is entier -strict $cycles]} {
    error "Invalid Bluesim clock checkpoint"
}
set ending unknown
if {[sim isfatal]} {set ending fatal}
{% if settings.max_cycles is not none %}
if {$ending eq "unknown" && $cycles == {{ settings.max_cycles }}} {set ending max_cycles}
{% endif %}
set fd [open bluesim_end.json {WRONLY CREAT EXCL}]
puts $fd [format {{ '{"ended_by":"%s","cycles":%s,"events":[]}' | tcl_word }} $ending $cycles]
close $fd
