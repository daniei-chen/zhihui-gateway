import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';

test('actual rider script changes wheels, feet, scarf and scenery and honors pause', () => {
  const html = readFileSync(new URL('../public/animations/pelican-ride.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/animations/pelican-ride.html' });
  const w = dom.window;
  const frames = new Map();
  let sequence = 0, listener;
  const media = { matches: false, addEventListener: (_, callback) => { listener = callback; } };
  w.matchMedia = () => media;
  w.requestAnimationFrame = callback => { frames.set(++sequence, callback); return sequence; };
  w.cancelAnimationFrame = id => frames.delete(id);
  const tick = time => { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(time)); };
  const state = () => ['pc-front-spokes','pc-near-foot','pc-scarf-long','pc-strip-0'].map(id => {
    const el = w.document.getElementById(id);
    return el.getAttribute(id === 'pc-scarf-long' ? 'd' : 'transform') || el.parentElement.getAttribute('transform');
  });
  const send = data => w.dispatchEvent(new w.MessageEvent('message', { source: w, data }));
  try {
    for (const script of w.document.scripts) w.eval(script.textContent);
    const before = state();
    tick(100); tick(140); tick(180);
    const after = state();
    after.forEach((value, index) => assert.notEqual(value, before[index]));
    assert.equal(frames.size, 1);
    send({ type: 'pelican-playback', paused: true });
    assert.equal(frames.size, 0);
    assert.equal(w.document.getElementById('pelican-coast').dataset.playback, 'paused');
    media.matches = true; listener();
    send({ type: 'pelican-playback', paused: false });
    assert.equal(frames.size, 0);
    send({ type: 'pelican-playback', paused: false, reducedMotionOverride: true });
    assert.equal(frames.size, 1);
    assert.equal(w.document.getElementById('pelican-coast').dataset.playback, 'playing');
    assert.equal(w.document.querySelectorAll('script[src],link[href],img[src],iframe').length, 0);
    assert.ok(html.includes('--sea-a:#6597c7;--sea-b:#b4d3ec'));
    assert.ok(html.includes('--red:#6387df;--red-light:#a0baf0;--red-dark:#3c60b2'));
    assert.equal(w.document.getElementById('pc-scarf-long').getAttribute('fill'), 'var(--red)');
    assert.equal(w.document.getElementById('pc-scarf-short').getAttribute('fill'), 'var(--red-dark)');
    assert.equal(w.document.getElementById('pc-foreground').getAttribute('preserveAspectRatio'), 'xMidYMax meet');
  } finally { w.close(); }
});
