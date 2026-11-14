"""Finite retained/output budgets shared by the three inference protocols."""
import json
import os
from functools import wraps

FIELDS = ('max_tokens','max_completion_tokens','max_output_tokens')
PROTOCOL_FIELDS = {'openai': ('max_tokens','max_completion_tokens'),
                   'anthropic': ('max_tokens',), 'responses': ('max_output_tokens',)}


class Invalid(ValueError):
    def __init__(self,message,code='invalid_max_tokens'):
        super().__init__(message)
        self.code=code


def inspect(body: dict, protocol='openai'):
    allowed=PROTOCOL_FIELDS[protocol]
    present=tuple(name for name in FIELDS if name in body)
    if any(name not in allowed for name in present):
        raise Invalid('该协议不支持此输出上限字段，请使用对应协议字段','unsupported_output_limit')
    for name in present:
        if type(body[name]) is not int or body[name]<=0:raise Invalid('输出上限必须为正整数')
    if len(present)>1 and len({body[name] for name in present})!=1:
        raise Invalid('多个输出上限字段值不一致，请只传一个或使用相同值','conflicting_output_limits')
    field='max_completion_tokens' if 'max_completion_tokens' in present else (present[0] if present else allowed[0])
    return field,present,body[field] if present else None


def apply(body: dict, limit: int):
    field,present,_=inspect(body)
    for name in present or (field,):body[name]=limit


class OutputLimitExceeded(ValueError):
    pass


FAILURE_ENVELOPE_BYTES=4096


def output_limit_failure(protocol, *, response_id='', created_at=0, model='', request_id=None, upstream_request_id=None):
    """A separate failure envelope stays bounded even if model/headers are huge."""
    def bounded(value):return value[:64] if isinstance(value,str) else ''
    error={'code':'upstream_output_limit','message':'Upstream output exceeds the configured byte limit'}
    if protocol=='responses':
        payload={'type':'response.failed','response':{'id':bounded(response_id),'object':'response',
            'created_at':created_at,'status':'failed','model':bounded(model),'output':[],'error':error}}
    else:payload={'type':'error','error':dict(error,type='api_error')}
    if request_id:payload['request_id']=bounded(request_id)
    if upstream_request_id:payload['upstream_request_id']=bounded(upstream_request_id)
    return payload


def maximum_bytes():
    try:value=int(os.environ.get('WB_MAX_OUTPUT_BYTES','4194304'))
    except (TypeError,ValueError):value=4194304
    return max(65536,min(value,16 << 20))


#: 一个 token 折算成多少输出字节。取四 —— 中英混排 + SSE 事件包装（`data: {...}`、
#: 换行、字段名）之后，每个 token 的**线上字节**普遍在二到四之间。用四偏保守。
BYTES_PER_OUTPUT_TOKEN = 4

#: 输出承诺的**下限**：2 MiB。
#:
#: 这是记账下限，不是输出上限 —— 它不限制任何回答能有多长（那由
#: `WB_MAX_OUTPUT_BYTES` 决定，生产是 16 MiB）。
#:
#: 为什么不能更小：一次声明八千 token 的回答实际可能产出几兆字节（多字节字符、
#: SSE 事件包装），按几十千字节记账会让几个并发的长回答一起把预算撑爆，反而触发
#: 拒绝。2 MiB 覆盖绝大多数正常回答，同时仍比"一律按上限"省八倍。
DECLARED_OUTPUT_FLOOR_BYTES = 2 << 20


def declared_output_bytes(body, protocol='openai') -> int:
    """这个请求**可能**产出多少输出字节（用于容量预留）。

    依据是客户端声明的输出上限（`max_tokens` / `max_completion_tokens` /
    `max_output_tokens`）。

    ## 这是记账，不是限制

    返回值只用于**容量预留**，从不参与截断：真正能输出多少由 `WB_MAX_OUTPUT_BYTES`
    决定（超了会被裁，并在响应里给明确错误）。所以把这里的值算小，不会让用户的
    回答变短 —— 它只影响"同时能放几个请求进来"。

    ## 为什么按声明值算

    原先每个请求都按**全局上限**预留。那对一次一万六千 token 的长回答是合理的，
    但对 `max_tokens: 100` 的短问答就是三十多兆的空占，而它直接决定并发。

    ## 兜底策略

    声明不可信时一律退回上限：没声明、非法值、多字段冲突。换句话说，
    **只有明确且合理的小值才能省下预算**，其余全部保守处理 —— 不能让一个
    自称"只要十个 token"的请求把预算骗过去。

    ## 下限为什么是 2 MiB 而不是几千字节

    下限太小会伤到长回答：一个声明八千 token 的回答实际可能产出几兆字节（多字节
    字符、SSE 事件包装），按几十千字节记账会让几个并发的长回答一起把预算撑爆，
    反而触发拒绝。2 MiB 覆盖绝大多数正常回答，同时仍比"一律按上限"省八倍。
    """
    cap = maximum_bytes()
    if not isinstance(body, dict):
        return cap
    # 不依赖协议枚举：直接看请求里出现了哪个字段。`inspect` 会因为"该协议不支持
    # 这个字段"而抛错，但那只对**校验**有意义；这里只想知道客户端想要多少。
    present = [name for name in FIELDS if name in body]
    if not present:
        return cap
    values = {body[name] for name in present}
    if len(values) != 1:
        return cap                       # 多字段冲突：请求会被拒，按上限
    value = values.pop()
    if type(value) is not int or value <= 0:
        return cap
    estimate = value * BYTES_PER_OUTPUT_TOKEN
    # 下限 2 MiB：低于它的一律按 2 MiB 记，避免长回答被按几十千字节低估。
    floor = min(cap, DECLARED_OUTPUT_FLOOR_BYTES)
    return min(cap, max(floor, estimate))


class OutputBudget:
    def __init__(self):
        self.used=0
        self.maximum=maximum_bytes()

    def add(self, value):
        size=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))
        self.add_bytes(size)

    def add_bytes(self, size):
        if self.used+size>self.maximum:
            raise OutputLimitExceeded('Upstream output exceeds the configured byte limit')
        self.used+=size


def bounded_events(method):
    """Charge actual translated bytes once, including nested feed -> finish.

    Validate an entire returned batch before exposing any executable tool event.
    Routers reserve a small, separate failure envelope outside the success cap.
    """
    @wraps(method)
    def guarded(self, *args, **kwargs):
        depth=getattr(self,'_wire_event_depth',0)
        self._wire_event_depth=depth+1
        try:
            events=method(self,*args,**kwargs)
        finally:
            self._wire_event_depth=depth
        if depth==0:
            budget=getattr(self,'wire_budget',None)
            if budget is None:
                budget=self.wire_budget=OutputBudget()
                budget.maximum=self.output_budget.maximum
            budget.add_bytes(sum(len(event) for event in events))
        return events
    return guarded


def tool_object(raw):
    def constant(_):raise ValueError('nonfinite tool input')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate tool input key')
            value[key]=item
        return value
    try:
        if not isinstance(raw,str):raise ValueError('tool input must be JSON text')
        value=json.loads(raw,parse_constant=constant,object_pairs_hook=unique)
        if not isinstance(value,dict):raise ValueError('tool input must be an object')
        return value
    except (ValueError,TypeError,RecursionError) as exc:
        raise ValueError('Upstream tool arguments are incomplete or invalid') from exc
