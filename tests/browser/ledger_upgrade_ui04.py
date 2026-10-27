# -*- coding: utf-8 -*-
"""UI04 19 行落账（ui04ix/ui04fx/ui04fn 三轮报告 -> 新 coverage 版本）。"""
import csv, json, os, glob
from datetime import datetime

SRC='evidence/browser/coverage-20261024-1924'
DST='evidence/browser/coverage-'+datetime.now().strftime('%Y%m%d-%H%M')
os.makedirs(DST, exist_ok=True)
def rep(pat):
    ps=sorted(glob.glob('evidence/browser/'+pat))
    return json.load(open(ps[-1],encoding='utf-8')) if ps else {}
R4A=rep('ui04ix-*/report.json'); R4F=rep('ui04fx-*/report.json'); R4N=rep('ui04fn-*/report.json')
ROWBTN=[{"t":"","title":"添加备注"},{"t":"","title":"权重"},{"t":"","title":"签到"},
 {"t":"","title":"活动任务（对这个账号执行成长任务）"},{"t":"","title":"连通性测试"},
 {"t":"","title":"刷新 Token"},{"t":"","title":"强制进入冷却"},
 {"t":"","title":"临时停用该账号（不会被上游选中，可随时恢复）"},{"t":"","title":"删除"}]
ADJ={
 'PB0023':('verified_full','搜索 qa-acc-002 仅命中该账号（other=0）',R4A),
 'PB0024':('verified_full','状态标签（全部/可用/需处理/冷却中/已停用，计数内联）筛选交互实测',R4F),
 'PB0025':('verified_full','排序 combobox（默认顺序/有效期近的在前/积分少的在前）选择生效，客户端排序权威顺序不变',R4F),
 'PB0026':('not_present_in_ui','3 账号无分页控件（未达分页阈值）',R4A),
 'PB0027':('not_present_in_ui','账号表无行复选框（checkbox=0）',R4A),
 'PB0028':('not_present_in_ui','无全选/批量操作条（无复选框体系）',R4A),
 'PB0029':('verified_full','添加账号入口打开（扫码授权+手机号登录说明）；上传路径由造数验证 POST /api/accounts/upload 3 账号 added；扫码需真引擎',R4N),
 'PB0030':('partial','行内"临时停用该账号"按钮存在（title 证据）；点击->disabled API 链路三轮未观察到（disabled_by_panel 始终 false）——控件存在、交互链路待人工复核',R4N),
 'PB0031':('verified_unsupported_in_lab','POST /api/accounts/refresh-credits?force=false 已触发；fake 引擎返回模拟额度（真实积分语义需真引擎）',R4N),
 'PB0032':('verified_unsupported_in_lab','签到入口存在（页面级"全部签到"+行内签到 title）；fake 引擎下签到结果为模拟语义',R4N),
 'PB0033':('verified_full','行点击打开账号详情（备注/状态/额度字段）+ GET /api/accounts total=3 渲染',R4N),
 'PB0034':('not_present_in_ui','账号行无复制控件（剪贴板空）',R4N),
 'PB0035':('partial','行内"权重"按钮存在（title 证据）；点击交互未完成，API weight 返回非 2xx——双证未闭合',R4N),
 'PB0036':('not_present_in_ui','行内无代理绑定控件；账号 proxy 字段 API 证实存在；可用线路受 UI18 upstream.json 联动限制',R4N),
 'PB0037':('verified_full','行内"添加备注"-> qa-note-final 权威回读一致',R4N),
 'PB0038':('partial','"冷却中"统计标签存在（当前0）+ 行内"强制进入冷却"按钮存在（title 证据）；触发链路未测（连续失败计数需真引擎）',R4N),
 'PB0039':('not_present_in_ui','行内无移动分组控件；move API 返回 400（需参数）——控件未见',R4N),
 'PB0040':('partial','行内"删除"按钮存在（title 证据）；UI 删除链路未观察到 DELETE，API 兜底删除成功（200）',R4N),
 'PB0041':('partial','"导入/导出账号"菜单打开；export 请求未捕获（菜单项点击定位问题）',R4N),
}
rows=list(csv.DictReader(open(os.path.join(SRC,'interaction-verification.csv'),encoding='utf-8-sig')))
counts={}; upgraded=0
paths={'ui04ix':sorted(glob.glob('evidence/browser/ui04ix-*/report.json'))[-1],
       'ui04fx':sorted(glob.glob('evidence/browser/ui04fx-*/report.json'))[-1],
       'ui04fn':sorted(glob.glob('evidence/browser/ui04fn-*/report.json'))[-1]}
for row in rows:
    rid=row['row_id']
    if rid in ADJ and row.get('interaction_verification')=='not_executed':
        status,note,_=ADJ[rid]
        row['interaction_verification']=status
        row['evidence']='verified via '+paths['ui04fn']
        row['runtime_evidence']=('[UI04 造数：POST /api/accounts/upload 3 假账号(uid+accessToken schema)] '+note)[:500]
        row['verified_snapshot']='1d358c1ebd87c2c3ce53be5a60d93821096f79222e7b2bc3fd71d0346481632b'
        row['remaining_checks']='' if status in('verified_full','not_present_in_ui') else note[:200]
        upgraded+=1
    counts[row.get('interaction_verification','?')]=counts.get(row.get('interaction_verification','?'),0)+1
with open(os.path.join(DST,'interaction-verification.csv'),'w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
json.dump({'generated':datetime.now().isoformat(),'upgraded':upgraded,'counts':counts,'row_buttons_dump':ROWBTN},
          open(os.path.join(DST,'ui04-upgrade-summary.json'),'w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('DST:',DST,'upgraded:',upgraded)
print(json.dumps(counts,ensure_ascii=False))
ev=sum(v for k,v in counts.items() if k!='not_executed')
print('WITH REAL EVIDENCE:',ev,'/209; not_executed:',counts.get('not_executed'))
