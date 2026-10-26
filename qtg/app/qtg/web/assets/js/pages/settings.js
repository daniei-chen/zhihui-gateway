/* 设置：额度护栏 / 通知 / 备份 / 系统自检 / 安全自检（tab 化，避免 dock 膨胀）。 */
import { api, request } from '../api.js';
import {
  $, badge, card, confirmDialog, el, fmtTokens, num, renderPage, setHead, table, toast,
} from '../ui.js';

let tab = 'limits';
let ops = null;
let settings = null;
let sys = null;
let sec = null;

/* ---------------- 额度 ---------------- */
function limitsForm(limits) {
  return el('div', { class: 'stack' },
    el('label', { class: 'check' }, el('input', { id: 'limEnabled', type: 'checkbox', checked: limits.enabled !== false }), '启用额度护栏（每小时检查 + 告警）'),
    el('div', { class: 'grid cols-3' },
      el('label', { class: 'field' }, el('span', { text: '每日 Token 上限' }), el('input', { id: 'limToken', type: 'number', min: '0', value: limits.daily_token_limit || 0 })),
      el('label', { class: 'field' }, el('span', { text: '每日积分上限' }), el('input', { id: 'limCredit', type: 'number', min: '0', value: limits.daily_credit_limit || 0 })),
      el('label', { class: 'field' }, el('span', { text: '余额地板' }), el('input', { id: 'limReserve', type: 'number', min: '0', value: limits.reserve_credits || 0 })),
    ),
    el('p', { class: 'xs muted', text: '这是平台的软护栏（监控 + 告警）。按密钥的硬性额度在「密钥 → 数据面密钥」里配置日/月限额与并发上限。' }),
    el('button', {
      class: 'btn primary', type: 'button', text: '保存护栏',
      onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        try {
          await api.saveLimits({
            enabled: $('#limEnabled').checked,
            daily_token_limit: Number($('#limToken').value || 0),
            daily_credit_limit: Number($('#limCredit').value || 0),
            reserve_credits: Number($('#limReserve').value || 0),
            model_daily_token_limit: {},
          });
          toast('已保存', '额度护栏', 'ok');
        } catch (e) { toast('保存失败', e.message, 'bad'); }
        finally { b.disabled = false; }
      },
    }),
  );
}

/* ---------------- 通知 ---------------- */
function notifyForm(notify) {
  return el('div', { class: 'stack' },
    el('label', { class: 'check' }, el('input', { id: 'ntEnabled', type: 'checkbox', checked: !!notify.enabled }), '启用通知'),
    el('label', { class: 'field' }, el('span', { text: 'Bark URL' }), el('input', { id: 'ntBark', type: 'text', value: notify.bark_url && notify.bark_url !== '***' ? notify.bark_url : '', placeholder: 'https://api.day.app/xxx' })),
    el('label', { class: 'field' }, el('span', { text: 'ServerChan SendKey' }), el('input', { id: 'ntSc', type: 'text', value: notify.serverchan_sendkey && notify.serverchan_sendkey !== '***' ? notify.serverchan_sendkey : '', placeholder: 'SCT...' })),
    el('label', { class: 'field' }, el('span', { text: 'Webhook URL' }), el('input', { id: 'ntWebhook', type: 'text', value: notify.webhook_url && notify.webhook_url !== '***' ? notify.webhook_url : '', placeholder: 'https://...' })),
    el('p', { class: 'xs muted', text: '已配置的敏感值以 *** 显示；留空表示不改动。' }),
    el('button', {
      class: 'btn primary', type: 'button', text: '保存通知',
      onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const payload = { enabled: $('#ntEnabled').checked };
        const bark = $('#ntBark').value.trim(); if (bark) payload.bark_url = bark;
        const sc = $('#ntSc').value.trim(); if (sc) payload.serverchan_sendkey = sc;
        const wh = $('#ntWebhook').value.trim(); if (wh) payload.webhook_url = wh;
        if (payload.enabled && !bark && !sc && !wh && !(notify.bark_url || notify.serverchan_sendkey || notify.webhook_url)) {
          toast('需要至少一个通道', '', 'warn'); b.disabled = false; return;
        }
        try { await api.saveNotify(payload); toast('已保存', '通知配置', 'ok'); }
        catch (e) { toast('保存失败', e.message, 'bad'); }
        finally { b.disabled = false; }
      },
    }),
  );
}

/* ---------------- 备份 ---------------- */
function backupCard(backups) {
  const rows = [];
  for (const [platform, list] of Object.entries(backups || {})) {
    for (const b of list || []) rows.push({ ...b, platform });
  }
  const runBackup = (target) => async (ev) => {
    const ok = await confirmDialog(`备份 ${target}`, '将调用系统备份脚本，可能需要几十秒。');
    if (!ok) return;
    const b = ev.currentTarget; b.disabled = true; b.textContent = '备份中…';
    try {
      const r = await api.backup(target);
      toast(r.ok ? '备份完成' : '备份失败', (r.data?.output || '').slice(-160), r.ok ? 'ok' : 'bad');
      await load();
    } catch (e) { toast('备份失败', e.message, 'bad'); }
    finally { b.disabled = false; b.textContent = `立即备份 ${target === 'qoder' ? 'Qoder' : 'Trae'}`; }
  };
  return card('备份', el('div', { class: 'stack' },
    el('div', { class: 'row' },
      el('button', { class: 'btn sm', type: 'button', text: '立即备份 Qoder', onclick: runBackup('qoder') }),
      el('button', { class: 'btn sm', type: 'button', text: '立即备份 Trae', onclick: runBackup('trae') }),
    ),
    table([
      { title: '平台', render: (r) => badge(r.platform === 'qoder' ? 'Qoder' : 'Trae', 'info') },
      { title: '文件', render: (r) => el('span', { class: 'mono-sm', text: r.name }) },
      { title: '大小', num: true, render: (r) => `${num(Math.round(r.size / 1024))} KB` },
      { title: '时间', key: 'mtime' },
    ], rows, { empty: '暂无备份' }),
  ));
}

/* ---------------- 系统 ---------------- */
function systemTab() {
  const s = sys || {};
  const svc = s.service || {};
  const rt = s.runtime || {};
  const st = s.storage || {};
  const rel = s.release || {};
  return el('div', { class: 'stack' },
    el('div', { class: 'grid cols-2' },
      card('服务', el('div', {},
        ['version', 'uptime_text', 'pid', 'host', 'port'].map((k) =>
          el('div', { class: 'kv' }, el('span', { class: 'k', text: k }), el('span', { class: 'v mono-sm', text: String(svc[k] ?? '—') }))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '数据面' }), el('span', { class: 'v' }, svc.proxy_enabled ? badge('启用', 'ok') : badge('关闭', 'bad'))),
      )),
      card('运行时', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: 'Python' }), el('span', { class: 'v', text: rt.python || '—' })),
        ...Object.entries(rt.deps || {}).map(([k, v]) =>
          el('div', { class: 'kv' }, el('span', { class: 'k', text: k }), el('span', { class: 'v mono-sm', text: v }))),
      )),
    ),
    el('div', { class: 'grid cols-2' },
      card('存储', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '数据目录' }), el('span', { class: 'v mono-sm', text: st.data_dir || '—' })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '数据大小' }), el('span', { class: 'v', text: fmtTokens(st.data_size || 0) + ' B' })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '审计文件' }), el('span', { class: 'v', text: `${num(st.audit_records)} 条 / ${num(Math.round((st.audit_size || 0) / 1024))} KB` })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '数据库' }), el('span', { class: 'v', text: `${num(Math.round((st.db_size || 0) / 1024))} KB` })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '审计链' }), el('span', { class: 'v' }, st.audit_integrity?.ok ? badge('完整', 'ok') : badge('异常', 'bad'))),
      )),
      card('发布完整性', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '签名清单' }), el('span', { class: 'v' }, rel.present ? badge('存在', 'ok') : badge('缺失', 'warn'))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '分离签名' }), el('span', { class: 'v' }, rel.signed ? badge('已签名', 'ok') : badge('未签名', 'warn'))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '受管文件' }), el('span', { class: 'v', text: num(rel.files || 0) })),
        el('p', { class: 'xs muted', text: '校验命令：sudo /opt/qtg/scripts/verify-release.sh /opt/qtg' }),
      )),
    ),
    card('运行参数', el('div', {},
      ...Object.entries(s.limits || {}).map(([k, v]) =>
        el('div', { class: 'kv' }, el('span', { class: 'k', text: k }), el('span', { class: 'v mono-sm', text: String(v) }))),
    )),
  );
}

/* ---------------- 安全 ---------------- */
function securityTab() {
  const s = sec || {};
  const sess = s.session || {};
  const tok = s.tokens || {};
  const dp = s.data_plane || {};
  const up = s.upstream_secrets || {};
  const hard = s.hardening || {};
  return el('div', { class: 'stack' },
    el('div', { class: 'grid cols-2' },
      card('当前会话', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '认证方式' }), el('span', { class: 'v' }, badge(sess.via || '—', 'info'))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '有效期' }), el('span', { class: 'v', text: `${Math.round((sess.ttl_sec || 0) / 3600)} 小时` })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '读操作需鉴权' }), el('span', { class: 'v' }, tok.read_requires_auth ? badge('是', 'warn') : badge('否', ''))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: 'Cookie Secure' }), el('span', { class: 'v', text: tok.cookie_secure || 'auto' })),
      )),
      card('令牌来源', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '主令牌' }), el('span', { class: 'v mono-sm', text: tok.primary || '—' })),
        ...(tok.compatible || []).map((p) =>
          el('div', { class: 'kv' }, el('span', { class: 'k', text: '兼容令牌' }), el('span', { class: 'v mono-sm', text: p }))),
        el('p', { class: 'xs muted', text: '兼容令牌用于升级过渡，确认新版可用后建议从配置中移除。' }),
      )),
    ),
    el('div', { class: 'grid cols-2' },
      card('数据面密钥', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '密钥总数' }), el('span', { class: 'v', text: num(dp.keys_total) })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '设日限额' }), el('span', { class: 'v', text: num(dp.keys_with_daily_limit) })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '设月限额' }), el('span', { class: 'v', text: num(dp.keys_with_monthly_limit) })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '设模型白名单' }), el('span', { class: 'v', text: num(dp.keys_with_model_allowlist) })),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: '设并发上限' }), el('span', { class: 'v', text: num(dp.keys_with_concurrency_limit) })),
      )),
      card('上游凭据', el('div', {},
        el('div', { class: 'kv' }, el('span', { class: 'k', text: 'Qoder' }), el('span', { class: 'v' }, up.qoder_configured ? badge('已配置', 'ok') : badge('未配置', 'bad'))),
        el('div', { class: 'kv' }, el('span', { class: 'k', text: 'Trae' }), el('span', { class: 'v' }, up.trae_configured ? badge('已配置', 'ok') : badge('未配置', 'bad'))),
        el('p', { class: 'xs mono-sm muted', text: up.qoder_key_file || '' }),
        el('p', { class: 'xs mono-sm muted', text: up.trae_key_file || '' }),
      )),
    ),
    card('加固基线', el('div', {},
      ...Object.entries(hard).map(([k, v]) =>
        el('div', { class: 'kv' }, el('span', { class: 'k', text: k }), el('span', { class: 'v mono-sm', text: String(v) }))),
    )),
  );
}

/* ---------------- 加载与渲染 ---------------- */
async function load() {
  try {
    const [o, s] = await Promise.all([api.ops(), api.settings()]);
    ops = o; settings = s;
    sys = await request('/system');
    sec = await request('/security');
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  setHead('设置', `审计链 ${ops.audit?.records || 0} 条 · 完整性 ${ops.audit?.integrity?.ok ? '正常' : '异常'}`);

  const tabs = el('div', { class: 'tabs' },
    ...[['limits', '额度护栏'], ['notify', '通知'], ['backup', '备份'], ['system', '系统'], ['security', '安全']].map(([k, label]) =>
      el('button', {
        class: 'tab', type: 'button', 'aria-selected': tab === k ? 'true' : 'false', text: label,
        onclick: () => { tab = k; load(); },
      })));

  const body = tab === 'limits' ? limitsForm(ops.limits || {})
    : tab === 'notify' ? notifyForm(ops.notify || {})
      : tab === 'backup' ? backupCard(ops.backups)
        : tab === 'system' ? systemTab()
          : securityTab();

  renderPage(
    el('div', { class: 'card' }, tabs, el('div', { style: 'margin-top:14px' }, body)),
    card('定时器', el('div', { class: 'list' },
      ...((ops.timers || []).length ? ops.timers.map((t) => el('div', { class: 'list-item' }, badge('timer', 'info'), el('span', { class: 'mono-sm', text: t })))
        : [el('div', { class: 'empty', text: '未发现相关定时器' })]))),
  );
}

export default async function page() {
  await load();
  return () => {};
}
