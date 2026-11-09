import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {productionHtml} from './prepare-color-release.mjs';

test('approved design publication removes local endpoints and isolates public console examples', () => {
  const source = fs.readFileSync(new URL('../home-source/public/index.html', import.meta.url), 'utf8');
  const result = productionHtml(source, '/home-assets/console-flow-20261030-1');
  assert.equal((result.match(/class="public-console-frame"/g) || []).length, 1);
  const frames = result.match(/<iframe class="public-console-frame"[^>]+>/g) || [];
  assert.ok(frames.every(frame => frame.includes('sandbox="allow-scripts" referrerpolicy="no-referrer"')));
  assert.doesNotMatch(result, /127\.0\.0\.1|localhost|本地待审|allow-same-origin/);
  assert.match(result, /href="\/dashboard\/"/);
  assert.match(result, /scripts\/connected-paths\.js/);
  assert.match(result, /styles\/color-release\.css/);
  assert.match(result, /把快乐，做进作品/);
  assert.doesNotMatch(result, /id="console"|海绵宝宝|比奇堡|inspiration-flow/);
  assert.throws(() => productionHtml(source, 'https://external.invalid'));
});
