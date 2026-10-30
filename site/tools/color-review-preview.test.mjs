import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createReviewServer, reviewHtml} from './color-review-preview.mjs';

test('review renders the actual console frames instead of recoloring old screenshots', () => {
  const input = fs.readFileSync(new URL('../home-source/public/index.html', import.meta.url), 'utf8');
  const output = reviewHtml(input);
  assert.equal((output.match(/<iframe class="review-console-frame"/g) || []).length, 9);
  assert.ok(output.includes('/review-palette.css'));
  assert.ok(output.includes('http://127.0.0.1:4189/dashboard/'));
  assert.doesNotMatch(output, /data-preview-panel=/);
  assert.ok(output.includes('本地待审 · 合成数据 · 未上传'));
  assert.ok(output.includes('每一步，都有上下文'));
  assert.ok(output.includes('可旋转三维场景：海绵宝宝骑行与比奇堡街区'));
  assert.ok(output.includes('比奇堡的一天 · 拖动旋转'));
  assert.ok(output.includes('在 AI 的世界，创造一点快乐'));
  assert.ok(output.includes('带着好奇出发，<br>把快乐做出来'));
  assert.doesNotMatch(output, /复杂的留给智汇|就像顺着海风骑行/);
  assert.doesNotMatch(output, /示例吞吐图表/);
});

test('review serves selected colors but never accepts login or API traffic', async () => {
  const server = createReviewServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal((await fetch(origin + '/')).status, 200);
    const css = await fetch(origin + '/review-palette.css');
    assert.equal(css.status, 200);
    assert.match(await css.text(), /--home-blue: #3683fc/);
    for (const path of ['/login', '/api/me']) assert.equal((await fetch(origin + path)).status, 403);
    assert.equal((await fetch(origin + '/', {method: 'POST'})).status, 405);
    assert.equal((await fetch(origin + '/dashboard/', {redirect: 'manual'})).headers.get('Location'), 'http://127.0.0.1:4189/dashboard/');
  } finally {server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
});
