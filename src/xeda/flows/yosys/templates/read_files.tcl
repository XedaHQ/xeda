yosys -import
{% if settings.debug or settings.verbose > 1 -%}
yosys echo on
{% endif -%}

{% set include_dirs=namespace(i=[]) %}
{% for src in design.rtl.sources if src.type is not none and src.type.name in ("VerilogHeader", "SVHeader") -%}
{% set flag = ("-I" ~ src.path.parent)|verbatim_path -%}
{% if flag not in include_dirs.i -%}{% set include_dirs.i = include_dirs.i + [flag] %}{% endif -%}
{% endfor -%}

{% for plugin in settings.plugins -%}
yosys plugin -i {{plugin|tcl_word}}
{% endfor -%}

{% set sv_files = design.sources_of_type("SystemVerilog", rtl=true, tb=false) -%}
{% set uhdm_plugin = (settings.systemverilog == "uhdm") and sv_files -%}
{% if uhdm_plugin and "systemverilog" not in settings.plugins -%}
yosys plugin -i systemverilog
{% endif -%}
{% set slang_plugin = (settings.systemverilog == "slang") and sv_files -%}
{% if slang_plugin and settings.use_slang_plugin and "slang" not in settings.plugins -%}
yosys plugin -i slang
{% endif -%}

{% for src in design.rtl.sources -%}
{% if src.type is not none -%}
    {% if src.type.name == "Verilog" -%}
yosys log -stdout "** Reading {{src|tcl_quote}} **"
yosys read_verilog -defer {{settings.read_verilog_flags|join(" ")}} {{defines|join(" ")}} {{include_dirs.i|join(" ")}} {{src|read_path}}
    {% elif src.type.name == "SystemVerilog" %}
yosys log -stdout "** Reading {{src|tcl_quote}} **"
        {%- if slang_plugin %}
yosys read_slang --extern-modules --best-effort-hierarchy {{defines|join(" ")}} {{include_dirs.i|join(" ")}} {{src|verbatim_path}}
        {%- elif uhdm_plugin %}
yosys read_systemverilog -defer {{settings.read_systemverilog_flags|join(" ")}} {{src|verbatim_path}}
        {%- else %}
yosys read_verilog -sv -defer {{settings.read_verilog_flags|join(" ")}} {{defines|join(" ")}} {{include_dirs.i|join(" ")}} {{src|read_path}}
        {%- endif %}
    {% endif -%}
{% endif -%}
{% endfor -%}

{% set vhdl_files = design.sources_of_type("Vhdl", rtl=true, tb=false) | list -%}
{% if vhdl_files -%}
yosys log -stdout "** Elaborating VHDL files **"
yosys plugin -i ghdl
yosys ghdl {{ghdl_args|map("ghdl_arg")|join(" ")}} {{vhdl_files|map("verbatim_path")|join(" ")}} -e {% if design.rtl.top -%} {{design.rtl.top}} {%- endif %}
{% endif -%}

{% if settings.liberty is defined -%}
{% for lib in settings.liberty -%}
yosys read_liberty -lib {{lib|read_path}}
{% endfor -%}
{% endif -%}

{% for src in settings.verilog_lib -%}
yosys read_verilog -lib {{src|read_path}}
{% endfor -%}

{#- a VHDL top got its generics from GHDL, and has no parameters left #}
{% if not top_is_vhdl() -%}
{% for key, value in parameters.items() -%}
yosys chparam -set {{key}} {{value|esc}} {% if design.rtl.top -%} {{design.rtl.top}} {%- endif %}
{% endfor -%}
{% endif -%}

{% if settings.clockgate_map is defined and settings.clockgate_map -%}
yosys read_verilog -defer {{settings.clockgate_map|read_path}}
{% endif -%}

{% if uhdm_plugin -%}
yosys read_systemverilog -link
{% endif -%}
yosys hierarchy -check {% if design.rtl.top -%} -top {{design.rtl.top}} {% else %} -auto-top {%- endif %}
{% for mod in settings.black_box -%}
puts "Converting module {{mod|tcl_quote}} into blackbox"
yosys blackbox {{mod}}
{% endfor -%}

{% for attr, value in settings.set_attribute.items() -%}
{% if value is mapping -%}
{% for path, v in value.items() -%}
yosys setattr -set {{attr}} {{v|esc}} {{path}}
{% endfor -%}
{% else %}
yosys setattr -set {{attr}} {{value|esc}}
{% endif -%}
{% endfor -%}
{% for attr, value in settings.set_mod_attribute.items() -%}
{% for path, v in value.items() -%}
yosys setattr -mod -set {{attr}} {{v|esc}} {{path}}
{% endfor -%}
{% endfor -%}

yosys check -initdrv
