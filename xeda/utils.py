import re
import csv
import importlib


def camelcase_to_snakecase(name: str) -> str:
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def snakecase_to_camelcase(name: str) -> str:
    return ''.join(word.title() for word in name.split('_'))


def load_class(full_class_string: str, defualt_module_name=None) -> type:
    cls_path_lst = full_class_string.split(".")
    assert len(cls_path_lst) > 0

    cls_name = snakecase_to_camelcase(cls_path_lst[-1])
    if len(cls_path_lst)  == 1: # module name not specified, use default
        mod_name = defualt_module_name
    else:
        mod_name = ".".join(cls_path_lst[:-1])
    assert mod_name

    module = importlib.import_module(mod_name, __package__ if mod_name.startswith('.') else None )
    return getattr(module, cls_name)


def dict_merge(base_dct, merge_dct, add_keys=True):
    rtn_dct = base_dct.copy()
    if add_keys is False:
        merge_dct = {key: merge_dct[key] for key in set(rtn_dct).intersection(set(merge_dct))}

    rtn_dct.update({
        key: dict_merge(rtn_dct[key], merge_dct[key], add_keys=add_keys)
        if isinstance(rtn_dct.get(key), dict) and isinstance(merge_dct[key], dict)
        else merge_dct[key]
        for key in merge_dct.keys()
    })
    return rtn_dct


def try_convert(s):
    if s is None:
        return 'None'
    if isinstance(s, str) and (s.startswith('"') or s.startswith('\'')):
        return s.strip('"\'')
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            s = str(s)
            if s.lower in ['true', 'yes']:
                return True
            if s.lower in ['false', 'no']:
                return False
            return s


def parse_csv(path, id_field, field_parser=(lambda x: x), id_parser=(lambda x: x), interesting_fields=None):
    data = {}

    with open(path, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if not interesting_fields:
                interesting_fields = row.keys()
            id = id_parser(row[id_field])
            data[id] = {k: field_parser(row[k]) for k in interesting_fields}
        return data
