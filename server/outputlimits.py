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
