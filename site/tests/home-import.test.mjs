import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createPreviewServer, siteRoot, homeCsp, loginCsp, sceneCsp} from '../tools/preview-server.mjs';

const manifest = JSON.parse(fs.readFileSync(path.join(siteRoot, 'home-import.json'), 'utf8'));
const prefix = manifest.asset_prefix;
test('hero arrival never hides essential content on the first paint', () => {
  const cssPath = 'home/' + prefix.slice('/home-assets/'.length) + 'styles/motion.css';
  const css = fs.readFileSync(path.join(siteRoot, cssPath), 'utf8');
  assert.match(css, /#top\s+\.hero-copy\s*\{\s*animation:\s*none\s*!important\s*;/,
    'the inherited parent fade must remain disabled as well');
  const arrival = css.match(/@keyframes\s+zh-arrive\s*\{\s*from\s*\{([^}]+)\}\s*to\s*\{([^}]+)\}\s*\}/);
  assert.ok(arrival, 'the reviewed arrival animation must remain explicit');
  for (const frame of arrival.slice(1)) {
    assert.match(frame, /opacity\s*:\s*1\s*;/);
    assert.doesNotMatch(frame, /visibility\s*:\s*hidden|display\s*:\s*none/);
  }
});
test('all imported bytes match the manifest and use a content-derived namespace', () => {
  assert.match(prefix, /^\/home-assets\/r-[a-f0-9]{20}\/$/);
  assert.equal(prefix, `/home-assets/r-${manifest.source_tree_sha256.slice(0, 20)}/`);
  for (const row of manifest.files) {
    const file = path.resolve(siteRoot, row.path);
    assert.ok(file.startsWith(siteRoot + path.sep));
    const data = fs.readFileSync(file);
    assert.equal(data.length, row.bytes, row.path);
    assert.equal(crypto.createHash('sha256').update(data).digest('hex'), row.sha256, row.path);
  }
});

test('production-like routes serve every local reference with strict MIME and CSP', async () => {
  const server = createPreviewServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const urls = new Set();
    for (const row of manifest.files) {
      if (!/\.(html|css|js)$/.test(row.path)) continue;
      const text = fs.readFileSync(path.join(siteRoot, row.path), 'utf8');
      for (const match of text.matchAll(/\/home-assets\/[A-Za-z0-9_./-]+/g)) {
        if (match[0].startsWith('/home-assets/console-flow-20261030-1/')) {
          assert.equal(row.path, 'home/index.html', 'Public demo links belong only to the parent page');
          assert.match(text, /sandbox="allow-scripts" referrerpolicy="no-referrer"/);
          continue; // Separate export closure is checked by the release publisher.
        }
        assert.ok(match[0].startsWith(prefix), `${row.path}: unversioned asset`);
        if (!match[0].endsWith('/')) urls.add(match[0]);
      }
      // /models/ is an intentional console navigation target, not a model asset.
      assert.doesNotMatch(text, /['"]\/(?:models\/[^'"]|(?:vendor|scripts|styles|animations)\/)/, row.path);
    }
    for (const url of urls) {
      const response = await fetch(origin + url, {method: 'HEAD'});
      assert.equal(response.status, 200, url);
      if (url.endsWith('.js')) assert.match(response.headers.get('content-type'), /javascript/);
      if (url.endsWith('.css')) assert.match(response.headers.get('content-type'), /text\/css/);
    }
    const response = await fetch(origin);
    assert.equal(response.headers.get('content-security-policy'), homeCsp);
    assert.ok(!homeCsp.includes("'unsafe-eval'"));
    assert.match(homeCsp, /frame-src 'self'/);
    for (const scene of ['task-workspace', 'velo-coast']) {
      const child = await fetch(origin + prefix + `animations/${scene}.html`);
      assert.equal(child.headers.get('x-frame-options'), 'SAMEORIGIN');
      assert.equal(child.headers.get('content-security-policy'), sceneCsp);
      assert.match(sceneCsp, /connect-src 'none'/);
    }
    assert.equal((await fetch(origin + '/models/thinker.glb')).status, 404);
    assert.equal((await fetch(origin + '/api/me')).status, 403);
    assert.equal((await fetch(origin + '/api/login', {method: 'POST', body: '{}'})).status, 405);
    const login = await fetch(origin + '/login');
    const html = await login.text();
    assert.match(html, /三平台/);
    assert.doesNotMatch(html, /四网关|Codex/);
    assert.match(html, /class="auth-card"/);
    assert.doesNotMatch(html, /gateway-story|gateway-viewer\.js|styles\/login\.css/);
    assert.match(html, /\/__preview__\/login-guard\.js/);
    assert.equal(login.headers.get('content-security-policy'), loginCsp);
    assert.match(loginCsp, /connect-src 'none'/);
    assert.doesNotMatch(loginCsp, /script-src[^;]*'unsafe-inline'/);
    const guard = await (await fetch(origin + '/__preview__/login-guard.js')).text();
    assert.match(guard, /window\.fetch=async/);
    assert.doesNotMatch(guard, /fetch\(/);
    assert.equal((await fetch(origin + '/favicon/zhihui-z-20261029/favicon.svg')).status, 200);
    assert.equal(login.headers.get('x-frame-options'), 'DENY');
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('login is byte-identical to the serving original', () => {
  const original = JSON.parse(fs.readFileSync(path.join(siteRoot, 'login/preservation.json'), 'utf8'));
  const raw = fs.readFileSync(path.join(siteRoot, 'login/index.html'));
  assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), original.candidate_sha256);
  let reverted = raw.toString('utf8');
  assert.equal(original.permitted_text_changes.length, 0);
  for (const change of original.permitted_text_changes) {
    assert.equal(reverted.split(change.to).length - 1, 1);
    reverted = reverted.replace(change.to, change.from);
  }
  assert.equal(crypto.createHash('sha256').update(reverted).digest('hex'), original.source_sha256);
  assert.equal(manifest.login.sha256, original.candidate_sha256);
  assert.equal(manifest.files.some(row => row.path === 'login/index.html'), false);
});
