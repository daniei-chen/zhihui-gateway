"""One output-limit contract before protocol conversion and budget admission."""
from __future__ import annotations


FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')
PROTOCOL_FIELDS = {'openai': ('max_tokens', 'max_completion_tokens'),
                   'anthropic': ('max_tokens',), 'responses': ('max_output_tokens',)}


class Invalid(ValueError):
    def __init__(self, message, code='invalid_max_tokens'):
        super().__init__(message)
        self.code = code


def inspect(body: dict, protocol='openai') -> tuple[str, tuple[str, ...], int | None]:
    allowed = PROTOCOL_FIELDS[protocol]
    present = tuple(name for name in FIELDS if name in body)
    if any(name not in allowed for name in present):
        raise Invalid('该协议不支持此输出上限字段，请使用对应协议字段', 'unsupported_output_limit')
    for name in present:
        if type(body[name]) is not int or body[name] <= 0:
            raise Invalid('输出上限必须为正整数')
    if len(present) > 1 and len({body[name] for name in present}) != 1:
        raise Invalid('多个输出上限字段值不一致，请只传一个或使用相同值', 'conflicting_output_limits')
    field = 'max_completion_tokens' if 'max_completion_tokens' in present else (present[0] if present else allowed[0])
    return field, present, body[field] if present else None


def apply(body: dict, limit: int) -> None:
    field, present, _ = inspect(body)
    for name in present or (field,):
        body[name] = limit
