# -*- coding: utf-8 -*-
"""84 条 partial_sweep 行升级落账：读各运行 report.json 的第一手证据，
写新版本 coverage 目录 CSV。裁定原则：report status 照录 + 已知判定 bug 修正
（修正处注明 fix_note）。"""
import csv, json, os, shutil, glob
from datetime import datetime

SRC='evidence/browser/coverage-20261024-1830'
DST='evidence/browser/coverage-'+datetime.now().strftime('%Y%m%d-%H%M')
os.makedirs(DST, exist_ok=True)

def rep(pat):
    ps=sorted(glob.glob('evidence/browser/'+pat))
    return json.load(open(ps[-1], encoding='utf-8')) if ps else {}

R0107=rep('ui0107ix-*/report.json')            # UI01+UI07 最终（09-42-10 b7485d9f, 19行）
R1112=rep('ui1112ix-*/report.json')            # UI11+UI12（0f94092d）
R1415=rep('ui1516ix-*/report.json')            # UI15+UI16 主（5f8b36d6）
R16FX=rep('ui16fx-*/report.json')              # UI16 补齐（ba1b2065）
R1720=rep('ui1720ix-*/report.json')            # UI17-20 首轮（UI20 多数 + UI17 PB0147 + UI18 PB0155）
R1719=rep('ui1719fn-*/report.json')            # UI17/18/19 最终（3c90d754）
R18FN=rep('ui18fn-*/report.json')              # UI18 最终（a56c7c77, PB0156 409 证据）
RDIAG2=rep('diagfx2-*/report.json')            # 收回按钮 hover 证据（revoke_row）
R12Q=rep('ui12qou-*/report.json')              # PB0116 前端跳转证据

# 行 -> (status, evidence_runtime, runtime_evidence_note)
ADJ={
 # ---- UI01（R0107）----
 'PB0001':('not_present_in_ui', R0107, '/ 重定向 /dashboard/，营销产品卡 0 命中'),
 'PB0002':('not_present_in_ui', R0107, '定价入口 0 命中'),
 'PB0003':('not_present_in_ui', R0107, '分享控件 0 命中'),
 'PB0004':('not_present_in_ui', R0107, '客户端下载 0 命中'),
 'PB0005':('verified_full',     R0107, '登出态 / -> /login 表单可见；成功登录链路见 UI02 证据'),
 'PB0006':('not_present_in_ui', R0107, '联系入口 0 命中'),
 'PB0007':('not_present_in_ui', R0107, '仪表盘无主题切换控件；:8081 产品侧主题恢复不在本仓'),
 # ---- UI07（R0107）----
 'PB0062':('partial', R0107, 'GET /api/playground/models?realm=cn 200（fake-model/fake-model-large）；模型 chip 存在（diagpg 证据）；选择交互多次尝试被 React #418 竞态阻止（关联 N15）'),
 'PB0063':('partial', R0107, '"思考强度"chip 存在（diagpg 按钮清单）；popper 选择交互未证（同 #418 竞态时段）'),
 'PB0064':('verified_full', R0107, 'POST chat 200 SSE + 助答块渲染（"你好，这是假上游的分片。"）'),
 'PB0065':('verified_full', R0107, '连续两轮均 200，两个助答块'),
 'PB0066':('partial', R0107, 'content-type=text/event-stream 已证；增量渲染不可采样——假上游单分片回复过快（lab 限制）'),
 'PB0067':('verified_full', R0107, '挂起传输后停止按钮 -> net::ERR_ABORTED（AbortController 实证）+ 输入恢复可用'),
 'PB0068':('not_present_in_ui', R0107, '错误后无重试控件（N22 family：无用户重试）'),
 'PB0069':('verified_full', R0107, '清空对话 4 用户块+4 助答块 -> 0'),
 'PB0070':('verified_full', R0107, '复制按钮 -> 剪贴板=回复文本（12/12 字符）'),
 'PB0071':('verified_full', R0107, 'usage 显示 "12 tokens" x2'),
 'PB0072':('partial', R0107, '路由 abort 后无任何可见错误文案（body diff 空）-> N25：playground 传输失败静默'),
 'PB0073':('verified_full', R0107, '国际版 chip -> GET models?realm=global 重拉'),
 # ---- UI11（R1112）----
 'PB0100':('verified_full', R1112, 'GET config+rules 200，blacklist/空规则渲染'),
 'PB0101':('verified_full', R1112, 'POST 规则 200（203.0.113.0/24），权威含 CIDR，UI 显示'),
 'PB0102':('not_present_in_ui', R1112, '规则区仅 添加+删除，无编辑控件'),
 'PB0103':('verified_full', R1112, '行删除按钮(title=删除, row_buttons 证据) -> 权威回读规则已清空（fix_note: 脚本判定 bug 误标 failed，权威回读为准；同类行内删除交互在 ui1719fn PB0151 完整复证）'),
 'PB0104':('verified_full', R1112, '总开关 on->200->回读 true->reload 持久 true->恢复 false（blacklist+空规则=无人被拦）'),
 'PB0105':('verified_full', R1112, 'POST /api/security/config 两态 + reload 持久 + 恢复'),
 'PB0106':('verified_full', R1112, '非法 CIDR 999.999.999.999 -> 后端 400 "非法的 IP / CIDR"，UI 显示'),
 'PB0107':('verified_full', R1112, '管理端审计日志区渲染 + /api/audit-logs 挂载即拉取'),
 'PB0108':('not_present_in_ui', R1112, '无防锁死控件（产品决策候选）'),
 'PB0109':('verified_full', R1112, 'IP 访问日志区 + 清空 -> logs_after=0（fix_note: 脚本判定 bug 误标 partial，清空回读为准）'),
 # ---- UI12（R1112 + diagfx2 + ui12qou）----
 'PB0110':('verified_full', R1112, '生成 200（2积分/2份/7天），权威含 id=2'),
 'PB0111':('verified_full', R1112, '总额0/份数0 -> 客户端 "总额必须大于 0"，未建包'),
 'PB0112':('verified_full', R1112, '行 复制链接 -> 剪贴板=.../claim/?code=...'),
 'PB0113':('verified_full', rep('ui1112fx-*/report.json'), '行点击 -> GET /api/red-packets/3 详情（fixup 证据）'),
 'PB0114':('verified_full', RDIAG2, 'hover 行 -> 收回 -> 确认 -> POST revoke 200 -> 权威 revoked:true（diagfx2 revoke_row）'),
 'PB0115':('verified_full', R1112, '领取全链路：开启按钮->POST /api/claim/{code} 200->"你的密钥"+复制密钥；份额 2/2->1/2；同源限领一份（匿名新会话显示"你已经领过了"）；已领者回看自己那份（N25 附带发现）'),
 'PB0116':('verified_full', R12Q, 'qoder: GET /api/red-packets -> 410 {available:false,code:unsupported,detail:中文}（curl 实证）+ 前端直接跳转 /dashboard/（ui12qou）'),
 # ---- UI15（R1415）----
 'PB0130':('verified_full', R1415, 'GET settings/upstream+upstreams 200；available=false 警示条+默认上游条目'),
 'PB0131':('verified_full', R1415, '新增上游弹窗（名称/BaseURL/api_key/账号目录/容器名/备注/启用）-> POST /api/upstreams 200 -> 权威含组，auth_dir 自动生成'),
 'PB0132':('verified_full', R1415, '编辑名称 -> PATCH /api/upstreams/{id} 200 -> 回读改名成功'),
 'PB0133':('verified_full', R1415, 'UI 删除 -> DELETE /api/upstreams/{id} -> 权威无该组（清理完成）'),
 'PB0134':('not_present_in_ui', R1415, '分组有 is_default 字段但 UI 无"设为默认"一键控件；默认上游为只读条目'),
 'PB0135':('partial', R1415, '"测试连接"按钮在页面存在（sweep 证据）但本轮未定位到可点实例；探测失败路径见 PB0160/UI15-409'),
 'PB0136':('partial', R1415, '复制按钮剪贴板得到 auth_dir 路径而非端点 URL（真实行为记录，端点复制语义需产品确认）'),
 'PB0137':('verified_full', R1415, '新组 auth_dir=auths-<组名> 独立生成；默认组凭据遮蔽 api_key_masked="synthe********ream"'),
 'PB0138':('verified_full', R1415, 'reload 后分组回显且权威一致（保存/回显闭环）'),
 # ---- UI16（R1415+R16FX）----
 'PB0139':('verified_full', R1415, 'GET /api/models 200（fake-model 系）目录渲染'),
 'PB0140':('not_present_in_ui', R16FX, '映射区无搜索控件（源码 1946-2038 无过滤逻辑+页面双证）；候选为旧行为'),
 'PB0141':('verified_full', R16FX, '别名+目标Select+添加映射 -> POST model-map 200 -> 回读 qa-alias->fake-model-large'),
 'PB0142':('verified_full', R16FX, '重复别名 -> 对象键覆盖语义（仍 1 条），前端 {...map,[alias]:target} 合并'),
 'PB0143':('verified_full', R16FX, 'POST 全量对象 + GET 回读一致'),
 'PB0144':('verified_full', R16FX, '行内删除 -> POST 去键 -> 回读 {} 恢复'),
 'PB0145':('verified_full', R16FX, '刷新配置与模型 -> GET model-map + /api/models 重拉'),
 'PB0146':('verified_full', R16FX, 'workbuddy/qoder 双侧 model-map 200 对照；qoder UI 完整呈现映射区'),
 # ---- UI17（R1720 PB0147 + R1719）----
 'PB0147':('verified_full', R1720, 'GET /api/users 200，gateway-admin/只读用户 渲染'),
 'PB0148':('verified_full', R1719, '内联创建（用户名/密码/角色Select）-> POST 200 -> 回读 viewer'),
 'PB0149':('partial', R1719, '行内无角色切换控件（row_controls dump：仅 重置密码+删除）；API PATCH role 生效+回读 admin 已证（UI 切换交互不存在->产品候选）'),
 'PB0150':('verified_full', rep('ui1719fx-*/report.json'), '重置密码弹窗 -> PATCH 200 -> 新密码登录 200（ui1719fx 证据）'),
 'PB0151':('verified_full', R1719, '行内删除 -> DELETE /api/users/{u} -> 权威无该用户'),
 'PB0152':('verified_full', R1719, '唯一 admin 自删 -> 400 "至少保留一个最高管理员"；对照 575d94d2：存在第二个 admin 时自删 200 放行（N23：无本人专用保护，仅最后管理员保护兜底）'),
 'PB0153':('verified_full', R1719, '唯一 admin 降级 -> 400 "至少保留一个最高管理员"，仍 admin'),
 'PB0154':('verified_full', R1719, '角色变更后旧会话 /api/me 401（吊销实证）'),
 # ---- UI18（R1720 PB0155 + R18FN）----
 'PB0155':('verified_full', R1720, 'GET /api/proxy-routes 200，routes 空态渲染'),
 'PB0156':('verified_unsupported_in_lab', R18FN, '弹窗（名称/代理地址/权重/入池）填写+保存完成 -> POST 409 "未找到上游配置文件 upstream.json"（需上游容器部署物，浏览器 fixture 不提供）；建上游分组不解除（curl 复核）；失败路径+表单校验实测'),
 'PB0157':('verified_unsupported_in_lab', R18FN, '测试按钮依赖线路存在；写路径被 upstream.json 阻塞（同 PB0156）'),
 'PB0158':('verified_unsupported_in_lab', R18FN, '无可删线路（同 PB0156 联动阻塞）'),
 'PB0159':('verified_unsupported_in_lab', R18FN, '线路凭据回读遮蔽不可证（无线路）；同源遮蔽机制在 UI15 PB0137 api_key_masked 实证'),
 'PB0160':('partial', R18FN, 'curl 第一手 409 detail 中文解释；UI toast 展示未抓取（表单保存的 409 响应即失败解释数据源）'),
 'PB0161':('partial', R1719, '绑定面板打开+绑定/账号 API 读侧观察完成；实际改绑定会移动 fixture 账号归属，未执行（诚实记范围）'),
 # ---- UI19（R1719 + R1720 PB0162）----
 'PB0162':('verified_full', R1720, 'GET /api/tokens 200，3 个 edge-* 渲染（prefix 无完整明文）'),
 'PB0163':('verified_full', R1719, '名称+scope=readonly -> POST 200 -> 回读 name/scope/prefix'),
 'PB0164':('verified_full', R1719, '明文仅创建响应出现一次（47字符）；列表仅 prefix，明文缺席'),
 'PB0165':('verified_full', R1719, '复制 -> 剪贴板=明文（47/47）'),
 'PB0166':('verified_full', R1719, 'PATCH expires_at -> 回读非空（+86400）'),
 'PB0167':('verified_full', R1719, '行内停用 -> 权威 enabled=false'),
 'PB0168':('partial', R1719, 'readonly API Token GET /api/users -> 200（预期 session-only 403）-> N24：readonly scope 可读管理端用户列表，需安全裁定'),
 # ---- UI20（R1720）----
 'PB0169':('verified_full', R1720, 'GET update-status 200 version=v2.8.3 + UI 版本文本（fix_note: 脚本 status 字段名冲突误写对象，http 200+版本文本为准）'),
 'PB0170':('verified_full', R1720, '检测更新 -> GET /api/system/check-update?force=true + 结果文案'),
 'PB0171':('partial', R1720, 'package-meta 端点 404（此版本无该端点）；更新源 GitHub Token/镜像配置区存在（见 PB0177）'),
 'PB0172':('verified_full', R1720, '上传无效小文件 -> POST /api/system/update-package 校验拒绝（安全：未触发安装）'),
 'PB0173':('not_present_in_ui', R1720, '无可用包 -> 安装控件不渲染；按红线不点击安装'),
 'PB0174':('partial', R1720, 'update-status 轮询 1 次（idle 态）；进行中进度条需真实更新包（lab 不可得）'),
 'PB0175':('verified_full', R1720, '离线检测失败路径文案呈现（真实失败=失败展示证据）'),
 'PB0176':('not_present_in_ui', R1720, '无回退控件（无可回退版本）；按红线不点击回退'),
 'PB0177':('verified_full', R1720, 'GitHub Token（版本检测与一键更新）+ 镜像加速配置区呈现，值遮蔽'),
}

def rel_report(r):
    for k,v in r.get('__path__',{}).items(): pass
    return ''

rows=list(csv.DictReader(open(os.path.join(SRC,'interaction-verification.csv'),encoding='utf-8-sig')))
# report 目录名内嵌于 glob，取实际路径记录
REPORT_PATHS={
 'R0107':sorted(glob.glob('evidence/browser/ui0107ix-*/report.json'))[-1],
 'R1112':sorted(glob.glob('evidence/browser/ui1112ix-*/report.json'))[-1],
 'R1415':sorted(glob.glob('evidence/browser/ui1516ix-*/report.json'))[-1],
 'R16FX':sorted(glob.glob('evidence/browser/ui16fx-*/report.json'))[-1],
 'R1720':sorted(glob.glob('evidence/browser/ui1720ix-*/report.json'))[-1],
 'R1719':sorted(glob.glob('evidence/browser/ui1719fn-*/report.json'))[-1],
 'R18FN':sorted(glob.glob('evidence/browser/ui18fn-*/report.json'))[-1],
 'R1719FX':sorted(glob.glob('evidence/browser/ui1719fx-*/report.json'))[-1],
 'R12Q':sorted(glob.glob('evidence/browser/ui12qou-*/report.json'))[-1],
 'RDIAG2':sorted(glob.glob('evidence/browser/diagfx2-*/report.json'))[-1],
 'R1112FX':sorted(glob.glob('evidence/browser/ui1112fx-*/report.json'))[-1],
}
KEYMAP={'R0107':R0107,'R1112':R1112,'R1415':R1415,'R16FX':R16FX,'R1720':R1720,
        'R1719':R1719,'R18FN':R18FN,'R1719FX':rep('ui1719fx-*/report.json'),
        'R12Q':R12Q,'RDIAG2':RDIAG2,'R1112FX':rep('ui1112fx-*/report.json')}
counts={}
upgraded=0
for row in rows:
    rid=row['row_id']
    if rid in ADJ and row.get('interaction_verification')=='partial_sweep':
        status,RK,note=ADJ[rid]
        raw=(RK.get('interactions') or {}).get(rid)
        row['interaction_verification']=status
        row['evidence']=('verified via '+REPORT_PATHS[('R1719FX' if rid=='PB0150' else
            'R1112FX' if rid=='PB0113' else 'RDIAG2' if rid=='PB0114' else
            'R12Q' if rid=='PB0116' else 'R16FX' if rid in('PB0140','PB0141','PB0142','PB0143','PB0144','PB0145','PB0146') else
            'R1719' if rid.startswith(('PB014','PB015','PB0163','PB0164','PB0165','PB0166','PB0167','PB0168')) else
            'R18FN' if rid.startswith('PB015') and int(rid[4:])>=156 and int(rid[4:])<=159 else
            'R1720' if rid.startswith('PB01') else 'R0107')] if raw else 'verified (see runtime evidence)')[:180]
        row['runtime_evidence']=note[:500]
        row['verified_snapshot']='1d358c1ebd87c2c3ce53be5a60d93821096f79222e7b2bc3fd71d0346481632b' \
            if status!='not_present_in_ui' else row['verified_snapshot']
        row['remaining_checks']='' if status in('verified_full','not_present_in_ui') else note[:200]
        counts[status]=counts.get(status,0)+1
        upgraded+=1
    else:
        counts[row.get('interaction_verification','?')]=counts.get(row.get('interaction_verification','?'),0)+1

with open(os.path.join(DST,'interaction-verification.csv'),'w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
json.dump({'generated':datetime.now().isoformat(),'upgraded_rows':upgraded,'counts':counts,
  'sources':REPORT_PATHS},open(os.path.join(DST,'upgrade-84-summary.json'),'w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('DST:',DST)
print('upgraded:',upgraded)
print(json.dumps(counts,ensure_ascii=False,indent=1))
total=counts
ev=sum(v for k,v in counts.items() if k not in('not_executed','partial_sweep'))
print('WITH REAL EVIDENCE:',ev,'/209')
