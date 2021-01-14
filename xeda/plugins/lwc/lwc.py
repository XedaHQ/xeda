import re
from xeda.flows.flow import Flow

class LWC:
    @classmethod
    def variant(cls, design_settings):
        design_name = design_settings['name']
        lwc_settings = design_settings.get('lwc', {})
        lwc_variant = lwc_settings.get('variant')
        if not lwc_variant:
            name_splitted = design_name.split('-')
            assert len(
                name_splitted) > 1, "either specify design.lwc.variant or design.name should be ending with -v\d+"
            lwc_variant = name_splitted[-1]
            assert re.match(
                r'v\d+', lwc_variant), "either specify design.lwc.variant or design.name should be ending with -v\d+"

        return lwc_variant

    @classmethod
    def supports_hash(cls, design_settings):
        lwc_settings = design_settings.get('lwc', {})
        algorithms = lwc_settings.get('algorithm')
        if lwc_settings.get('supports_hash'):
            return True
        return (algorithms and (isinstance(algorithms, list) or isinstance(algorithms, tuple)) and len(algorithms) > 1)
