import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
// Upstream TS intentionally keeps its original .js import specifiers. Use the same bundler as production.
const compiled = await build({stdin:{contents:`export {startInspirationViewer,FPS} from './src/inspiration-viewer.js';
  export {renderSize,MAX_PIXELS,createInspirationRenderer} from './src/inspiration-renderer.js';`,
  resolveDir:fileURLToPath(new URL('..',import.meta.url))},bundle:true,write:false,format:'esm'});
const {startInspirationViewer,FPS,renderSize,MAX_PIXELS,createInspirationRenderer} = await import(
  'data:text/javascript;base64,'+Buffer.from(compiled.outputFiles[0].text).toString('base64'));

const html = readFileSync(new URL('../scenes/inspiration-flow.html', import.meta.url), 'utf8');
function fixture({failure = false, reduced = false, pending = false} = {}) {
  const dom = new JSDOM(html, {pretendToBeVisual:true, runScripts:'outside-only'}), w = dom.window;
  const frames = new Map(), draws = [], observers = [], callbacks = [];
  let serial = 0, hidden = false, created = 0, disposed = 0, finish;
  const media = new w.EventTarget(); media.matches = reduced;
  w.matchMedia = () => media;
  Object.defineProperty(w.document, 'hidden', {get:() => hidden});
  const parent = {postMessage(value) { callbacks.push(value); }};
  Object.defineProperty(w, 'parent', {value:parent});
  const surface = w.document.getElementById('inspirationSurface');
  Object.defineProperty(surface,'clientWidth',{value:900});
  Object.defineProperty(surface,'clientHeight',{value:490});
  surface.getBoundingClientRect = () => ({left:0,top:0,width:900,height:490});
  w.requestAnimationFrame = fn => { frames.set(++serial,fn); return serial; };
  w.cancelAnimationFrame = id => frames.delete(id);
  w.IntersectionObserver = class { constructor(fn) { this.fn=fn;observers.push(this); } observe() {} disconnect() {this.disconnected=true;} };
  const viewer = startInspirationViewer({win:w,doc:w.document,rendererFactory:() => {
    created++;
    if(failure) throw Error('Synthetic no WebGL');
    return {ready:pending ? new Promise(resolve=>{finish=resolve;}) : Promise.resolve(),
      render(data) { draws.push(data); },dispose(){disposed++;}};
  }});
  return {w,frames,draws,observers,callbacks,media,viewer,parent,surface,
    get created(){return created;},get disposed(){return disposed;},finish:()=>finish?.(),
    message(data,source=parent){w.dispatchEvent(new w.MessageEvent('message',{data,source}));},
    async flush(){await Promise.resolve();await Promise.resolve();},
    tick(now){const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn(now));},
    hide(value){hidden=value;w.document.dispatchEvent(new w.Event('visibilitychange'));},
    reduce(value){media.matches=value;media.dispatchEvent(new w.Event('change'));},
    close(){viewer.dispose();w.close();}};
}
const play = {type:'pelican-playback',paused:false};

test('new scene is local, accessible, has a real poster and no 3D model or remote requests', () => {
  assert.match(html, /inspiration-poster\.webp/);
  assert.match(html, /role="img" aria-label=/);
  assert.match(html, /touch-action:pan-y/);
  assert.match(html, /prefers-reduced-motion:reduce/);
  assert.doesNotMatch(html,/https?:|coastal|SpongeBob|pelican-ride\.html|\.glb/);
  assert.equal((html.match(/<script /g)||[]).length,1);
});
test('pixel budget never increases DPR or allocates giant/mobile hidden surfaces', () => {
  for(const [w,h] of [[320,360],[900,490],[4000,4000],[1,999999],[0,0],[NaN,Infinity]]) {
    const [rw,rh]=renderSize(w,h);
    assert.ok(rw>=1 && rh>=1);
    assert.ok(rw*rh <= MAX_PIXELS);
    assert.ok(rw<=4096 && rh<=4096);
  }
  assert.deepEqual(renderSize(320,360),[320,360]);
});
test('renderer requests one bounded low-power WebGL2 context; unsupported devices fail cleanly', () => {
  let options;
  assert.throws(()=>createInspirationRenderer({getContext(name,config){assert.equal(name,'webgl2');options=config;return null;}}),/unavailable/);
  assert.deepEqual(options,{alpha:false,antialias:false,depth:false,stencil:false,preserveDrawingBuffer:false,powerPreference:'low-power'});
});
test('strict parent and boolean playback fencing precedes all GPU allocation', async () => {
  const f=fixture();try {
    assert.equal(f.created,0);assert.equal(f.frames.size,0);
    assert.equal(f.callbacks[0].type,'pelican-ready');
    f.message(play,{});f.message({type:'pelican-playback',paused:'false'});await f.flush();
    assert.equal(f.created,0);
    f.message(play);await f.flush();assert.equal(f.created,1);assert.equal(f.frames.size,1);
    f.tick(0);assert.equal(f.w.document.documentElement.dataset.scene,'ready');
  }finally{f.close();}
});
test('24fps ceiling, pause, offscreen and background stop all draws and never accumulate catch-up time', async () => {
  const f=fixture();try {
    f.message(play);await f.flush();
    for(let time=0;time<=1000;time+=5)f.tick(time);
    assert.ok(f.draws.length<=FPS+1,f.draws.length);assert.ok(f.draws.length>15);
    f.message({...play,paused:true});const before=f.draws.length;assert.equal(f.frames.size,0);
    f.tick(2000);assert.equal(f.draws.length,before);
    f.message(play);f.hide(true);assert.equal(f.frames.size,0);
    f.hide(false);assert.equal(f.frames.size,1);
    f.observers[0].fn([{isIntersecting:false}]);assert.equal(f.frames.size,0);
    f.observers[0].fn([{isIntersecting:true}]);f.tick(90000);
    assert.equal(f.draws.at(-1).time,f.draws.at(-2).time);
    f.viewer.dispose();assert.equal(f.frames.size,0);assert.equal(f.disposed,1);
    assert.equal(f.observers[0].disconnected,true);
    f.message(play);assert.equal(f.frames.size,0);
  }finally{f.close();}
});
test('reduced motion defaults to static poster with zero GPU work; only explicit parent override starts it', async () => {
  const f=fixture({reduced:true});try {
    f.message(play);await f.flush();assert.equal(f.created,0);
    f.message({...play,reducedMotionOverride:true});await f.flush();assert.equal(f.created,1);
    f.tick(0);f.message(play);assert.equal(f.frames.size,0);
    f.reduce(false);assert.equal(f.frames.size,1);
  }finally{f.close();}
});
test('shape buttons stay useful when paused, pointer never captures touch scroll, and modes stay finite', async () => {
  const f=fixture();try {
    f.message(play);await f.flush();f.tick(0);
    f.message({...play,paused:true});
    const button=f.w.document.querySelector('[data-flow-mode="open"]');button.click();
    assert.equal(f.draws.at(-1).mode,'open');assert.equal(button.getAttribute('aria-pressed'),'true');
    assert.equal(f.frames.size,0);
    button.dataset.flowMode='arbitrary';button.click();assert.equal(f.draws.at(-1).mode,'open');
    f.message(play);
    f.surface.dispatchEvent(new f.w.MouseEvent('pointermove',{clientX:99999,clientY:-99999}));
    f.tick(50);assert.ok(f.draws.at(-1).pointer.every(v=>Math.abs(v)<=.0275));
  }finally{f.close();}
});
test('initialization failure and context loss keep the poster and disable unusable controls', async () => {
  for(const initialFailure of [true,false]) {
    const f=fixture({failure:initialFailure});try {
      f.message(play);await f.flush();
      if(!initialFailure){f.tick(0);f.w.document.querySelector('canvas').dispatchEvent(new f.w.Event('webglcontextlost',{cancelable:true}));}
      assert.equal(f.w.document.documentElement.dataset.scene,'fallback');assert.equal(f.frames.size,0);
      assert.ok([...f.w.document.querySelectorAll('button')].every(b=>b.disabled));
      assert.match(f.w.document.getElementById('inspirationStatus').textContent,/静态/);
    }finally{f.close();}
  }
});
test('late texture completion after teardown cannot revive a scene or schedule frames', async () => {
  const f=fixture({pending:true});try {
    f.message(play);f.viewer.dispose();f.finish();await f.flush();
    assert.equal(f.frames.size,0);assert.equal(f.disposed,1);assert.equal(f.draws.length,0);
  }finally{f.close();}
});
test('bfcache suspension is reversible but final pagehide disposes exactly once', async () => {
  const f=fixture();try {
    f.message(play);await f.flush();f.tick(0);
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pagehide',{persisted:true}));
    assert.equal(f.frames.size,0);assert.equal(f.disposed,0);
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pageshow',{persisted:true}));assert.equal(f.frames.size,1);
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pagehide',{persisted:false}));
    assert.equal(f.frames.size,0);assert.equal(f.disposed,1);
  }finally{f.close();}
});
