"""红包：一次创建 N 个「一份一个 key」的密钥，额度按拼手气或均分分配。

为什么叫红包而不是「批量建密钥」
--------------------------------
它解决的是**分发**场景：管理员一次拿到 N 个现成的 key，各自额度不同，
随手发给不同的人。手动建 N 个 key 要填 N 次表单（还要自己想额度怎么分），
红包只填两次——总额与份数。

两种分发方式：管理员自己发 / 分享链接让别人抽
-----------------------------------------------
创建时会生成一个**抽奖码**（`token_urlsafe(16)` ≈ 128 位熵），两种都用得上：

  · 管理员直接把某个 key 发出去 —— 结果页一次性给全明文，他自己挑着发；
  · 把 `…/claim?code=<抽奖码>` 发给同事朋友，对方打开抽一份 —— **不需要登录**，
    收到链接的人多半没有账号，也不该为了领个红包去注册。

抽奖码本身就是凭据（拿到它就能抽走一份），所以它只在管理端展示、不写日志。
公开端的防滥用靠三件事，**都不可省**：

  · **每个 IP 对同一个红包只能抽一次**（`draw` 在事务里判定，并发也拦得住）；
  · 抽奖码 128 位熵 —— 猜不出来，也就没法扫；
  · 红包自带失效时间（默认 7 天）。

已知限制：同一个人换网络就能多领，同一个办公室共用一个出口 IP 却只能领一次 ——
IP 是这里能拿到的最省事、也最粗的标识。要更严就得引入手机号/邀请码级的
风控，那是另一套设计，不该和这个混在一起。

两条设计约束值得先知道
----------------------
1. 普通 `api_keys` 表只存 hash + 前缀；红包的 `red_packet_shares.token`
   **另存明文**以支持后续领取和按 IP 找回。创建响应和符合 `claim_info` 条件的
   查询都可能返回明文，因此红包码、数据库及其备份均需按凭据保护。
2. **默认 7 天失效**。发出去的东西收不回，且熟人场景里常见「拿到就忘了」；
   给个默认期限让它们自动清理，比留一堆永不失效的 key 干净。
"""
from __future__ import annotations

import json
import math
import secrets
import time
from decimal import Decimal, InvalidOperation

from . import db, keylimits, keysvc

# 额度类别。两者的**限制对象不同**，不只是单位不同：
#   · credit —— 限制上游返回的真实扣费（usage.credit）。口径准：同样 1M token，
#     便宜模型与贵模型的实际扣费能差几十倍，按积分限才反映真实成本。
#   · token  —— 限制 prompt+completion 总数。直观、与客户端显示的数字一致，
#     但估不准花了多少钱。
# 所以两条路都要保留：管理员知道自己在发什么就行。
KIND_CREDIT = 'credit'
KIND_TOKEN = 'token'
KINDS = (KIND_CREDIT, KIND_TOKEN)

# 每份的最小额度。积分支持小数（上游 credit 本身就是小数，如 0.05），
# token 是整数。
MIN_UNIT = {KIND_CREDIT: 0.01, KIND_TOKEN: 1}

# 分配方式
MODE_LUCKY = 'lucky'   # 拼手气：随机，有人多有人少
MODE_EVEN = 'even'     # 均分：每份一样
MODES = (MODE_LUCKY, MODE_EVEN)

# 默认有效期（天）。见模块头注释第 2 条。
DEFAULT_TTL_DAYS = 7

# 单次红包的份数上限。上限存在的理由不是技术限制，而是**防手滑**：
# 一次建几千个 key 会把密钥列表刷爆，而管理员大概率是填错了。
MAX_SHARES = 100


class RedPacketError(ValueError):
    """参数不合法。路由层把它翻成 400。"""


def _units(total: float, kind: str) -> int:
    """把总额换算成最小单位的**整数个数**；不是整单位就抛 RedPacketError。

    每份最终都会成为一把密钥的额度，而额度必须是最小单位的整数倍（积分
    0.01、token 1）。总额不是整单位时（0.045 积分、2.5 个 token）**没有精确
    解**：浮点拆分会把差额静默吞掉（实测 0.045 分 4 份合计只有 0.04），只能拒绝。

    Decimal 而不是浮点：`0.045 * 100 == 4.499999999999999`，`round` 给出 4，
    正好丢掉那 0.005。
    """
    unit = MIN_UNIT[kind]
    try:
        count = Decimal(str(total)) / Decimal(str(unit))
    except (InvalidOperation, ValueError, TypeError):
        raise RedPacketError('总额不是合法的数值') from None
    if count != count.to_integral_value():
        if kind == KIND_TOKEN:
            raise RedPacketError('总额必须是整数（token 的最小单位是 1 个）')
        raise RedPacketError(
            f'总额必须是 {unit:g} 的整数倍（每份都得是最小单位的整数倍）')
    return int(count)


def split_amount(total: float, shares: int, kind: str,
                 mode: str = MODE_LUCKY) -> list[float]:
    """把 total 分成 shares 份，每份都是**最小单位的正整数倍**。

    为什么先在整数单位上拆：这个函数产出的每个数都会被当成一把密钥的额度，
    而额度 0 在 keylimits 里的语义是「**不限**」。用浮点直接拆，`round` 能把
    一份压到 0 或负数 —— `split_amount(0.03, 5, 'credit', MODE_EVEN)` 给出
    `[0.01, 0.01, 0.01, 0.0, 0.0]`（三份钱、两份无限额度），
    `split_amount(3, 3, 'token', MODE_LUCKY)` 可能给出 `[2, 1, 0]`。
    换成整数单位后每份天然 ≥ 1 个单位；总额不够每份一个单位
    （`shares * unit > total`）时显式抛错，绝不静默发一份 0 出去。

    拼手气用**二倍均值法**：每份的随机上限是「当前剩余均值 × 2」，下限是一个
    最小单位。为什么要设上限：不设的话第一份可能抽走绝大部分（均匀随机会这样），
    后面的人拿到接近 0——那是 bug 不是惊喜。上限保证了**越往后越稳**，
    同时保留随机性（有人多有人少，但不会有人什么都拿不到）。

    均分用商与余数：余数每份补一个单位，各份最多相差一个最小单位。
    """
    unit = MIN_UNIT[kind]
    decimals = 2 if kind == KIND_CREDIT else 0
    scale = 10 ** decimals
    units_total = _units(total, kind)
    if units_total < shares:
        need = float(Decimal(str(unit)) * shares)
        raise RedPacketError(
            f'总额太小：{shares} 份每份至少 {unit:g}，合计至少 {need:g}')

    if mode == MODE_EVEN:
        each, extra = divmod(units_total, shares)
        parts = [each + (1 if i < extra else 0) for i in range(shares)]
    else:
        rng = secrets.SystemRandom()
        parts = []
        remaining = units_total
        for i in range(shares - 1):
            left = shares - i
            hi = remaining / left * 2          # 剩余均值 × 2（单位：最小单位）
            amt = 1 if hi <= 1 else int(round(rng.uniform(1, hi)))
            # 钳位：至少 1 个单位，且给后面每份留够 1 个单位 —— 少了这一步
            # 最后一份会在整数上变成 0 甚至负数。
            amt = min(max(amt, 1), remaining - (left - 1))
            parts.append(amt)
            remaining -= amt
        parts.append(remaining)
    return [p / scale for p in parts]


def validate(total: float, shares: int, kind: str, mode: str,
             ttl_days: int | None, models: list[str] | None = None) -> None:
    """校验创建参数。不合法时抛 RedPacketError（路由层翻成 400）。

    模型范围的规则（#131 起）：

      · **token 红包必须限定模型**。token 是「量」，与模型强相关——同一段
        上下文在不同模型下的 token 数、输出长度、上下文窗口都不同，不限定
        范围的话「10 万 token 红包」的含义是浮动的，收的人也不知道自己
        能拿它干什么。
      · **积分红包可选**。不填 = 不限制（按真实扣费计，任何模型都能用，这
        也是它原本的行为）；填了 = 只放行这些模型 —— 用来把一份额度限定到
        某个模型上（issue #131：「只想把一部分额度分享给别人用某一个模型」）。
        做成可选的理由：不填的人算额度时不会多一层变量，要控范围的人也不必
        绕开红包另发密钥。
    """
    if kind not in KINDS:
        raise RedPacketError(f'额度类别只能是 {" 或 ".join(KINDS)}')
    if mode not in MODES:
        raise RedPacketError(f'分配方式只能是 {" 或 ".join(MODES)}')

    names = [str(m).strip() for m in (models or []) if str(m).strip()]
    if kind == KIND_TOKEN and not names:
        raise RedPacketError('Token 红包必须限定模型范围（token 数与模型强相关）')

    if not isinstance(shares, int) or isinstance(shares, bool) or shares < 1:
        raise RedPacketError('份数必须是正整数')
    if shares > MAX_SHARES:
        raise RedPacketError(f'份数最多 {MAX_SHARES} 份（一次建太多会把密钥列表刷爆）')

    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        raise RedPacketError('总额必须大于 0')
    try:
        finite = math.isfinite(total)
    except OverflowError:
        finite = False
    if not finite:
        raise RedPacketError('总额必须是有限数值')
    # 关键约束：每份至少一个最小单位，且总额必须是整单位（否则拆不出精确的等额）。
    # 不拦的话 split_amount 会产出 0 份额，而那些 key 的额度是 0 —— 0 在
    # keylimits 里的语义是「不限」，于是一份残渣变成无限额度的钥匙。
    unit = MIN_UNIT[kind]
    if _units(total, kind) < shares:
        need = float(Decimal(str(unit)) * shares)
        raise RedPacketError(
            f'总额太小：{shares} 份每份至少 {unit:g}，合计至少 {need:g}')

    if ttl_days is not None:
        if not isinstance(ttl_days, int) or isinstance(ttl_days, bool) or ttl_days < 1:
            raise RedPacketError('有效期必须是不小于 1 的天数')
        if ttl_days > 3650:
            raise RedPacketError('有效期最长 10 年')


def create_packet(name: str, kind: str, total: float, shares: int,
                  mode: str, ttl_days: int | None, actor: str,
                  models: list[str] | None = None) -> dict:
    """创建红包：生成 shares 个密钥 + 记录这一批。返回含**明文 key** 的结果。

    整批写在**同一个事务**里：中途失败就整体回滚，不会留下「几个 key 建好了、
    红包记录却没有」的半成品——那些 key 没人知道是红包发的，也就收不回来。

    `models`：token 红包必填、积分红包可选（见 `validate`）。
    """
    validate(total, shares, kind, mode, ttl_days, models)

    amounts = split_amount(total, shares, kind, mode)
    # 兜底：算出来的份额一旦是 0，落到 create_key 就是一把「不限额度」的密钥
    # （0 = 显式不限额，见 keylimits）。split_amount 已经保证每份 ≥ 一个最小
    # 单位，这里再用 require_positive 拦一次 —— 将来谁改坏了拆包逻辑，坏的是
    # 建包失败（400），而不是悄悄发出一把无限额度的钥匙。
    quota_field = 'quota' if kind == KIND_TOKEN else 'quota_credit'
    for amount in amounts:
        try:
            keylimits.require_positive(quota_field, amount)
        except ValueError as exc:
            raise RedPacketError(f'拆包结果存在非正份额，已拒绝创建：{exc}') from exc
    expires_at = int(time.time()) + (ttl_days or DEFAULT_TTL_DAYS) * 86400
    title = db._clean(name, 64) or '红包'
    # 抽奖码：token_urlsafe(16) ≈ 128 位熵（22 字符）。
    # 它是**凭据** —— 拿到它就能抽走红包里的一份，所以要长到猜不出来。
    code = secrets.token_urlsafe(16)
    # 归一化后再存：与密钥侧的白名单是**同一份**数据（都从用户输入来），
    # 两边规则不同的话，红包说限了 A、密钥实际限了 B，排查时会怀疑人生。
    # 两类都接受（token 必填、积分可选，见 validate）；积分不填就是空 = 不限制。
    model_list = [str(m).strip() for m in (models or []) if str(m).strip()]

    # 与 create_key 共用同一段 INSERT（见 keysvc.create_key 的 `_conn` 参数），
    # 不把 SQL 抄第二遍——两份 SQL 漂移过一次就够了（count_tokens 的鉴权）。
    created: list[dict] = []
    with db._lock:
        # 连接在锁内取：PG 恢复会在锁内关闭并重开共享连接。
        conn = db.connect()
        try:
            conn.execute('BEGIN')
            packet_id = conn.execute(
                'INSERT INTO red_packets(title, quota_kind, total_amount, shares, '
                'mode, models, code, created_by, created_at, expires_at) '
                'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (title, kind, float(total), shares, mode,
                 json.dumps(model_list), code, actor, int(time.time()), expires_at),
            ).lastrowid
            # 红包是**系统代建**密钥：必须显式给保守集合。`create_key` 的默认值
            # 走 `platformpolicy.normalize(None)`，那是「全部平台」——新增的
            # 豆包工作会因此对每一张红包密钥静默生效（权限放大）。
            from . import platformpolicy
            _conservative_platforms = list(platformpolicy.DEFAULT_PLATFORMS)
            for i, amount in enumerate(amounts, start=1):
                made = keysvc.create_key(
                    name=f'{title}-{i}',
                    expires_at=expires_at,
                    models=model_list,      # 积分红包不填时为 []（= 不限制）
                    platforms=_conservative_platforms,
                    quota=amount if kind == KIND_TOKEN else 0,
                    quota_credit=amount if kind == KIND_CREDIT else 0,
                    _conn=conn,          # ← 在同一个事务里，由本函数统一提交
                )
                conn.execute(
                    'INSERT INTO red_packet_shares(packet_id, key_id, amount, token) '
                    'VALUES(?, ?, ?, ?)',
                    # token 存明文：抽奖时要原样返回给领取者，而那时库里只剩
                    # 哈希（见 db.py 建表处的取舍说明）。
                    (packet_id, made['id'], float(amount), made['key']),
                )
                created.append(made)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        'id': packet_id,
        'title': title,
        'quota_kind': kind,
        'total_amount': float(total),
        'shares': shares,
        'mode': mode,
        'models': model_list,   # token 红包非空、积分红包可选
        'code': code,           # 抽奖码 —— 拼出分享链接用（它是凭据，别外传）
        'expires_at': expires_at,
        'created_at': int(time.time()),
        'keys': created,        # 含明文；后续领取/同IP找回见 claim_info
    }


def backfill_codes() -> int:
    """给没有抽奖码的红包补上，返回补了几个（幂等）。

    抽奖码是本功能后半段才加的列：在那之前建的红包 `code` 是 NULL，而拼不出
    抽奖链接就等于这份红包只能自己发 key、没法让大家抽。启动时补一次即可。

    **已有的不动**：换一个码等于让已经发出去的链接全部失效。
    """
    rows = db.query("SELECT id FROM red_packets WHERE code IS NULL OR code = ''")
    for r in rows:
        db.execute('UPDATE red_packets SET code = ? WHERE id = ?',
                   (secrets.token_urlsafe(16), r['id']))
    return len(rows)


def _models_of(raw: object) -> list[str]:
    """把库里存的 JSON 数组还原成列表；坏数据按空处理，不让它把接口打成 500。

    读接口不该因为一行脏数据整个挂掉——这是旁路展示数据，不是决策依据。
    """
    try:
        out = json.loads(str(raw or '[]'))
    except (TypeError, ValueError):
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def zero_share_amount(key_id: int) -> float | None:
    """该密钥若是红包发出来的**非正额度**份额，返回它的金额；否则 None。

    存量数据**只读**：修复之后不会再产出新的 0 份额（见 split_amount 与
    create_packet），但库里已经发出去的改不了，也不该悄悄改 —— 份额金额、
    领取记录、详情页照旧可读。这里只回答一个问题：「这把密钥是不是一份没分到
    钱的残渣」。消费侧（`budget.prepare`）据此拒绝服务，而不是把额度 0 读成
    「不限」。
    """
    row = db.query_one('SELECT amount FROM red_packet_shares WHERE key_id = ?',
                       (int(key_id),))
    if row is None:
        return None
    amount = float(row['amount'])
    # NaN 同样算残渣（NaN > 0 为假）：脏数据不该被当成合法额度放行。
    return None if amount > 0 else amount


def list_packets() -> list[dict]:
    """红包列表不返回明文 key；份额表为领取功能另存明文。"""
    rows = db.query(
        'SELECT p.*, '
        '  (SELECT COUNT(*) FROM red_packet_shares s WHERE s.packet_id = p.id) AS shares_actual '
        'FROM red_packets p ORDER BY p.id DESC'
    )
    out = []
    for r in rows:
        # 「被领走了多少份」而不是「密钥被停用了多少把」—— 抽奖式红包一眼要看的是
        # 进度（还剩几份），停用数只在收回后才等于份数。两个都返回，让界面自己选。
        # 「被领走」要把**作废的**排除掉：收回时未领的份额会被打上空 IP 标记
        # （见 revoke_packet），它们既不是「已领」也不是「还能领」。
        claimed = db.query_one(
            'SELECT COUNT(*) AS n FROM red_packet_shares '
            'WHERE packet_id = ? AND claimed_by_ip IS NOT NULL '
            "  AND claimed_by_ip != ''", (r['id'],))['n']
        # 收回的判据：这批**密钥已经全都不存在了**。
        #
        # 为什么不用「有多少把被停用」：收回现在是**删除**密钥（收回后要立刻从
        # 列表消失），删了就没得数。也不看「有没有空 IP 标记」—— 那个标记只在
        # 有未领份额时才会被打上，而「全部被领完之后再收回」不会有标记，那种
        # 情况只能靠「密钥还在不在」判断。
        n_keys = db.query_one(
            'SELECT COUNT(*) AS n FROM red_packet_shares s '
            'JOIN api_keys k ON k.id = s.key_id WHERE s.packet_id = ?',
            (r['id'],))['n']
        revoked = n_keys == 0 and int(r['shares_actual']) > 0
        out.append({
            'id': r['id'],
            'title': r['title'],
            'quota_kind': r['quota_kind'],
            'total_amount': float(r['total_amount']),
            'shares': int(r['shares']),
            'mode': r['mode'],
            'models': _models_of(r['models']),
            'code': r['code'] or '',
            'created_by': r['created_by'],
            'created_at': int(r['created_at']),
            'expires_at': int(r['expires_at']),
            'claimed': int(claimed),
            # 上面已经算成布尔了，别再拿 shares 去比一遍 —— 那是「停用数 ≥ 份数」
            # 时代留下的判据，换判据时漏改这里会让 revoked 恒为 false。
            'revoked': bool(revoked),
        })
    return out


def packet_detail(packet_id: int) -> dict | None:
    """红包详情：每一份的密钥与用量（仍不含明文）。"""
    p = db.query_one('SELECT * FROM red_packets WHERE id = ?', (packet_id,))
    if not p:
        return None
    # LEFT JOIN：收回时那批密钥被**删掉**了（见 revoke_packet），用 INNER JOIN
    # 的话已收回的红包在详情里会变成空的 —— 而「这个红包原本几份」恰恰是
    # 收回之后最该看得到的信息。键没了就显示成已收回。
    rows = db.query(
        'SELECT s.amount, s.claimed_by_ip, s.claimed_at, '
        '       k.id AS key_id, k.prefix, k.enabled, k.used_tokens, k.used_credit '
        'FROM red_packet_shares s LEFT JOIN api_keys k ON k.id = s.key_id '
        'WHERE s.packet_id = ? ORDER BY s.id', (packet_id,))
    return {
        'id': p['id'],
        'title': p['title'],
        'quota_kind': p['quota_kind'],
        'total_amount': float(p['total_amount']),
        'shares': int(p['shares']),
        'mode': p['mode'],
        'models': _models_of(p['models']),
        'created_by': p['created_by'],
        'created_at': int(p['created_at']),
        'expires_at': int(p['expires_at']),
        'code': p['code'] or '',
        'items': [{
            'key_id': r['key_id'],
            # 同上：密钥已被收回删掉时 prefix 是 NULL，直接透给前端会渲染成
            # 「null…」——那看着像 bug。空串让界面走「已收回」那一支。
            'prefix': r['prefix'] or '',
            'amount': float(r['amount']),
            'enabled': bool(r['enabled']),
            # LEFT JOIN 之后密钥可能不存在（收回时被删了）→ 这几个字段是 NULL，
            # 直接 int(None) 会抛 TypeError 把整个详情接口打成 500
            'used_tokens': int(r['used_tokens'] or 0),
            'used_credit': float(r['used_credit'] or 0),
            # 被谁领走的（抽奖式才有）。空 IP 是「收回时被作废」的占位，见
            # revoke_packet —— 那种既不是没领、也不是有效领取。
            'claimed_by_ip': (r['claimed_by_ip'] or None) or None,
            'claimed_at': int(r['claimed_at']) if r['claimed_at'] else None,
        } for r in rows],
    }


def revoke_packet(packet_id: int) -> int:
    """收回整批：**删除**这批密钥，并作废还没被抽走的份额。返回删除的数量。

    「删除」而不是「停用」是刻意的：收回的语义是「这批不发了」，那些密钥就该
    从密钥列表里消失 —— 留一堆 disabled 的红包密钥在列表里，既占地方又没人会
    去逐把处理（这也正是「红包密钥」要单独分组的原因）。要留痕的话，用量记录
    与 `red_packet_shares` 都还在，删掉 api_keys 行不影响「这批发过多少」。

    为什么要连未领的份额一起作废：抽奖是「从**未领取**的份额里随机取一份」。
    只处理已发出的密钥的话，收回之后链接**照样能抽出新的一份** —— 管理员以为
    收回了，实际还能继续领。

    同时**抹掉未领份额里存的明文 key**（评审补）：收回之后这些份额已不可再被
    抽走，明文留着就只是「库被读走时多泄露一份」的纯风险；已领走的那份明文
    不在此列（领取者手里本来就有，库里那份再抹也收不回来，而留着便于管理员
    对照排查）。金额、领取记录都保留，详情页照旧能回答「原本几份、还剩几份」。

    删除后 `red_packet_shares.key_id` 会悬空（指向不存在的密钥），所以
    `packet_detail` 用的是 LEFT JOIN，界面上把这类显示成已收回。
    """
    # Deleting every key and invalidating undrawn shares is one revocation.
    # A storage failure must roll back the whole batch, never report that still
    # usable keys were revoked. Missing keys are already handled as False.
    with db.transaction() as conn:
        rows = conn.execute(
            'SELECT key_id FROM red_packet_shares WHERE packet_id = ?', (packet_id,)).fetchall()
        key_ids = [int(r['key_id']) for r in rows if r['key_id']]
        deleted = sum(keysvc.delete_key(kid) for kid in key_ids)
        # 未领的份额用空 IP 占位（抽奖只认 claimed_by_ip IS NULL）。
        # 不删行：删了就看不出「这个红包原本几份、收回时还剩几份」。
        conn.execute(
            "UPDATE red_packet_shares SET claimed_by_ip = '', claimed_at = ?, token = '' "
            "WHERE packet_id = ? AND claimed_by_ip IS NULL",
            (int(time.time()), packet_id))
    return deleted


class ClaimError(ValueError):
    """抽奖失败。`already` 为真表示「这个 IP 已经领过了」——与其它失败区分，
    前端要给的提示不同（前者是「你已经抽过」，后者是「来晚了/链接失效」）。"""

    def __init__(self, message: str, *, already: bool = False) -> None:
        super().__init__(message)
        self.already = already


def claim_info(code: str, ip: str) -> dict:
    """抽奖页要显示的信息。

    **已经领过的 IP 会连带把那一份的密钥一起返回**：用户关掉弹窗之后往往
    才想起来没存，而明文只显示那一次；让他刷新一下就能找回来，比「请联系
    发红包的人」有用得多。没领过的 IP 拿不到任何密钥（`my_key` 为 None）。

    代价说清楚：同一个 NAT 出口下的人（同一间办公室、同一个手机热点）
    能看到彼此领到的那份。这是刻意的取舍 —— 红包的场景本来就是熟人，
    而「领完就再也找不回来」是更常发生、更让人恼火的问题。
    """
    p = db.query_one('SELECT * FROM red_packets WHERE code = ?', (code,))
    if not p:
        raise ClaimError('红包不存在或链接已失效')
    left = db.query_one(
        'SELECT COUNT(*) AS n FROM red_packet_shares '
        'WHERE packet_id = ? AND claimed_by_ip IS NULL', (p['id'],))['n']
    mine = db.query_one(
        'SELECT amount, token FROM red_packet_shares '
        'WHERE packet_id = ? AND claimed_by_ip = ?', (p['id'], ip))
    return {
        'title': p['title'],
        'quota_kind': p['quota_kind'],
        'shares': int(p['shares']),
        'left': int(left),
        'models': _models_of(p['models']),
        'expires_at': int(p['expires_at']),
        'expired': int(p['expires_at']) < time.time(),
        'claimed': mine is not None,
        # 只有「这个 IP 自己领过」的那一份才会出现在这里，不是别人的。
        'my_amount': float(mine['amount']) if mine else None,
        'my_key': str(mine['token']) if mine else None,
    }


def draw(code: str, ip: str) -> dict:
    """抽一份，返回**明文密钥**与这一份的额度。每个 IP 对同一个红包只能抽一次。

    「查重 → 随机选一份 → 标记领取」必须在**同一个事务**里完成。分开做的话
    两个并发请求会各自查到「没领过」、各自挑一份：同一个人抽到两份，而且其中
    一份原本该属于别人（先被标记走了）。
    """
    with db._lock:
        # 连接在锁内取：PG 恢复会在锁内关闭并重开共享连接。
        conn = db.connect()
        try:
            conn.execute('BEGIN')
            p = conn.execute('SELECT * FROM red_packets WHERE code = ?',
                             (code,)).fetchone()
            if not p:
                raise ClaimError('红包不存在或链接已失效')
            if int(p['expires_at']) < time.time():
                raise ClaimError('红包已过期')
            if conn.execute('SELECT id FROM red_packet_shares '
                            'WHERE packet_id = ? AND claimed_by_ip = ?',
                            (p['id'], ip)).fetchone():
                raise ClaimError('这个网络已经领过了', already=True)
            row = conn.execute(
                'SELECT id, amount, token FROM red_packet_shares '
                'WHERE packet_id = ? AND claimed_by_ip IS NULL '
                'ORDER BY RANDOM() LIMIT 1', (p['id'],)).fetchone()
            if not row:
                raise ClaimError('红包已被领完')
            # 明文为空 = 这个红包是在「存明文」这一列上线之前建的（它的密钥
            # 当时只存了哈希）。**必须在标记领取之前拦下**：放过去的话份额被
            # 消耗掉，而抽到的人拿到的是一把看不见的密钥 —— 两头都亏。
            #
            # 这类红包可以用 scripts 里的补救脚本修（重新签一把密钥并回填），
            # 所以提示里直接告诉他找发红包的人，而不是让人以为链接坏了。
            if not str(row['token'] or '').strip():
                raise ClaimError('这个红包创建于旧版本，不支持抽奖；请联系发红包的人重新生成')
            conn.execute(
                'UPDATE red_packet_shares SET claimed_by_ip = ?, claimed_at = ? '
                'WHERE id = ?', (ip, int(time.time()), row['id']))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        'amount': float(row['amount']),
        'quota_kind': p['quota_kind'],
        'models': _models_of(p['models']),
        'key': str(row['token']),     # 明文；同IP持码找回见 claim_info
        'expires_at': int(p['expires_at']),
    }
