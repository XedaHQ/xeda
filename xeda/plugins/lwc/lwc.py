import logging
import re
from xeda.flows.flow import Flow

_logger = logging.getLogger()

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

    @classmethod
    def wrap_design(cls, design_settings):
        lwc = design_settings.get('lwc', {})
        lwc_wrapper = lwc.get('wrapper')
        if lwc_wrapper:
            two_pass = lwc.get('two_pass')
            for section in 'rtl', 'tb':
                for k,v in lwc_wrapper.get(section, {}).items():
                    if k == 'sources':
                        _logger.info(f"Extending design.{section}.{k} with sources from design.lwc.wrapper.{section}.{k}")
                        design_settings[section][k] += [x for x in v if x not in design_settings[section].get(k, {})]
                    else:
                        _logger.info(f"Replacing design.{section}.{k} with design.lwc.wrapper.{section}.{k}={v}")
                        design_settings[section][k] = v
