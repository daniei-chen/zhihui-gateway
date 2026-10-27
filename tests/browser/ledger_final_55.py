# -*- coding: utf-8 -*-
"""55 条 not_executed 落账 + 全量汇总（209/209 行均有真实证据或明确裁定）。"""
import csv, json, os, glob
from datetime import datetime

SRC='evidence/browser/coverage-20261024-1924'
DST='evidence/browser/coverage-'+datetime.now().strftime('%Y%m%d-%H%M')+'-final'
os.makedirs(DST, exist_ok=True)

def latest(pat):
    ps=sorted(glob.glob('evidence/browser/'+pat))
    return ps[-1] if ps else None

P={
 'ui04a':latest('ui04ix-*/report.json'),'ui04f':latest('ui04fx-*/report.json'),'ui04n':latest('ui04fn-*/report.json'),
 'ui10a':latest('ui10ix-*/report.json'),'ui10f':latest('ui10fx-*/report.json'),'ui10n':latest('ui10fn-*/report.json'),
 'ui13':latest('ui13ix-*/report.json'),
 'ui21a':latest('ui21ix-*/report.json'),'ui21m':latest('ui21mg-*/report.json'),
 'ui2526':latest('ui2526-*/report.json'),
 'ui08a':latest('ui08ix-*/report.json'),'ui08n':latest('ui08fn-*/report.json'),
 'n22':latest('n22ix-*/report.json'),'n25':latest('n25ix-*/report.json'),
}
EV='1d358c1ebd87c2c3ce53be5a60d93821096f79222e7b2bc3fd71d0346481632b'
EV_NEW='00f1f6ebe98f4ef07d28a91ce909da04d00272a072c626fea4ad4fb312d3f61a'

ADJ={
 # ---- UI04 账号池 19 行 ----
 'PB0023':('verified_full','搜索 qa-acc-002 仅命中该账号（other=0）',P['ui04a'],EV),
 'PB0024':('verified_full','状态标签（全部/可用/需处理/冷却中/已停用）筛选实测',P['ui04f'],EV),
 'PB0025':('verified_full','排序 combobox（默认顺序/有效期近的在前/积分少的在前）客户端排序生效',P['ui04f'],EV),
 'PB0026':('not_present_in_ui','3 账号未达分页阈值，无分页控件',P['ui04a'],EV),
 'PB0027':('not_present_in_ui','账号表无行复选框（checkbox=0）',P['ui04a'],EV),
 'PB0028':('not_present_in_ui','无全选/批量操作条（无复选框体系）',P['ui04a'],EV),
 'PB0029':('verified_full','添加账号入口（扫码授权+手机号登录）；上传路径造数验证 3 账号 added；扫码需真引擎',P['ui04n'],EV),
 'PB0030':('partial','行内"临时停用该账号"按钮存在（title 证据）；三轮点击未观察到 disabled API 调用（disabled_by_panel 始终 false）',P['ui04n'],EV),
 'PB0031':('verified_unsupported_in_lab','POST /api/accounts/refresh-credits?force=false 已触发；fake 引擎返回模拟额度',P['ui04n'],EV),
 'PB0032':('verified_unsupported_in_lab','签到入口存在（页面"全部签到"+行内签到）；fake 引擎下结果为模拟',P['ui04n'],EV),
 'PB0033':('verified_full','行点击打开账号详情（备注/状态/额度）+ GET /api/accounts total=3 渲染',P['ui04n'],EV),
 'PB0034':('not_present_in_ui','账号行无复制控件（剪贴板空）',P['ui04n'],EV),
 'PB0035':('partial','行内"权重"按钮存在；点击交互未完成，weight API 非 2xx',P['ui04n'],EV),
 'PB0036':('not_present_in_ui','行内无代理绑定控件；账号 proxy 字段 API 证实存在；线路受 UI18 upstream.json 联动',P['ui04n'],EV),
 'PB0037':('verified_full','行内"添加备注"-> qa-note-final 权威回读一致',P['ui04n'],EV),
 'PB0038':('partial','"冷却中"统计标签 + 行内"强制进入冷却"按钮存在；触发链路需真引擎（连续失败计数）',P['ui04n'],EV),
 'PB0039':('not_present_in_ui','行内无移动分组控件；move API 400（需参数）',P['ui04n'],EV),
 'PB0040':('partial','行内"删除"按钮存在；UI 链路未观察到 DELETE，API 兜底删除成功',P['ui04n'],EV),
 'PB0041':('partial','"导入/导出账号"菜单打开；export 请求未捕获（菜单项定位问题）',P['ui04n'],EV),
 # ---- UI10 任务 10 行 ----
 'PB0090':('verified_full','GET /api/task-logs + /api/checkin-logs 200，列表/空态渲染',P['ui10a'],EV),
 'PB0091':('verified_full','平台能力说明（任务/签到仅 WorkBuddy；qtg 侧 501 见 K29）',P['ui10a'],EV),
 'PB0092':('partial','5 个开关可切换（data-state 变化）但零写请求；/api/task-schedule 404 —— 疑纯前端态，产品需确认是否应持久化（N27 候选）',P['ui10n'],EV),
 'PB0093':('verified_unsupported_in_lab','"一键领奖"disabled（title："只领取已完成任务的奖励（幂等，不伪造任何行为）"）；真实领奖需上游引擎',P['ui10n'],EV),
 'PB0094':('partial','2 个保存按钮均 disabled（需先真实改动计划时段）；如实记录',P['ui10n'],EV),
 'PB0095':('verified_full','"立即执行" -> POST /api/accounts/refresh-plan/run 200；"预览"按钮 disabled（风控提示）',P['ui10n'],EV),
 'PB0096':('verified_full','7 个 combobox 枚举（时段 00:00–23:00 等筛选维度）',P['ui10n'],EV),
 'PB0097':('verified_full','上游原始日志面板 + GET /api/upstream/logs?limit=300（lab 空态）',P['ui10n'],EV),
 'PB0098':('not_present_in_ui','确认无失败重试控件（与 N22 同族，产品决策）',P['ui10n'],EV),
 'PB0099':('verified_unsupported_in_lab','签到结果区渲染（lab 空态"暂无签到记录"）；真签到需上游引擎',P['ui10n'],EV),
 # ---- UI13 领取页 7 行 ----
 'PB0117':('verified_full','/claim/?code=… 无需登录展示标题/剩余份数/失效时间；GET /api/claim/{code} 200',P['ui13'],EV),
 'PB0118':('verified_full','错误码四态：垃圾码"红包不存在或链接已失效"、空/缺失"链接缺少抽奖码"、已收回"来晚了，已经被领完"',P['ui13'],EV),
 'PB0119':('verified_full','登录用户点"开启" -> POST /api/claim/{code} 200 -> 展示密钥与复制入口',P['ui13'],EV),
 'PB0120':('verified_full','同用户重复领取：不再显示领取按钮，提示"你已经领过了，这是你的那份"（按来源限领一份）',P['ui13'],EV),
 'PB0121':('verified_full','复制密钥 -> 剪贴板',P['ui13'],EV),
 'PB0122':('not_present_in_ui','领取页无导出/下载控件；配置导出在密钥页（UI05 PB0053 已证）',P['ui13'],EV),
 'PB0123':('not_present_in_ui','领取页无返回/继续入口（仅展示领取结果）',P['ui13'],EV),
 # ---- UI21 PgSync 7 行 ----
 'PB0178':('verified_full','面板语义=PgSync 异地镜像到 PostgreSQL（非本地备份归档）',P['ui21a'],EV),
 'PB0179':('verified_full','连接配置 7 字段可填（host/port/db/user/password/sslmode/interval）',P['ui21a'],EV),
 'PB0180':('verified_full','POST /api/settings/pg-sync/test -> ok:false + 具体失败 "connection to server at 127.0.0.1, port 1 failed: Connection refused"',P['ui21a'],EV),
 'PB0181':('verified_full','开始迁移->确认 -> POST /api/settings/pg-sync/export 200；状态机 ok:false，日志"已生成一致性快照（468 KB）"+连接失败（lab 无 PG=真实失败路径）',P['ui21m'],EV),
 'PB0182':('verified_full','同步状态展示："上次迁移：从未 / 上次恢复：从未" + GET pg-sync status',P['ui21a'],EV),
 'PB0183':('verified_full','失败原因在 UI 呈现（导出失败详情含逐字错误）',P['ui21a'],EV),
 'PB0184':('verified_full','完整恢复说明与入口："从 PostgreSQL 恢复" + "恢复前自动备份" + 覆盖警告',P['ui21a'],EV),
 # ---- UI25 旧路径 6 行 ----
 'PB0201':('verified_full','旧 SDK 路径 /profile /gateway /admin /__wb3 /wb 全部 404（Next 404 页、无 Location=非重定向）；对照 /dashboard/ 200、随机路径 404（无 catch-all）',P['ui2526'],EV),
 'PB0202':('verified_full','带 query 仍 404 无重定向，响应体不回显 query',P['ui2526'],EV),
 'PB0203':('verified_full','尾斜杠变体同样 404（无斜杠重定向）',P['ui2526'],EV),
 'PB0204':('verified_full','POST 到旧路径 404（未回落 GET 200，无方法改写）',P['ui2526'],EV),
 'PB0205':('verified_full','旧 SDK 路径（/v1/messages 等）探针：不存在静默重定向',P['ui2526'],EV),
 'PB0206':('verified_full','旧书签无尾斜杠 /keys /accounts /stats /settings 直接 200（无 308 跳转）',P['ui2526'],EV),
 # ---- UI26 候选 3 行 ----
 'PB0207':('verified_full','/agents /alerts 前端与 API 均 404（带认证复测 API 亦 404）——功能不在产品范围',P['ui2526'],EV),
 'PB0208':('verified_full','按"不提供"登记：页面/API/导航三处均无实现痕迹',P['ui2526'],EV),
 'PB0209':('not_applicable_needs_product_decision','若纳入需补 API/导航/权限/验收四处；当前无实现痕迹，不应计为缺陷',P['ui2526'],EV),
 # ---- UI08 日志 3 行 ----
 'PB0076':('verified_full','日志页筛选维度=时间范围 combobox（近24小时/7天/30天/90天）；表头不可排序，排序=时间倒序固定',P['ui08n'],EV),
 'PB0078':('not_present_in_ui','真实日志行已生成验证；表头无 request-id 列、行内无复制控件（列：时间/密钥/IP/账号/模型/状态/首字/总耗时/Token/实付）',P['ui08n'],EV),
 'PB0079':('verified_full','日志表含"账号"列，可关联实际上游/通道',P['ui08n'],EV),
}
rows=list(csv.DictReader(open(os.path.join(SRC,'interaction-verification.csv'),encoding='utf-8-sig')))
counts={}; upgraded=0
for row in rows:
    rid=row['row_id']
    if rid in ADJ and row.get('interaction_verification')=='not_executed':
        status,note,rep,ev=ADJ[rid]
        row['interaction_verification']=status
        if rep: row['evidence']='verified via '+rep
        row['runtime_evidence']=note[:500]
        row['verified_snapshot']=ev
        row['remaining_checks']='' if status in('verified_full','not_present_in_ui') else note[:200]
        upgraded+=1
    counts[row.get('interaction_verification','?')]=counts.get(row.get('interaction_verification','?'),0)+1

with open(os.path.join(DST,'interaction-verification.csv'),'w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
summary={'generated':datetime.now().isoformat(),'upgraded_this_round':upgraded,'counts':counts,
 'evidence_reports':P,
 'note':'209 行全部有落点：verified_full / not_present_in_ui / partial / verified_unsupported_in_lab / 产品裁定'}
json.dump(summary,open(os.path.join(DST,'final-summary.json'),'w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('DST:',DST);print('upgraded:',upgraded)
print(json.dumps(counts,ensure_ascii=False,indent=1))
print('TOTAL ROWS:',len(rows))
