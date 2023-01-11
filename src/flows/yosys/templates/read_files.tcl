yosys -import
{% if settings.debug or settings.verbose > 1 %}
yosys echo on
{% endif %}

{% set sv_files = design.sources_of_type("SystemVerilog", rtl=true, tb=false) %}
{% if sv_files %}
yosys plugin -i systemverilog
{% endif %}

{% for src in design.rtl.sources %}
{% if src.type.name == "Verilog" %}
yosys log -stdout "Reading {{src}}"
## -Dname=value -Idir
yosys read_verilog -defer {{settings.read_verilog_flags|join(" ")}} {{defines|join(" ")}} {{src}}
{% elif src.type.name == "SystemVerilog" %}
yosys log -stdout "Reading {{src}}"
yosys read_systemverilog -defer {{settings.read_systemverilog_flags|join(" ")}} {{src}}
{% endif %}
{% endfor %}

{% set vhdl_files = design.sources_of_type("Vhdl", rtl=true, tb=false) %}
{% if vhdl_files %}
yosys log -stdout "Elaborating VHDL files"
yosys plugin -i ghdl
yosys ghdl {{ghdl_args|join(" ")}}
{% endif %}

{% if settings.liberty is defined %}
{% for lib in settings.liberty %}
yosys read_liberty -lib {{lib}}
{% endfor %}
{% endif %}

{% for src in settings.verilog_lib %}
yosys read_verilog -lib {{src}}
{% endfor %}

{% for key, value in parameters.items() %}
yosys chparam -set {{key}} {{value}} {% if design.rtl.top %} {{design.rtl.top}} {% endif %}
{% endfor %}

{% if settings.clockgate_map %}
yosys read_verilog -defer {{settings.clockgate_map}}
{% endif %}

{% if sv_files %}
yosys read_systemverilog -link
{% endif %}

yosys hierarchy -check {% if design.rtl.top %} -top {{design.rtl.top}} {% else %} -auto-top {% endif %}

{% for mod in settings.keep_hierarchy %}
yosys select -module {{mod}}
yosys setattr -mod -set keep_hierarchy 1
yosys select -clear
{% endfor %}

{% for mod in settings.black_box %}
puts "Converting module {{mod}} into blackbox"
yosys blackbox {{mod}}
{% endfor %}

{% for attr, value in settings.set_attribute.items() %}
{% if value is mapping %}
{% for path, v in value.items() %}
yosys setattr -set {{attr}} {{v}} {{path}}
{% endfor %}
{% else %}
yosys setattr -set {{attr}} {{value}}
{% endif %}
{% endfor %}
{% for attr, value in settings.set_mod_attribute.items() %}
{% if value is mapping %}
{% for path, v in value.items() %}
yosys setattr -mod -set {{attr}} {{v}} {{path}}
{% endfor %}
{% else %}
yosys setattr -mod -set {{attr}} {{value}}
{% endif %}
{% endfor %}

yosys check -initdrv -assert
