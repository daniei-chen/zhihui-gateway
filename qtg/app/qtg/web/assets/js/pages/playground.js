/* Playground：用数据面密钥直接试跑统一入口，实时看流式输出/首字延迟/用量。
   这是验证「一个 base URL 打通两平台」最直接的方式。 */
import { api } from '../api.js';
import { $, badge, card, el, renderPage, setHead, toast } from '../ui.js';

const KEY_STORE = 'qtg-dp-key';
let models = [];
let running = false;

function apiKey() {
  return (localStorage.getItem(KEY_STORE) || '').trim();
}

async function loadModels() {
  const key = apiKey();
  if (!key) return [];
  try {
    const res = await fetch('v1/models', { headers: { Authorization: 'Bearer ' + key }, cache: 'no-store' });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(`HTTP ${res.status} ${t.slice(0, 120)}`);
    }
    const data = await res.json();
    return (data.data || []).map((m) => ({ id: m.id, platform: m.platform, upstream: m.upstream_id }));
  } catch (e) {
    toast('模型列表获取失败', e.message, 'bad');
    return [];
  }
}

function paintModels() {
  const sel = $('#pgModel');
  if (!sel) return;
  sel.replaceChildren(
    el('option', { value: '', text: models.length ? '选择模型…' : '（先保存密钥后加载）' }),
    ...models.map((m) => el('option', { value: m.id, text: `${m.platform === 'qoder' ? 'Qoder' : 'Trae'} · ${m.upstream}` })),
  );
}

async function run() {
  if (running) return;
  const key = apiKey();
  const model = $('#pgModel')?.value;
  const prompt = $('#pgPrompt')?.value.trim();
  const stream = $('#pgStream')?.checked !== false;
  const out = $('#pgOutput');
  const stat = $('#pgStat');
  if (!key) { toast('请先保存数据面密钥', '', 'warn'); return; }
  if (!model) { toast('请选择模型', '', 'warn'); return; }
  if (!prompt) { toast('请输入内容', '', 'warn'); return; }

  running = true;
  $('#pgRun').disabled = true;
  $('#pgRun').textContent = '生成中…';
  out.textContent = '';
  stat.textContent = '请求中…';
  const t0 = performance.now();
  let ttft = 0;
  let text = '';
  let usage = null;

  try {
    const res = await fetch('v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key },
      body: JSON.stringify({ model, messages: [{ role: 'user', content: prompt }], stream }),
    });
    const platform = res.headers.get('x-qtg-platform') || '';
    const route = res.headers.get('x-qtg-route') || '';
    if (!res.ok) {
      const t = await res.text();
      throw new Error(`HTTP ${res.status} ${t.slice(0, 300)}`);
    }
    if (!stream) {
      const data = await res.json();
      text = data.choices?.[0]?.message?.content || JSON.stringify(data).slice(0, 500);
      usage = data.usage;
      out.textContent = text;
      ttft = Math.round(performance.now() - t0);
    } else {
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          const s = line.trim();
          if (!s.startsWith('data:')) continue;
          const payload = s.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            if (obj.usage) usage = obj.usage;
            const delta = obj.choices?.[0]?.delta?.content || '';
            if (delta) {
              if (!ttft) ttft = Math.round(performance.now() - t0);
              text += delta;
              out.textContent = text;
            }
          } catch { /* 忽略不完整分片 */ }
        }
      }
    }
    const total = Math.round(performance.now() - t0);
    stat.replaceChildren(
      badge(platform ? `路由 ${platform}` : '路由 ?', 'info'),
      badge(`理由 ${route || '—'}`, ''),
      badge(`首字 ${ttft} ms`, ttft > 2000 ? 'warn' : 'ok'),
      badge(`总耗时 ${total} ms`, ''),
      badge(`tokens ${usage ? (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) : '—'}`, ''),
      badge(`字数 ${text.length}`, ''),
    );
  } catch (e) {
    out.textContent = '请求失败：' + e.message;
    stat.replaceChildren(badge('失败', 'bad'));
  } finally {
    running = false;
    $('#pgRun').disabled = false;
    $('#pgRun').textContent = '运行';
  }
}

async function load() {
  const dp = await api.dataPlane().catch(() => null);
  const up = dp?.upstreams || {};
  models = await loadModels();

  setHead('Playground', '直接调用统一入口，验证路由与流式输出',
    badge(up.qoder?.configured ? 'Qoder 就绪' : 'Qoder 未配置', up.qoder?.configured ? 'ok' : 'bad'),
    badge(up.trae?.configured ? 'Trae 就绪' : 'Trae 未配置', up.trae?.configured ? 'ok' : 'bad'),
  );

  const keyInput = el('input', { id: 'pgKey', type: 'password', placeholder: 'sk-qtg-...（数据面密钥）', value: apiKey(), style: 'flex:1' });
  renderPage(
    card('数据面密钥', el('div', { class: 'stack' },
      el('div', { class: 'row' },
        keyInput,
        el('button', {
          class: 'btn sm primary', type: 'button', text: '保存并加载模型',
          onclick: async () => {
            const v = keyInput.value.trim();
            if (!v) { toast('请输入密钥', '', 'warn'); return; }
            localStorage.setItem(KEY_STORE, v);
            models = await loadModels();
            paintModels();
            toast('已保存', `加载到 ${models.length} 个模型`, models.length ? 'ok' : 'warn');
          },
        }),
        el('a', { href: '#/keys', class: 'btn sm', text: '去创建密钥' }),
      ),
      el('p', { class: 'xs muted', text: '密钥只存在本机浏览器 localStorage，不会写入服务端。' }),
    )),
    card('试跑', el('div', { class: 'stack' },
      el('div', { class: 'row' },
        el('select', { id: 'pgModel', style: 'flex:1', 'aria-label': '模型' }),
        el('label', { class: 'check' }, el('input', { id: 'pgStream', type: 'checkbox', checked: true }), '流式'),
      ),
      el('textarea', { id: 'pgPrompt', placeholder: '输入问题，例如：用一句话介绍你自己', style: 'min-height:90px' }),
      el('div', { class: 'row' },
        el('button', { class: 'btn primary', type: 'button', id: 'pgRun', text: '运行', onclick: run }),
        el('span', { id: 'pgStat', class: 'row', style: 'gap:6px' }),
      ),
      el('pre', {
        id: 'pgOutput',
        class: 'mono-sm',
        style: 'margin:0;min-height:120px;max-height:420px;overflow:auto;padding:12px;background:var(--muted);border-radius:12px;white-space:pre-wrap;word-break:break-word',
      }),
    ), { sub: 'OpenAI 兼容 /v1/chat/completions' }),
  );
  paintModels();
}

export default async function page() {
  await load();
  return () => {};
}
