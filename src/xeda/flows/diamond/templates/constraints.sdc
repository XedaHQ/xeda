create_clock -period {{ "%.3f"|format(settings.main_clock.period) }} -name {{ settings.main_clock.name or "clock" }} [get_ports {{settings.main_clock.port}}]
