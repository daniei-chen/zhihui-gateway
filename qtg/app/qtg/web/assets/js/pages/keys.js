/* 密钥：三块 —— 数据面密钥（qtg 发行）、上游凭据（供网关转发）、上游面板密钥（只读）。 */
import { api } from '../api.js';
import {
  $, badge, card, confirmDialog, el, modal, renderPage, setHead, table, toast,
} from '../ui.js';

let tab = 'qtg';
let qtgKeys = [];
let upKeys = { qoder: [], trae: [] };
let dataPlane = null;

/* ---------------- 数据面密钥 ---------------- */
function createKeyDialog() {
  const body = el('div', { class: 'stack' },
    el('p', { class: 'muted sm', text: '创建一个 sk-qtg-* 密钥，客户端用它访问统一入口；上游凭据由网关保管。' }),
    el('label', { class: 'field' }, el('span', { text: '名称' }), el('input', { id: 'nkName', type: 'text', placeholder: '例如：我的应用' })),
    el('label', { class: 'field' }, el('span', { text: '允许平台' }),
      el('div', { class: 'row' },
        el('label', { class: 'check' }, el('input', { id: 'nkQoder', type: 'checkbox', checked: true }), 'Qoder'),
        el('label', { class: 'check' }, el('input', { id: 'nkTrae', type: 'checkbox', checked: true }), 'Trae'),
      )),
    el('label', { class: 'field' }, el('span', { text: '每日 Token 上限（0 = 不限）' }), el('input', { id: 'nkLimit', type: 'number', min: '0', value: '0' })),
    el('label', { class: 'field' }, el('span', { text: '模型白名单（逗号分隔，留空 = 全部）' }), el('input', { id: 'nkModels', type: 'text', placeholder: 'Auto,glm-5.3' })),
  );
  modal({
    title: '新建数据面密钥', body,
    actions: [
      { label: '取消' },
      {
        label: '创建', kind: 'primary',
        onClick: async () => {
          const platforms = [];
          if ($('#nkQoder').checked) platforms.push('qoder');
          if ($('#nkTrae').checked) platforms.push('trae');
          if (!platforms.length) { toast('至少选一个平台', '', 'warn'); return true; }
          try {
            const r = await api.createQtgKey({
              name: $('#nkName').value.trim(),
              platforms,
              daily_limit: Number($('#nkLimit').value || 0),
              models: $('#nkModels').value.split(',').map((x) => x.trim()).filter(Boolean),
            });
            modal({
              title: '密钥已创建（只显示一次）',
              body: el('div', { class: 'stack' },
                el('p', { class: 'muted sm', text: '请立即复制保存。关闭后无法再次查看明文。' }),
                el('pre', { class: 'mono-sm', style: 'margin:0;padding:12px;background:var(--muted);border-radius:10px;word-break:break-all;white-space:pre-wrap', text: r.data.key }),
                el('div', { class: 'row' },
                  el('button', { class: 'btn sm primary', type: 'button', text: '复制', onclick: async () => {
                    try { await navigator.clipboard.writeText(r.data.key); toast('已复制', '', 'ok'); }
                    catch { toast('复制失败', '请手动选择', 'warn'); }
                  } }),
                ),
              ),
              actions: [{ label: '我已保存' }],
            });
            await load(true);
          } catch (e) {
            toast('创建失败', e.message, 'bad');
            return true;
          }
          return false;
        },
      },
    ],
  });
}

function qtgKeyTable() {
  return table([
    { title: '名称', render: (r) => el('div', {}, el('div', { style: 'font-weight:600', text: r.name || '未命名' }), el('div', { class: 'xs muted mono-sm', text: r.masked })) },
    { title: '平台', render: (r) => el('div', { class: 'row', style: 'gap:4px' }, ...(r.platforms || []).map((p) => badge(p === 'qoder' ? 'Qoder' : 'Trae', 'info'))) },
    { title: '模型', render: (r) => (r.models?.length ? el('span', { class: 'xs', text: r.models.join(', ') }) : el('span', { class: 'xs muted', text: '全部' })) },
    { title: '日限额', num: true, render: (r) => (r.daily_limit ? String(r.daily_limit) : '不限') },
    { title: '状态', render: (r) => (r.enabled ? badge('启用', 'ok') : badge('停用', 'bad')) },
    { title: '最近使用', render: (r) => el('span', { class: 'xs muted', text: r.last_used_at || '—' }) },
    {
      title: '操作',
      render: (r) => el('div', { class: 'row', style: 'gap:4px' },
        el('button', {
          class: 'btn sm', type: 'button', text: r.enabled ? '停用' : '启用',
          onclick: async (ev) => {
            ev.currentTarget.disabled = true;
            try { await api.qtgKeyOp(r.id, r.enabled ? 'disable' : 'enable'); await load(true); }
            catch (e) { toast('操作失败', e.message, 'bad'); }
            finally { ev.currentTarget.disabled = false; }
          },
        }),
        el('button', {
          class: 'btn sm danger', type: 'button', text: '删除',
          onclick: async () => {
            const ok = await confirmDialog('删除密钥', `确认删除「${r.name}」？使用该密钥的客户端会立即失效。`, { danger: true, confirmText: '删除' });
            if (!ok) return;
            try { await api.qtgKeyOp(r.id, 'delete'); toast('已删除', '', 'ok'); await load(true); }
            catch (e) { toast('删除失败', e.message, 'bad'); }
          },
        }),
      ),
    },
  ], qtgKeys, { empty: '还没有数据面密钥，点右上「新建密钥」' });
}

/* ---------------- 上游凭据 ---------------- */
function upstreamPanel() {
  const form = (platform, info) => el('div', { class: 'stack' },
    el('div', { class: 'row between' },
      el('h4', { style: 'margin:0;font-size:13px', text: platform === 'qoder' ? 'Qoder 上游 Key' : 'Trae 上游 Key' }),
      info.configured ? badge('已配置', 'ok') : badge('未配置', 'bad'),
    ),
    el('p', { class: 'xs muted', text: `模型 ${info.models} 个 · 文件 ${info.key_file}` }),
    el('div', { class: 'row' },
      el('input', { id: `up-${platform}`, type: 'password', placeholder: info.configured ? '已配置，重新填写可覆盖' : '粘贴上游 API Key', style: 'flex:1' }),
      el('button', {
        class: 'btn sm primary', type: 'button', text: '保存',
        onclick: async (ev) => {
          const v = $(`#up-${platform}`).value.trim();
          if (!v) { toast('请输入 Key', '', 'warn'); return; }
          ev.currentTarget.disabled = true;
          try {
            const r = await api.saveUpstreamKey(platform, v);
            toast(r.ok ? '已保存' : '保存失败', r.message || '', r.ok ? 'ok' : 'bad');
            $(`#up-${platform}`).value = '';
            await load(true);
          } catch (e) { toast('保存失败', e.message, 'bad'); }
          finally { ev.currentTarget.disabled = false; }
        },
      }),
    ),
  );
  const up = dataPlane?.upstreams || {};
  return card('上游凭据（供网关转发用）', el('div', { class: 'stack' },
    el('p', { class: 'sm muted', text: '网关转发时使用这里配置的上游 Key。它们只写本机 0600 文件，不会下发到客户端。' }),
    form('qoder', up.qoder || {}),
    form('trae', up.trae || {}),
    el('p', { class: 'xs muted', text: '获取方式：在 Qoder/Trae 面板的「API 服务/密钥」页新建一个 Key，粘贴到这里。' }),
  ));
}

/* ---------------- 上游面板密钥（只读） ---------------- */
function upstreamKeysCard() {
  const rows = [
    ...(upKeys.qoder || []).map((k) => ({ ...k, platform: 'Qoder' })),
    ...(upKeys.trae || []).map((k) => ({ ...k, platform: 'Trae' })),
  ];
  return card('上游面板密钥（只读）', table([
    { title: '平台', render: (r) => badge(r.platform, 'info') },
    { title: '名称', key: 'name' },
    { title: '掩码', render: (r) => el('span', { class: 'mono-sm', text: r.masked || '—' }) },
    { title: '状态', render: (r) => (r.enabled ? badge('启用', 'ok') : badge('停用', 'bad')) },
    { title: '创建', key: 'created_at' },
  ], rows, { empty: '上游未配置任何密钥' }), { sub: '由 BFF 管理，本面板只读（BFF 不返回明文）' });
}

/* ---------------- 加载 ---------------- */
let rendered = false;
async function load(force = false) {
  try {
    [dataPlane, qtgKeys] = await Promise.all([api.dataPlane(), api.qtgKeys().then((r) => r.keys)]);
    const ks = await api.keys();
    upKeys = { qoder: ks.qoder || [], trae: ks.trae || [] };
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  rendered = true;

  const tabs = el('div', { class: 'tabs' },
    ...[['qtg', `数据面密钥 ${qtgKeys.length}`], ['upstream', '上游凭据'], ['legacy', '上游面板密钥']].map(([k, label]) =>
      el('button', {
        class: 'tab', type: 'button', 'aria-selected': tab === k ? 'true' : 'false', text: label,
        onclick: () => { tab = k; load(false); },
      })),
  );

  setHead('密钥', `数据面 ${qtgKeys.length} 个 · 上游 ${dataPlane?.upstreams?.qoder?.configured ? 'Qoder✓' : 'Qoder✗'} ${dataPlane?.upstreams?.trae?.configured ? 'Trae✓' : 'Trae✗'}`,
    tab === 'qtg' ? el('button', { class: 'btn sm primary', type: 'button', text: '新建密钥', onclick: createKeyDialog }) : null,
  );

  renderPage(
    el('div', { class: 'card' },
      tabs,
      el('div', { style: 'margin-top:12px' },
        tab === 'qtg' ? qtgKeyTable()
          : tab === 'upstream' ? el('div', {}, upstreamPanel())
            : upstreamKeysCard()),
    ),
    card('统一接入示例', el('pre', {
      class: 'mono-sm',
      style: 'margin:0;overflow:auto;padding:12px;background:var(--muted);border-radius:12px',
      text: [
        '# 一个 base URL + 一个 sk-qtg-* 密钥即可访问 Qoder / Trae',
        `export OPENAI_BASE_URL=${dataPlane?.base_url || 'http://127.0.0.1:8795/v1'}`,
        'export OPENAI_API_KEY=sk-qtg-...',
        '',
        '# 自动路由（按余额选平台）：',
        'curl $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \\',
        '  -H "Content-Type: application/json" \\',
        '  -d \'{"model":"Auto","messages":[{"role":"user","content":"hi"}]}\'',
        '',
        '# 指定平台：',
        '  "model":"qoder/Auto"   或   "model":"trae/glm-5.3"',
      ].join('\n'),
    }), { sub: 'OpenAI 兼容' }),
  );
}

export default async function page() {
  rendered = false;
  tab = 'qtg';
  await load(false);
  return () => {};
}
