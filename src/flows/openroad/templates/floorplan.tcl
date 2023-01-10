{% if settings.floorplan_def %}
# Initialize floorplan by reading in floorplan DEF
puts "Read in Floorplan DEF to initialize floorplan: {{settings.floorplan_def}}"
read_def -floorplan_initialize {{settings.floorplan_def}}

{% elif settings.footprint %}
# Initialize floorplan using ICeWall footprint
ICeWall load_footprint {{settings.footprint}}
initialize_floorplan \
  -die_area  [ICeWall get_die_area] \
  -core_area [ICeWall get_core_area] \
  -site      {{platform.place_site}}
ICeWall init_footprint {{settings.sig_map_file}}

{% elif settings.core_utilization %}
# Initialize floorplan using core_utilization
initialize_floorplan -utilization {{settings.core_utilization}} \
  -aspect_ratio {{settings.core_aspect_ratio}} \
  -core_space  {{settings.core_margin}} \
  -site {{platform.place_site}}

{% elif settings.core_area and settings.die_area %}
# Initialize floorplan using DIE_AREA/CORE_AREA
initialize_floorplan -die_area {{settings.die_area|join(" ")|embrace}} \
  -core_area {{settings.core_area|join(" ")|embrace}} \
  -site {{platform.place_site}}
{% endif %}

{% set make_tracks_tcl = settings.make_tracks_tcl or platform.make_tracks_tcl %}
{% if make_tracks_tcl %}
source {{make_tracks_tcl}}
{% else %}
make_tracks
{% endif %}

{% if settings.footprint_tcl %}
source {{settings.footprint_tcl}}
{% endif %}

# remove buffers inserted by yosys/abc
remove_buffers

{{ write_checkpoint(step, sdc=true)}}
