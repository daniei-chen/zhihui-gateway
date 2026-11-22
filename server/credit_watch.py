"""积分余额快照（只记录，不扣费）。

2026-11-22 实测：Trae 按**积分**结算，但上游**不返回单次消耗多少积分**——
连发两次请求（257 / 267 tokens）后账号余额一点没动，`updated_at` 也没变，
说明积分是周期批量结算的。所以现在无法逐次记 credit。

在搞清楚「多少 token 折多少积分」之前，先把余额按小时存下来。有了
「某时段 token 消耗」与「同时段积分减少」两条时间序列，就能反推换算系数 ——
这也是接入第三方平台时唯一能用的校验手段。

本模块**只写快照**，不参与任何扣费判定。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

from . import db, externalplatforms

log = logging.getLogger('server.credit_watch')

TICK_SECONDS = 3600        # 积分是小时级/日级结算，一分钟一次只白占磁盘
STARTUP_DELAY_SECONDS = 90
DEFAULT_LOW_RATIO = 0.2    # 可被设置项 `credit_low_ratio` 覆盖
CRITICAL_RATIO = 0.05
#: WorkBuddy 账号池的「单号余额偏低」线：低于这个数就点名。
WB_LOW_ACCOUNT = 1000
WB_CREDIT_TIMEOUT = 90
#: 快照保留天数。每小时 2 条（trae + workbuddy），30 天约 1440 行、
#: workbuddy 那条带明细约 3KB —— 总量十几 MB，够做趋势与对账。
#: 不设上限的话这张表会一直长，而「一年前的每小时余额」没有任何用处。
RETENTION_DAYS = 30

DDL = (
    "CREATE TABLE IF NOT EXISTS platform_credit_snapshots("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "platform TEXT NOT NULL,"
    "account TEXT,"          # NULL = 平台级单账号（直连平台通常如此）
    "plan TEXT,"
    "total REAL, used REAL, remain REAL,"
    "captured_at INTEGER NOT NULL,"
    "raw TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_pcs_platform_ts "
    "ON platform_credit_snapshots(platform, captured_at)",
)


def ensure_schema() -> None:
    for statement in DDL:
        db.execute(statement)


def _ratio(remain, total):
    try:
        remain, total = float(remain), float(total)
    except (TypeError, ValueError):
        return None
    return None if total <= 0 else remain / total


def _snapshot_rows() -> list:
    """从平台视图取带积分字段的账号；任何失败都返回空列表，绝不抛。"""
    rows = []
    try:
        view = externalplatforms.trae_accounts()
    except Exception as exc:  # noqa: BLE001 - 采集失败不该影响在线请求
        log.warning('trae 账号视图读取失败，本轮跳过：%s', exc)
        return rows
    for account in (view or {}).get('accounts') or []:
        if not isinstance(account, dict):
            continue
        total, remain = account.get('credits_size'), account.get('credits_remain')
        if total is None and remain is None:
            continue
        rows.append({
            'platform': 'trae',
            'account': account.get('uid') or None,
            'plan': account.get('credits_plan') or account.get('plan') or '',
            'total': total,
            # used 优先用上游给的；缺了才用 total-remain 反算。
            'used': (account.get('credits_used')
                     if account.get('credits_used') is not None
                     else (None if total is None or remain is None
                           else float(total) - float(remain))),
            'remain': remain,
        })
    return rows


def _wb_container() -> str:
    """wb2api 容器名；没配就返回空串（本机没有 WorkBuddy 时静默跳过）。"""
    return (os.environ.get('WB2API_CONTAINER') or '').strip()


def _wb_pool_snapshot() -> dict | None:
    """一次取回 WorkBuddy **整池**余额。

    为什么走容器里的 `./credit`，而不是像 trae 那样逐个问上游：

      · 一次 docker exec 拿 23 个账号，比 23 次独立上游请求快两个数量级；
      · 不占用 `CREDITS_CONCURRENCY` 的名额，不会把面板的实时查询挤排队；
      · 巡检本身不该让上游多收请求 —— 积分是钱，少打一次是一次。

    只做汇总：23 个账号逐条入库，一年就是 20 万行，而趋势只需要池级数字。
    明细放进 `raw`，需要点名某个号时仍查得到。

    任何失败都返回 None，绝不抛 —— 采集挂了不该影响在线请求。
    """
    container = _wb_container()
    if not container:
        return None
    try:
        proc = subprocess.run(
            ['docker', 'exec', container, 'sh', '-c',
             'cd /app && ./credit -json 2>/dev/null'],
            capture_output=True, timeout=WB_CREDIT_TIMEOUT, check=False)
    except Exception as exc:  # noqa: BLE001 - 采集失败降级为「本轮跳过」
        log.warning('workbuddy 积分采集执行失败，本轮跳过：%s', exc)
        return None
    if proc.returncode != 0:
        log.warning('workbuddy 积分采集返回 %s，本轮跳过', proc.returncode)
        return None
    try:
        payload = json.loads(proc.stdout.decode('utf-8', 'replace') or '{}')
    except ValueError as exc:
        log.warning('workbuddy 积分返回不是 JSON，本轮跳过：%s', exc)
        return None
    accounts = payload.get('accounts')
    if not isinstance(accounts, list) or not accounts:
        return None
    total = used = remain = 0.0
    ok_rows, failed, lows = [], [], []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if not item.get('ok'):
            # 单个号上游拒绝（session 失效等）不影响整池统计，
            # 但要在 raw 里留痕，否则「池子变少」会没有解释。
            failed.append({'uid': item.get('uid'), 'nickname': item.get('nickname'),
                           'error': str(item.get('error') or '')[:120]})
            continue
        size, rem, use = item.get('size'), item.get('remain'), item.get('used')
        if not isinstance(rem, (int, float)):
            continue
        total += float(size or 0)
        remain += float(rem)
        used += float(use or 0)
        row = {'uid': item.get('uid'), 'nickname': item.get('nickname'),
               'remain': rem, 'used': use, 'size': size}
        ok_rows.append(row)
        if isinstance(rem, (int, float)) and rem < WB_LOW_ACCOUNT:
            lows.append(row)
    if not ok_rows:
        return None
    return {
        'platform': 'workbuddy',
        'account': 'pool',
        'plan': '%d 个可用账号' % len(ok_rows),
        'total': total or None,
        'used': used or None,
        'remain': remain,
        'accounts_ok': len(ok_rows),
        # 明细整份留在 raw 里：面板要按 uid 把真实余额贴回账号列表，
        # 而 wb2api 自己的 credits 字段滞后可达数小时（多数时候就是 0）。
        # 23 个账号约 2KB，一小时一条完全可接受。
        'accounts': ok_rows,
        'accounts_failed': failed,
        'low_accounts': lows,
    }


def _last_remain(platform: str, account: str | None = None):
    """上一条快照的 (remain, captured_at)；读失败返回 (None, None)。

    `account` 非空时按「平台 + 账号」定位 —— Trae 是账号级快照，
    池级汇总查法在它身上会取到别的账号的数字。
    """
    try:
        if account:
            row = db.query_one(
                'SELECT remain, captured_at FROM platform_credit_snapshots'
                ' WHERE platform=? AND account=? ORDER BY id DESC LIMIT 1',
                (platform, account))
        else:
            row = db.query_one(
                'SELECT remain, captured_at FROM platform_credit_snapshots'
                ' WHERE platform=? ORDER BY id DESC LIMIT 1', (platform,))
    except Exception:  # noqa: BLE001
        return None, None
    if not row:
        return None, None
    return row['remain'], row['captured_at']


def capture_once() -> int:
    rows = _snapshot_rows()
    wb = _wb_pool_snapshot()
    if wb:
        rows.append(wb)
    if not rows:
        return 0
    # 统一给每条算与上一条的差额：这是回答「昨晚签到到底有没有生效」最直接的
    # 证据。余额上升只可能来自签到/活动/领奖，下降才是真实消耗 ——
    # 之前只有「剩余多少」，看不出中间发生过什么。
    #
    # 2026-11-23：从只覆盖 WorkBuddy 扩到所有平台。Trae 每天也签到补 100，
    # 同样需要「一看就知道补进来没有」；而且它只有账号级快照，必须按
    # (平台, 账号) 定位上一条，否则会拿别的账号的余额来相减。
    for row in rows:
        prev_remain, prev_at = _last_remain(row['platform'], row.get('account'))
        if isinstance(prev_remain, (int, float)) and row.get('remain') is not None:
            row['delta'] = round(float(row['remain']) - float(prev_remain), 2)
            row['delta_since'] = prev_at
            log.info('%s 余额 %+.2f（%s → %s）',
                     row['platform'], row['delta'], prev_remain, row['remain'])
    now = int(time.time())
    for row in rows:
        db.execute(
            'INSERT INTO platform_credit_snapshots'
            '(platform, account, plan, total, used, remain, captured_at, raw)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (row['platform'], row['account'], row['plan'], row['total'],
             row['used'], row['remain'], now,
             json.dumps(row, ensure_ascii=False)))
    _prune(now)
    # 上游自动签到的结果只在容器日志里。每小时采集顺带同步一次，
    # 面板上的「今日签到」才不会一直显示未签到（用户明明签过了）。
    # 失败只跳过：这是展示层补齐，不该影响采集本身。
    try:
        sync_checkin_from_logs()
    except Exception:  # noqa: BLE001
        log.warning('签到记录同步失败（不影响采集）', exc_info=True)
    return len(rows)


def _prune(now: int) -> int:
    """删掉超过保留期的快照，返回删除行数。

    顺手清理的理由：这张表每小时长 2 行，不设上限就一直涨；而「一个月前的
    每小时余额」既不能回答趋势（粒度太细），也不该长期占着库。
    删除失败只记一条警告 —— 清理是维护动作，不该让采集本身报错。
    """
    cutoff = int(now) - RETENTION_DAYS * 86400
    try:
        return db.execute(
            'DELETE FROM platform_credit_snapshots WHERE captured_at < ?',
            (cutoff,)) or 0
    except Exception:  # noqa: BLE001
        log.warning('历史积分快照清理失败（不影响采集）', exc_info=True)
        return 0


def latest(platform: str = 'trae', with_raw: bool = False):
    cols = ('platform, account, plan, total, used, remain, captured_at'
            + (', raw' if with_raw else ''))
    return db.query_one(
        'SELECT ' + cols +
        ' FROM platform_credit_snapshots WHERE platform=?'
        ' ORDER BY captured_at DESC, id DESC LIMIT 1', (platform,))


def low_ratio_threshold() -> float:
    """告警阈值；设置项写坏就退回默认值（不让运维配置把告警关掉）。"""
    try:
        value = float(db.get_setting('credit_low_ratio'))
    except (TypeError, ValueError):
        return DEFAULT_LOW_RATIO
    return value if 0 < value <= 1 else DEFAULT_LOW_RATIO


def alerts() -> list:
    """积分余额告警：`[(告警名, 是否触发, 文案)]`。

    2026-11-22 实测唯一 Trae 账号：积分 528.5/3796（13.9%），已用 86.1%，
    每日签到只补 100；而系统原先**只有「账号数=1」的告警**。积分见底那天
    平台直接不可用且无提前通知，所以要按余额比例单独告警。
    数据缺失时不告警 —— 宁可不说，也不制造假信号。
    """
    row = latest('trae')
    if not row:
        return [('account_credits_trae', False, '尚无积分快照')]
    ratio = _ratio(row['remain'], row['total'])
    if ratio is None:
        return [('account_credits_trae', False, '积分口径不完整（缺 total/remain）')]
    threshold = low_ratio_threshold()
    detail = ('trae 积分剩余 %.1f/%.1f（%.1f%%），已用 %.1f%%，每日签到补充有限；'
              '余额耗尽会让整个平台不可用'
              % (row['remain'] or 0, row['total'] or 0, ratio * 100, (1 - ratio) * 100))
    if ratio < CRITICAL_RATIO:
        return [('account_credits_trae', True, '[即将耗尽] ' + detail)]
    if ratio < threshold:
        return [('account_credits_trae', True, detail)]
    return [('account_credits_trae', False,
             'trae 积分剩余 %.1f%%（阈值 %.0f%%）' % (ratio * 100, threshold * 100))]


#: `usage_events.credit_units` 是**百万分之一积分**（见 ledger.SCALE）。
#: 与 Trae 那套按天结算不同，这里是 manager 按 token 定价算出来的**应扣**，
#: 不是上游的实扣 —— 上游结算明显滞后（快照里 remain 一夜只降 1 分，
#: 而同期按 token 估算要几千分）。所以拿它做**上界估算**：宁可高估消耗，
#: 也不要因为看不到扣款就以为用不完。
CREDIT_UNITS_SCALE = 1_000_000.0
#: 用量估算的缓存时长。`usage_events` 上没有以 `ts` 为前缀的索引
#: （现有的是 key+ts），实测查询计划是**全表扫描 + 临时 B 树分组**。
#: 而告警检查是分钟级的，每次都扫几十万行纯属浪费；用量本身是日级变化，
#: 十分钟缓存不会让数字失真。
USAGE_RATE_TTL = 600
_usage_rate_cache: dict = {}


def usage_credit_rate(days: int = 7) -> dict | None:
    """按 token 定价估算每日积分消耗（来自 usage_events，不依赖快照）。

    为什么单独存在：`consumption_rate` 读的是**余额快照**，而上游积分结算
    严重滞后 —— 快照里 remain 可以连续几十小时不动，于是算出「几乎不消耗」。
    那是个危险的假象：manager 自己按 token 记的应扣额度，同期是每天上千分的。

    这里返回的是**上界**（应扣），实际扣款只会更少、更晚。对「还能撑多久」
    这种问题，高估消耗是安全侧。

    数据不足返回 None（少于两天、或总额为 0）。
    结果缓存 `USAGE_RATE_TTL` 秒 —— 全表扫描不该被分钟级的告警检查反复触发。
    """
    now = time.time()
    cached = _usage_rate_cache.get(days)
    if cached and now - cached[0] < USAGE_RATE_TTL:
        return cached[1]
    since = int(now) - max(1, int(days)) * 86400
    try:
        rows = db.query(
            'SELECT date(ts, "unixepoch", "localtime") AS day,'
            ' SUM(credit_units) AS units, COUNT(*) AS n'
            ' FROM usage_events WHERE ts>=? AND credit_units>0'
            ' GROUP BY day ORDER BY day', (since,))
    except Exception:  # noqa: BLE001
        return None
    if not rows or len(rows) < 2:
        return None
    daily = []
    for row in rows:
        units = row['units']
        if isinstance(units, (int, float)) and float(units) > 0:
            daily.append(float(units) / CREDIT_UNITS_SCALE)
    if not daily:
        return None
    total = sum(daily)
    mean = total / len(daily)
    # 中位数做主口径：2026-11-16/17 两天各有约 5000 分的峰值（那两天 prompt
    # 平均 22 万 token，是压测/大批量导入的形态），把平均数抬到 1921，
    # 得出「只剩 40 天」；而最近三天稳定在 500~1900。用平均值会让一次突发
    # 长期污染预判，中位数对这种尖峰不敏感。
    ordered = sorted(daily)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    recent = daily[-3:]
    result = {'per_day': median, 'per_day_mean': mean,
            'per_day_recent': sum(recent) / len(recent),
            'median': median, 'total': total, 'days': len(daily),
            'peak': max(daily),
            'samples': sum(int(r['n'] or 0) for r in rows),
            'cached_at': int(now)}
    _usage_rate_cache[days] = (now, result)
    while len(_usage_rate_cache) > 8:
        _usage_rate_cache.pop(next(iter(_usage_rate_cache)))
    return result


def consumption_rate(platform: str = 'workbuddy', days: int = 14,
                     min_span_seconds: int = 6 * 3600) -> dict | None:
    """从快照里估算**每日积分净消耗**，用来回答「还能撑几天」。

    只累加余额**下降**的相邻区间：签到、旅行领奖都会让余额回升，把回升一起
    算进去会把消耗抹平，结论变成「永远用不完」——那是假的安全感。
    代价是净消耗略微低估（回升部分的真实支出看不见），对「还能撑多久」这个
    问题来说，低估消耗 = 高估天数，所以下面在文案里明确写出样本时长，
    让人自己判断可信度。

    数据不足（少于 2 条快照，或跨度短于 min_span_seconds）时返回 None，
    绝不拿两条相邻记录外推。
    """
    since = int(time.time()) - max(1, int(days)) * 86400
    try:
        rows = db.query(
            'SELECT remain, captured_at FROM platform_credit_snapshots'
            ' WHERE platform=? AND remain IS NOT NULL AND captured_at>=?'
            ' ORDER BY captured_at ASC', (platform, since))
    except Exception:  # noqa: BLE001 - 估算失败不该影响告警
        return None
    if not rows or len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    span = int(last['captured_at'] or 0) - int(first['captured_at'] or 0)
    if span < min_span_seconds:
        return None
    consumed = 0.0
    prev = None
    for row in rows:
        remain = row['remain']
        if not isinstance(remain, (int, float)):
            continue
        if prev is not None:
            delta = float(prev) - float(remain)
            if delta > 0:
                consumed += delta
        prev = remain
    if consumed <= 0:
        return {'per_day': 0.0, 'consumed': 0.0, 'span_hours': span / 3600.0,
                'samples': len(rows)}
    return {'per_day': consumed / (span / 86400.0), 'consumed': consumed,
            'span_hours': span / 3600.0, 'samples': len(rows)}


#: 上游调度器打印的签到结果形如 `checkin 昵称(uid前8): done`。
#: 这里只认这个形状；对不上就跳过，不去猜别的格式。
_CHECKIN_LINE = re.compile(
    r'checkin\s+(?P<name>\S+?)\((?P<uid8>[0-9a-f]{8})\):\s*(?P<result>.+?)\s*$')


def sync_checkin_from_logs(hours: int = 14, container: str | None = None) -> int:
    """把 wb2api 自动签到的结果同步进本端签到记录。

    为什么需要：自动签到由上游调度器自己执行，**不经过 manager**，所以面板上
    「今日签到」一直显示未签到 —— 而用户明明签过了。唯一的信息源是容器日志。

    这里只做一件事：把日志里已发生的签到结果搬进本端记录，让面板显示真实状态。
    解析失败、容器不可用、日志为空都只跳过，不抛异常 —— 这是展示层补齐，
    不该影响任何在线请求。

    去重按 (uid8, 本地日)：同一账号同一天只记一次，避免每小时采集重复插入。
    """
    name = (container or os.environ.get('WB2API_CONTAINER') or '').strip()
    if not name:
        return 0
    try:
        proc = subprocess.run(
            ['docker', 'logs', '--since', '%dh' % max(1, int(hours)), name],
            capture_output=True, timeout=60, check=False)
    except Exception as exc:  # noqa: BLE001
        log.warning('签到日志读取失败，本轮跳过：%s', exc)
        return 0
    if proc.returncode != 0:
        return 0
    text = (proc.stderr or b'') + b'\n' + (proc.stdout or b'')
    try:
        decoded = text.decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        return 0
    local = time.localtime()
    day = '%04d-%02d-%02d' % (local.tm_year, local.tm_mon, local.tm_mday)
    written = 0
    seen = set()
    for line in decoded.splitlines():
        hit = _CHECKIN_LINE.search(line)
        if not hit:
            continue
        uid8 = hit.group('uid8')
        if uid8 in seen:
            continue
        seen.add(uid8)
        result = hit.group('result').strip()
        low = result.lower()
        if 'already' in low or 'skip' in low:
            # 已签到 / 跳过：都不算失败，只是没产生新积分。
            success = True
        elif 'done' in low or low == 'ok' or low.startswith('ok'):
            success = True
        else:
            success = False
        try:
            exists = db.query_one(
                'SELECT id FROM checkin_logs WHERE uid LIKE ? AND ts>=? AND kind=?'
                ' ORDER BY id DESC LIMIT 1',
                (uid8 + '%', int(time.mktime((local.tm_year, local.tm_mon,
                                              local.tm_mday, 0, 0, 0, 0, 0, -1))),
                 'checkin'))
        except Exception:  # noqa: BLE001
            exists = None
        if exists:
            continue
        try:
            db.add_checkin_log(uid8, hit.group('name'), 'upstream-auto',
                               success, None, result[:120])
            written += 1
        except Exception:  # noqa: BLE001
            log.warning('签到记录写入失败 uid8=%s', uid8)
    if written:
        log.info('已从上游日志同步 %d 条签到记录（%s）', written, day)
    return written


def checkin_today_summary() -> dict | None:
    """今天签到结果的汇总：多少个成功、多少失败、多少是「今天已签过」。

    数据来自本端签到记录（含从上游日志同步来的那些）。`already` 单独计数，
    因为「本次新签到拿到积分」和「今天早就签过了」对运维是两个不同的事实 ——
    前者说明签到真的跑了，后者只能说明「今天这件事已经办过」。

    今天还没有任何记录时返回 None（不是全 0 —— 「还没签」和「签了但都失败」
    得区分开）。
    """
    now = time.time()
    local = time.localtime(now)
    day_start = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                 0, 0, 0, 0, 0, -1)))
    try:
        rows = db.query(
            'SELECT success, message FROM checkin_logs'
            ' WHERE ts>=? AND kind=? ORDER BY ts DESC', (day_start, 'checkin'))
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    success = failed = already = 0
    failed_names = []
    for row in rows:
        msg = str(row['message'] or '').lower()
        if row['success']:
            if 'already' in msg:
                already += 1
            else:
                success += 1
        else:
            failed += 1
            if len(failed_names) < 5:
                failed_names.append(str(row['message'] or '')[:60])
    return {'total': len(rows), 'success': success, 'already': already,
            'failed': failed, 'failed_samples': failed_names}


def usage_today(platform: str = 'workbuddy') -> dict | None:
    """今天到目前为止的积分用量，以及**昨日同一时刻**的用量。

    为什么要跟「昨日同期」比，而不是跟「昨日全天」比：现在是凌晨四点，
    拿半天用量去比全天会显得一切正常；只有对齐到同一个时刻，才能回答
    「今天是不是突然用得多」。

    数据来自 request_logs 的 credit 字段（按 token 定价的应扣），
    与用量估算同源。表上有 ts 索引，两个范围查询都很轻。
    """
    now = time.time()
    local = time.localtime(now)
    day_start = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                 0, 0, 0, 0, 0, -1)))
    elapsed = int(now) - day_start
    try:
        today = db.query_one(
            'SELECT SUM(credit) AS credits, COUNT(*) AS n FROM request_logs'
            ' WHERE platform=? AND ts>=?', (platform, day_start))
        yesterday = db.query_one(
            'SELECT SUM(credit) AS credits, COUNT(*) AS n FROM request_logs'
            ' WHERE platform=? AND ts>=? AND ts<?',
            (platform, day_start - 86400, day_start - 86400 + elapsed))
    except Exception:  # noqa: BLE001 - 估算失败不该影响告警
        return None
    if not today:
        return None
    # `db.query_one` 返回 sqlite3.Row —— **它没有 .get()**，只有下标和 .keys()。
    # 2026-11-23 第二次踩这个坑（第一次在 workbuddy_alerts）。凡是要按「可能
    # 不存在的键」取值的地方，先转 dict，别再直接对 Row 调 .get()。
    today = dict(today)
    yesterday = dict(yesterday) if yesterday else {}
    t_credits = float(today['credits'] or 0)
    y_credits = float(yesterday['credits'] or 0)
    ratio = None
    if y_credits > 0:
        ratio = round(t_credits / y_credits, 2)
    return {'credits': round(t_credits, 2), 'requests': int(today['n'] or 0),
            'yesterday_same_time': round(y_credits, 2),
            'yesterday_requests': int(yesterday['n'] or 0),
            'ratio': ratio, 'elapsed_hours': round(elapsed / 3600.0, 2)}


def today_gain(platform: str = 'workbuddy') -> dict | None:
    """今天（按本地日）累计的**正向增量**，用来回答「今天补进来多少」。

    为什么单独算：单条快照的 `delta` 只看最近一小时。签到是每天一次、
    补的是一整笔，只看一小时会错过 —— 早上那笔 +2300 到中午就看不到了。

    只累加正数：负数（真实消耗）与正数（签到/活动）混在一起算没有意义，
    前者由 `usage_credit_rate` 负责估算。

    当天没有任何快照时返回 None（不是 0 —— 「没采集」和「没补进来」是两件事）。
    """
    now = time.time()
    local = time.localtime(now)
    day_start = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                 0, 0, 0, 0, 0, -1)))
    try:
        rows = db.query(
            'SELECT raw, captured_at FROM platform_credit_snapshots'
            ' WHERE platform=? AND captured_at>=? ORDER BY captured_at ASC',
            (platform, day_start))
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    gain = 0.0
    events = []
    for row in rows:
        try:
            raw = json.loads(row['raw'] or '{}')
        except (ValueError, TypeError):
            continue
        delta = raw.get('delta')
        if isinstance(delta, (int, float)) and delta > 0:
            gain += float(delta)
            events.append({'at': row['captured_at'], 'delta': float(delta)})
    return {'gain': round(gain, 2), 'events': len(events),
            'samples': len(rows), 'day_start': day_start}


def account_token_alerts(days_warn: int = 7) -> list:
    """账号登录凭证的过期预警。

    为什么单独做一条：实测 23 个账号是**同一批登录**的，到期时刻几乎相同
    （2026-12-20 06:00）。这意味着失效不是「慢慢变少」，而是**某天集体掉线**
    —— 没有任何渐进信号，只能提前看日期。

    只读账号文件里的 `expiresAt`，不触发任何上游请求，也不改文件。
    读不到就当作未知，不猜。
    """
    from pathlib import Path

    auth_dir = Path(os.environ.get('WB_AUTH_DIR') or '/opt/workbuddy2api/auths')
    if not auth_dir.is_dir():
        return [('workbuddy_token_expiry', False,
                 '账号目录不可读（%s），跳过凭证检查' % auth_dir)]
    now = time.time()
    expired, soon, unknown, total = [], [], 0, 0
    try:
        paths = sorted(auth_dir.glob('workbuddy-*.json'))
    except OSError as exc:
        return [('workbuddy_token_expiry', False,
                 '账号目录读取失败：%s' % exc)]
    for path in paths:
        total += 1
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            unknown += 1
            continue
        auth = raw.get('auth') if isinstance(raw, dict) else None
        exp = auth.get('expiresAt') if isinstance(auth, dict) else None
        nick = ((raw.get('account') or {}).get('nickname')
                if isinstance(raw, dict) else None) or path.stem[-8:]
        if not isinstance(exp, (int, float)) or exp <= 0:
            unknown += 1
            continue
        # 上游给的是秒；万一出现毫秒也能兜住。
        if exp > 1e11:
            exp = exp / 1000.0
        days = (exp - now) / 86400.0
        if days <= 0:
            expired.append(str(nick))
        elif days <= days_warn:
            soon.append((str(nick), days))
    if expired:
        return [('workbuddy_token_expiry', True,
                 '%d 个账号凭证**已过期**（%s），这些号已无法使用'
                 % (len(expired), '、'.join(expired[:5])))]
    if soon:
        soon.sort(key=lambda x: x[1])
        return [('workbuddy_token_expiry', True,
                 '%d 个账号凭证将在 %d 天内过期（最近：%s 剩 %.1f 天）'
                 % (len(soon), days_warn, soon[0][0], soon[0][1]))]
    if not total:
        return [('workbuddy_token_expiry', False, '账号目录为空')]
    days_left = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            exp = ((raw.get('auth') or {}).get('expiresAt'))
        except (OSError, ValueError):
            continue
        if isinstance(exp, (int, float)) and exp > 0:
            if exp > 1e11:
                exp = exp / 1000.0
            days_left.append((exp - now) / 86400.0)
    if not days_left:
        return [('workbuddy_token_expiry', False, '凭证过期时间不可读')]
    return [('workbuddy_token_expiry', False,
             '%d 个账号凭证正常，最近一批约 %.0f 天后到期'
             % (total, min(days_left)))]


def workbuddy_alerts() -> list:
    """WorkBuddy 账号池的积分告警。

    与 trae 分开两个告警名：两个平台的积分体系、补充渠道、耗尽后果都不同，
    混在一条里会让「哪一边快没了」看不出来。

    池级比例看趋势，单号余额看「这一个号是不是马上要空」——
    后者尤其重要：池子总计 15 万分时，某个号剩 600 分照样会在一次长上下文
    请求里被打空，然后被 `skip_exhausted` 摘出池子，而总量看起来还很健康。
    """
    row = latest('workbuddy', with_raw=True)
    if not row:
        return [('account_credits_workbuddy', False, '尚无 WorkBuddy 积分快照')]
    # `db.query_one` 返回的是 sqlite3.Row —— **它没有 .get()**，
    # 只有下标和 .keys()。2026-11-23 首次上线时这里写成了 row.get('raw')，
    # 采集成功、告警却在装配阶段抛 AttributeError，把后面所有告警一起跳过。
    # 统一转成 dict，后面按下标/键取都安全。
    row = dict(row)
    lines = []
    ratio = _ratio(row['remain'], row['total'])
    if ratio is None:
        lines.append(('account_credits_workbuddy', False,
                      'WorkBuddy 积分口径不完整（缺 total/remain）'))
    else:
        threshold = low_ratio_threshold()
        detail = ('workbuddy 池剩余 %.0f/%.0f（%.1f%%），%s'
                  % (row['remain'] or 0, row['total'] or 0, ratio * 100,
                     row['plan'] or '账号数未知'))
        # 「还剩多少」之外，运维真正想问的是「还能撑几天」。
        #
        # 用 usage_events 的 token 定价（应扣）而不是余额快照：上游积分结算
        # 滞后严重，快照里 remain 可以连续几十小时不动，据此会得出「几乎不消耗」
        # 的假象。这里给出的是**上界**——高估消耗，宁可早提醒。
        _rate = usage_credit_rate()
        if _rate is None:
            detail += '；用量样本不足，暂不预测消耗'
        elif _rate['per_day'] <= 0:
            detail += '；近 %d 天无用量' % _rate['days']
        else:
            _days = (row['remain'] or 0) / _rate['per_day']
            # 三个数字一起给：中位数（主口径）、峰值日（说明最坏情况）、
            # 近三天（说明当前节奏）。只报一个数会让人误以为它是确定的。
            detail += ('；按近 %d 天用量中位数 %.0f 分/天约可撑 %.0f 天'
                       '（峰值日 %.0f 分，近三天 %.0f 分/天）'
                       % (_rate['days'], _rate['per_day'], _days,
                          _rate.get('peak') or 0, _rate.get('per_day_recent') or 0))
            # 今天补进来多少：签到是每天一笔，单条快照的 delta 只看一小时会错过。
            _gain = today_gain('workbuddy')
            if _gain and _gain['gain'] > 0:
                detail += '；今日已补进 %.0f 分（%d 笔）' % (
                    _gain['gain'], _gain['events'])
        active = ratio < threshold
        if ratio < CRITICAL_RATIO:
            lines.append(('account_credits_workbuddy', True, '[即将耗尽] ' + detail))
        elif active:
            lines.append(('account_credits_workbuddy', True, detail))
        else:
            # 健康态也给完整 detail：运维每天想看的就是「还剩多少、还能撑几天」，
            # 不该只在快见底时才告诉他。active=False 表示不告警，文案照样有信息。
            lines.append(('account_credits_workbuddy', False, detail))
    # 单号余额：从 raw 里取最近一次采集的明细
    lows = []
    try:
        raw = json.loads(row.get('raw') or '{}') if row.get('raw') else {}
        lows = raw.get('low_accounts') or []
        failed = raw.get('accounts_failed') or []
    except (ValueError, TypeError):
        lows, failed = [], []
    if lows:
        names = '、'.join(str(a.get('nickname') or a.get('uid') or '?')
                          for a in lows[:5])
        lines.append(('workbuddy_low_accounts', True,
                      '%d 个 WorkBuddy 账号余额低于 %d 分（%s），下一次长上下文请求就可能清零'
                      % (len(lows), WB_LOW_ACCOUNT, names)))
    else:
        lines.append(('workbuddy_low_accounts', False,
                      '所有 WorkBuddy 账号余额均高于 %d 分' % WB_LOW_ACCOUNT))
    if failed:
        lines.append(('workbuddy_credit_unreadable', True,
                      '%d 个 WorkBuddy 账号余额读不到（上游拒绝）：%s'
                      % (len(failed),
                         '、'.join(str(a.get('nickname') or '?') for a in failed[:5]))))
    return lines


async def _loop() -> None:
    import asyncio
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            written = await run_in_threadpool(capture_once)
            if written:
                log.info('积分快照已记录 %d 条', written)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception('积分快照采集失败（不影响在线请求）')
        await asyncio.sleep(TICK_SECONDS)


def start():
    """在 lifespan 里调用，返回可取消的 Task。"""
    import asyncio
    try:
        ensure_schema()
    except Exception:  # noqa: BLE001
        log.exception('积分快照建表失败')
    log.info('积分快照采集已启动（每 %s 秒一次）', TICK_SECONDS)
    return asyncio.create_task(_loop(), name='credit-watch')
