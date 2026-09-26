{% include 'read_files.tcl' %}
yosys check -initdrv -assert

{% if settings.prep is not none -%}
yosys prep {% if settings.flatten %} -flatten {% endif %} {{settings.prep|join(" ")}}
{% else -%}
yosys proc
{% if settings.flatten -%}
yosys flatten
{% endif %}
{% endif -%}
yosys check {% if settings.check_assert %} -assert {% endif %}

{% include 'post_rtl.tcl' %}

yosys log -stdout "Writing CXXRTL output to: {{cxxrtl_filename|tcl_quote}}"
yosys write_cxxrtl {%- if settings.cxxrtl.header %} -header {%- endif %} {%- if not settings.cxxrtl.flatten %} -noflatten {%- endif %} {%- if not settings.cxxrtl.hierarchy %} -nohierarchy {%- endif %} {%- if not settings.cxxrtl.proc %} -noproc {%- endif %} {%- if settings.cxxrtl.debug is not none %} -g{{settings.cxxrtl.debug}} {%- endif %} {%- if settings.cxxrtl.opt is not none %} -O{{settings.cxxrtl.opt}} {%- endif %} {%- if settings.cxxrtl.namespace %} -namespace {{settings.cxxrtl.namespace|tcl_word}} {%- endif %} {{cxxrtl_filename|path}}
