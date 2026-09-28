{%- for file in user_hooks %}
source {{file|tcl_word}}
{%- endfor %}

{#- Vivado's write_bitstream step writes the bitstream into the implementation run's directory,
    the working directory here, named after the design's top, with a .bin beside it when the
    step's BIN_FILE argument is set. #}
set xeda_top [get_property TOP [current_design]]
puts "\n=======================( Copying the bitstream to {{outputs.bitstream|tcl_quote}} )========================"
file mkdir [file dirname {{outputs.bitstream|tcl_word}}]
file copy -force ${xeda_top}.bit {{outputs.bitstream|tcl_word}}
if {[file exists ${xeda_top}.bin]} {
  file copy -force ${xeda_top}.bin {{bin_file|tcl_word}}
}
