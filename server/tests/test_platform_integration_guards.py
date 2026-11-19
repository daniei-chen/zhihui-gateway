"""第五个平台接入时的「漏改」守卫。

这些测试不测功能，测的是**结构一致性**：`PLATFORMS` 里出现的每个平台，
是否在每一处注册点都有对应的条目。

为什么需要它：本次接入豆包工作一共碰了 48 个文件，审计抓出的最严重一条
（`main.py` 的平台路由白名单漏了新平台 → 请求落到别的平台的 handler，
读写都是别人的数据）**没有任何测试会在当时失败**。靠人记清单是不可靠的，
所以把清单固化成断言。

新增平台时这些测试会集体变红，逼着你把清单走完——这正是它们存在的意义。
"""
import re
from pathlib import Path

import pytest

from server import platformpolicy
from server.qtgcore.models import AccountsResponse


ROOT = Path(__file__).resolve().parents[1]
CORE = platformpolicy.PLATFORMS


def test_every_platform_has_a_capability_row():
    """`/api/platforms` 的能力矩阵必须覆盖全集。

    漏一个的后果：返回 `name=<id>`、`upstream_id=0`、`capabilities={}` 的空壳，
    外部面板据此决定按钮可用性，会得到一个既不全禁用也不全放开的错状态。
    """
    from server.qtgcore.routers import compat_fixes

    missing_cap = [p for p in CORE if p not in compat_fixes._CAP_MATRIX]
    missing_name = [p for p in CORE if p not in compat_fixes._NAME]
    missing_upid = [p for p in CORE if p not in compat_fixes._UPID]
    assert not missing_cap, f'能力矩阵缺平台: {missing_cap}'
    assert not missing_name, f'显示名缺平台: {missing_name}'
    assert not missing_upid, f'upstream_id 缺平台: {missing_upid}'


def test_every_platform_has_an_upstream_id():
    """兼容层的 `upstream_id` 与 `PLATFORMS` 必须一一对应。

    漏一个的后果：`_rows_of` 取不到该平台的账号行，分组视图里整个平台消失。
    """
    from server.qtgcore.routers import compat_fixes

    mapped = set(compat_fixes._PLAT_TO_UPSTREAM)
    assert mapped == set(CORE), f'映射不一致: 缺 {set(CORE) - mapped}，多 {mapped - set(CORE)}'
    # id 必须唯一且是正整数
    ids = list(compat_fixes._PLAT_TO_UPSTREAM.values())
    assert len(ids) == len(set(ids)), f'upstream_id 重复: {ids}'
    assert all(isinstance(i, int) and i > 0 for i in ids), ids


def test_accounts_response_has_a_field_per_platform():
    """`AccountsResponse` 每个平台一个字段：漏了该平台的账号在聚合输出里没有位置。"""
    fields = set(AccountsResponse.model_fields)
    missing = [p for p in CORE if p not in fields]
    assert not missing, f'AccountsResponse 缺字段: {missing}'


def test_platform_routing_whitelist_covers_qtgcore_hosted_platforms():
    """`server/main.py` 的平台路由白名单必须覆盖「由 qtgcore 托管数据面」的平台。

    **这是本次审计抓出的最严重缺陷**：白名单是 `("qoder","trae")`，新平台不在里面，
    于是 `/api/accounts?platform=<新平台>` 不会被改写到 `/__qtg/...`，而是落到
    manager 的原生 handler —— 读的是 WorkBuddy 的账号池，写操作也写进它的目录。

    判据不写死平台名，而是从 main.py 的源码里读出那个元组，再与
    `qtgcore` 里真正实现了账号接口的平台集合比对。
    """
    source = (ROOT / 'main.py').read_text(encoding='utf-8')
    match = re.search(r'if platform in \(([^)]*)\) and _qtg_should_route', source)
    assert match, 'main.py 里找不到平台路由白名单，正则需要更新（实现变了？）'
    whitelist = {s.strip().strip('"\'') for s in match.group(1).split(',') if s.strip()}

    # 由 qtgcore 托管数据面的平台 = compat 层声明支持的那些（workbuddy 走 manager 原生）
    from server.qtgcore.routers import compat_fixes

    hosted = set(compat_fixes._PLAT_TO_UPSTREAM) - {'workbuddy'}
    missing = hosted - whitelist
    assert not missing, (
        f'main.py 平台路由白名单缺少 {sorted(missing)}：'
        f'这些平台的 /api/* 会落到 manager 原生 handler（串台）'
    )


def test_default_platforms_is_a_strict_subset_of_all():
    """授予集必须是全集的真子集：两者相等意味着「新增平台自动对所有密钥生效」。"""
    assert set(platformpolicy.DEFAULT_PLATFORMS) < set(CORE), (
        'DEFAULT_PLATFORMS 等于 PLATFORMS —— 新增平台会自动进入默认授予集（权限放大）'
    )


def test_every_adapter_is_exported():
    """适配器必须从 `adapters` 包导出，否则 runtime 拿不到。"""
    from server.qtgcore import adapters

    expected = {'QoderAdapter', 'TraeAdapter', 'WorkBuddyAdapter', 'DoubaoWorkAdapter'}
    missing = {n for n in expected if not hasattr(adapters, n)}
    assert not missing, f'adapters 包没有导出: {missing}'


def test_upstream_key_files_match_data_plane_spec():
    """`compat_fixes._UPSTREAM_KEY_FILES` 必须与数据面 `_THREEWAY_SPEC` 一致。

    两处都在描述「这个平台的密钥文件在哪」。漂移的后果：上游列表显示
    「已配置」，而数据面实际读的是另一个路径 —— 排障的人会先怀疑别的一切。
    """
    from server.qtgcore.routers import compat_fixes
    from server.routers import gateway

    spec = {name: path for name, (_base, path) in gateway._THREEWAY_SPEC.items()}
    local = compat_fixes._UPSTREAM_KEY_FILES
    assert set(spec) == set(local), (
        f'平台集合不一致: 数据面 spec={sorted(spec)}，上游列表 local={sorted(local)}'
    )
    for name in sorted(spec):
        assert spec[name] == local[name], (
            f'{name} 的密钥路径不一致: 数据面 {spec[name]} vs 上游列表 {local[name]}'
        )


def test_playground_routes_new_platform_or_refuses_loudly():
    """测试台必须认识新平台，且密钥缺失时**明确报不可用**而不是回落到别人。

    `routers/playground.py` 的平台分支是 `_plat in gateway._THREEWAY_SPEC`。
    新平台不在 `_THREEWAY_SPEC` 里会掉到 400「不支持的平台」；
    在但没密钥则 `_threeway_upstream` 返回 None → 503。
    两种都比「静默用 workbuddy 的 key 打过去」好。
    """
    from unittest import mock

    from server.routers import gateway

    assert 'doubaowork' in gateway._THREEWAY_SPEC, (
        'doubaowork 不在 _THREEWAY_SPEC：测试台会回 400「不支持的平台」'
    )
    with mock.patch('builtins.open', side_effect=OSError):
        assert gateway._threeway_upstream('doubaowork') is None, (
            '密钥缺失时 _threeway_upstream 必须返回 None（→503），不能回落'
        )


def test_new_platform_engine_switch_is_file_based():
    """引擎开关必须走 `_file_switch`，不能只有环境变量。

    这条是从一次真实事故来的：豆包工作第一版只写了 `_env_str(...,"off")`，
    于是文档里的回滚方式（`echo off > var/doubaowork_engine` + 重启）是**空操作**，
    运维以为关掉了、平台其实还开着。而 `_wb_engine_default` 上面那条注释
    批评的正是同一个错误。
    """
    from server.qtgcore import config as qtg_config

    source = (ROOT / 'qtgcore' / 'config.py').read_text(encoding='utf-8')
    # 每个 *_engine_default 函数体内都应出现 _file_switch
    funcs = re.findall(r'def (_\w+_engine_default)\(\)[^:]*:\n(.*?)(?=\ndef |\Z)', source, re.S)
    assert funcs, '找不到任何 *_engine_default 函数'
    bad = [name for name, body in funcs if '_file_switch' not in body]
    assert not bad, f'这些引擎开关没有走 _file_switch（回滚会失效）: {bad}'
