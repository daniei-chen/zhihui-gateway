"""把开头连续的 system 消息合并成一条。

## 为什么

官方客户端只会发**一条** system 消息。而经过多级封装（框架、SDK、再套一层代理）
之后，请求里常常出现三四条并列的 system —— 这本身就是一个很明显的「不是官方
客户端」的特征，而且我们观测到上游对「多条 system」的处理并不总是等价于拼接。

## 为什么只合并「开头连续」的纯文本

中间出现的 system 消息（例如工具调用结果之后的提醒）位置是有意义的：把它挪走
会改变模型看到的顺序。这里只动头部的、连续的、内容为纯字符串的那些，属于
**无损变换**：拼接后的文本与原顺序逐字一致。

任何带额外键（`name`、缓存标记、工具相关字段等）或非字符串内容（图片、结构化
part）的消息一律不参与合并 —— 那些键各有语义，丢掉或挪动都可能改变请求含义。
宁可少合并，也不要悄悄改掉用户的东西。
"""
from __future__ import annotations

#: 合并后的分隔符。用空行而不是单个换行：多条 system 之间通常本来就是段落关系，
#: 拼成一个空格会把标题和正文黏在一起，读起来反而更像被机器处理过。
JOINER = '\n\n'


def _mergeable(message) -> bool:
    """这条消息能不能参与合并（只允许 role + content 两个键，content 为字符串）。"""
    if not isinstance(message, dict) or message.get('role') != 'system':
        return False
    if set(message) - {'role', 'content'}:
        return False
    return isinstance(message.get('content'), str)


def collapse_leading_system(body) -> int:
    """就地合并开头的连续 system 消息，返回被合并掉的条数。

    返回 0 表示什么都没动（不足两条、或第一条就不符合条件）。
    """
    if not isinstance(body, dict):
        return 0
    messages = body.get('messages')
    if not isinstance(messages, list) or len(messages) < 2:
        return 0

    head = 0
    while head < len(messages) and _mergeable(messages[head]):
        head += 1
    if head < 2:
        return 0

    parts = [messages[index]['content'] for index in range(head)]
    # 空字符串不参与拼接：几条空 system 合并成空串没有意义，还会在日志里留下
    # 一条「改了什么」其实什么都没改的记录。
    texts = [part for part in parts if part.strip()]
    if not texts:
        return 0
    merged = dict(messages[0])
    merged['content'] = JOINER.join(texts)
    body['messages'] = [merged, *messages[head:]]
    return head - 1
