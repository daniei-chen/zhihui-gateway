"""Deterministic observations decide alerts; AI never decides whether to send."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    rule: str
    active: bool | None
    severity: str
    title: str
    detail: str


def evaluate(snapshot, policy):
    output = []
    disk = snapshot['resources']['disk']
    used = disk['used_percent']
    output.append(Signal('disk_capacity', used >= policy.disk_warning_percent or disk['free_bytes'] < 2 * 1024**3,
        'critical' if used >= policy.disk_critical_percent or disk['free_bytes'] < 2 * 1024**3 else 'warning',
        '存储空间需要关注', f"应用所在磁盘已用 {used}%，剩余 {round(disk['free_bytes'] / 1024**3, 2)} GiB。"))
    memory = snapshot['resources']['memory']['used_percent']
    output.append(Signal('memory_capacity', None if memory is None else memory >= policy.memory_warning_percent,
                         'warning', '内存余量偏低', f'按可用内存计算，已用比例 {memory}%。'))
    output.append(Signal('memory_evidence', memory is None, 'warning', '内存采集证据缺失',
                         '未取得当前可用内存，不能据此判断正常或恢复。'))
    traffic = snapshot['traffic']
    output.append(Signal('inference_failure', None if traffic['requests'] < policy.min_requests
                         or traffic['failure_percent'] is None
                         else traffic['failure_percent'] >= policy.failure_warning_percent,
                         'warning', '调用失败率上升',
                         f"最近5分钟 {traffic['requests']} 次已受理请求，失败 {traffic['failed']} 次；拒绝请求未混入该口径。"))
    latency = traffic['p95_total_latency_ms']
    output.append(Signal('inference_latency', None if traffic['latency_samples'] < policy.min_requests
                         or latency is None else latency >= policy.latency_warning_ms,
                         'warning', '调用完成耗时上升',
                         f'最近成功请求的总耗时 P95 为 {latency} ms；它受输出长度影响，不等于首字延迟。'))
    unknown = snapshot['budget']['unknown_count']
    output.append(Signal('budget_evidence', unknown >= policy.budget_unknown_warning, 'warning',
                         '部分用量仍待核对', f'共有 {unknown} 条预算待核对；不能据此断言扣费，也不能无证据清零。'))
    channel = snapshot['ledger']
    output.append(Signal('ledger_channel', channel['state'] == 'degraded' or channel['writable'] is not True
                         or (channel.get('pending') or 0) > 0 or (channel.get('dead') or 0) > 0,
                         'critical', '账本记录通道需要处理',
                         f"通道状态 {channel['state']}，待重放 {channel.get('pending')}，待处置 {channel.get('dead')}。"))
    backup = snapshot['backup']
    output.append(Signal('backup_evidence', backup['status'] != 'ok', 'warning', '备份证明需要复核',
                         f"本机备份状态 {backup['status']}；未证明异地完整恢复能力。"))
    certificate = snapshot.get('certificate', {})
    days = certificate.get('days_left')
    known = certificate.get('status') == 'ok' and type(days) is int
    output.append(Signal('certificate_expiry', days <= 21 if known else None,
                         'critical' if known and days <= 3 else 'warning', '证书续期需要关注',
                         f'最近本机证据的剩余有效期为 {days} 天；不等于公网实际证书链验收。'))
    output.append(Signal('certificate_evidence', not known, 'warning', '证书到期证据需要复核',
                         '最近七天内的固定证书证据缺失或无效，不能据此判断正常或恢复。'))
    for platform in snapshot.get('platforms', []):
        output.append(Signal('platform_' + platform['name'], platform['state'] != 'up', 'warning',
                             platform['name'] + ' 状态需要核对',
                             f"只读探针状态 {platform['state']}；未知不等于已宕机，健康也不等于真实推理验收。"))
    return output
