"""Only expose explicit, consistent native context defaults and choices."""


def optional_fields(ceiling, window):
    if not isinstance(window, dict):
        return {}
    ceiling = ceiling if type(ceiling) is int and ceiling > 0 else 0
    raw = window.get('supportedLengths')
    values, conflict = set(), False
    if isinstance(raw, list):
        for value in raw[:64]:
            if type(value) is int and value > 0:
                if not ceiling or value <= ceiling:
                    values.add(value)
                else:
                    conflict = True
    options = sorted(values)
    default = window.get('defaultLength')
    result = {}
    if options:
        result['context_length_options'] = options
    if type(default) is int and default > 0:
        if (not ceiling or default <= ceiling) and (not options or default in values):
            result['default_context_length'] = default
        else:
            conflict = True
    if conflict:
        result['context_window_conflict'] = True
    return result
