import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gzipSync} from 'node:zlib';
import * as THREE from 'three';
import {JSDOM} from 'jsdom';
import {createVeloScene} from '../src/velo-lite/model.js';
import {renderSize,MAX_PIXELS,MOBILE_PIXELS} from '../src/velo-renderer.js';
import {startVeloViewer} from '../src/velo-viewer.js';
const html=fs.readFileSync(new URL('../scenes/velo-coast.html',import.meta.url),'utf8');

test('selected source is attributed, local, textured-model-free and sandbox compatible',()=>{
  assert.match(html,/Jacory Ou/);assert.match(html,/velo-coast\.webp/);
  assert.match(html,/touch-action:pan-y/);assert.doesNotMatch(html,/https?:|type="module"|\.glb/);
  const boot=fs.readFileSync(new URL('../src/velo-viewer.js',import.meta.url),'utf8');
  assert.match(boot,/script\.src='\/scripts\/velo-renderer\.js'/);
  assert.doesNotMatch(boot,/^import /m);
  assert.ok(fs.readFileSync(new URL('../vendor/pelican-velo/LICENSE',import.meta.url),'utf8').includes('MIT License'));
});
test('scene is below 30000 triangles and 110 drawables; transforms stay finite at every sampled crank angle',()=>{
  const model=createVeloScene();
  try{
    let triangles=0,calls=0;
    model.scene.traverse(o=>{
      if(o.isMesh){triangles+=(o.geometry.index?.count??o.geometry.attributes.position.count)/3;calls++;}
      if(o.isLineSegments)calls++;
    });
    assert.ok(triangles<30000,triangles);assert.ok(calls<110,calls);
    for(let i=0;i<160;i++){
      model.update(.05,i*.05);model.scene.updateMatrixWorld(true);
      assert.equal(model.deck.position.x,0);
      assert.ok(model.planks.position.x<=0&&model.planks.position.x>-.6);
      model.scene.traverse(o=>assert.ok(o.matrixWorld.elements.every(Number.isFinite),o.name));
      const pedals=model.bike.getPedals();
      assert.ok(Math.abs(pedals.left.x+pedals.right.x+.1)<1e-9);
      assert.ok(Math.abs(pedals.left.y+pedals.right.y-1.44)<1e-9);
      for(const [leg,pedal] of [[model.bird.legs[0],pedals.left],[model.bird.legs[1],pedals.right]]){
        const foot=leg.foot.getWorldPosition(new THREE.Vector3());
        assert.ok(Math.abs(foot.x-pedal.x-.035)<1e-9);
        assert.ok(Math.abs(foot.y-pedal.y-.06)<1e-9);
        assert.ok(Math.abs(Math.abs(foot.z)-.27)<1e-9);
        assert.ok(Math.abs(leg.thigh.scale.y-.69)<1e-9);
        assert.ok(Math.abs(leg.shin.scale.y-.67)<1e-9);
      }
    }
  }finally{model.dispose();}
});
test('one 512px shadow light, no physical material, postprocessing, external textures or secondary town',()=>{
  const model=createVeloScene();try{
    let casters=0;
    model.scene.traverse(o=>{
      if(o.isLight&&o.castShadow)casters++;
      if(o.material)for(const mat of [o.material].flat()){
        assert.ok(!mat.isMeshPhysicalMaterial);
        for(const value of Object.values(mat))assert.ok(!value?.isTexture);
      }
    });
    assert.equal(casters,1);assert.equal(model.sun.shadow.mapSize.x,512);
    assert.equal(model.bird.root.name,'partridge');
  }finally{model.dispose();}
});
test('basket and flowers clear the full tire envelope and stay ahead of the handlebar at every crank angle',()=>{
  const model=createVeloScene();try{
    for(let i=0;i<48;i++){
      model.update(.05,i*.05);model.scene.updateMatrixWorld(true);
      const basket=new THREE.Box3().setFromObject(model.bike.basket);
      const tireTop=model.bike.frontWheel.getWorldPosition(new THREE.Vector3()).y+.66+.065;
      assert.ok(basket.min.y>tireTop+.04,`basket floor ${basket.min.y} tire top ${tireTop}`);
      assert.ok(basket.min.x>.88,'basket must not cover the handlebar');
      const flowers=model.bike.basket.children.filter(o=>o.name==='basket-flower');
      assert.equal(flowers.length,5);
      for(const flower of flowers){
        const box=new THREE.Box3().setFromObject(flower);
        assert.ok(box.min.y>tireTop+.1,'flower stems must not penetrate the wheel');
        assert.ok(box.min.x>.96,'flowers must not cross into the steering column');
      }
    }
  }finally{model.dispose();}
});
test('desktop and mobile pixels never scale above CSS resolution or fixed budget',()=>{
  for(const mobile of [false,true])for(const [w,h] of [[320,360],[900,490],[4096,4096],[1,99999],[0,0],[NaN,Infinity]]){
    const [x,y]=renderSize(w,h,mobile);
    assert.ok(x>=1&&y>=1&&x*y<=(mobile?MOBILE_PIXELS:MAX_PIXELS));
  }
  assert.deepEqual(renderSize(320,360),[320,360]);
});
test('full compressed renderer including its own Three runtime stays below 200KB, boot below 3KB',()=>{
  const boot=fs.readFileSync(new URL('../public/scripts/velo-viewer.js',import.meta.url));
  const renderer=fs.readFileSync(new URL('../public/scripts/velo-renderer.js',import.meta.url));
  const bootBytes=gzipSync(boot,{level:9}).length;
  assert.ok(bootBytes<3000);assert.ok(bootBytes+gzipSync(renderer,{level:9}).length<200000);
});
function fixture({reduced=false,pending=false,failure=false,mobile=false}={}){
  const dom=new JSDOM(html,{pretendToBeVisual:true,runScripts:'outside-only'}),w=dom.window;
  const frames=new Map(),draws=[],observers=[],messages=[];
  let created=0,disposed=0,serial=0,hidden=false,finish;
  const parent={postMessage:m=>messages.push(m)},media=new w.EventTarget();media.matches=reduced;
  w.matchMedia=()=>media;Object.defineProperty(w,'parent',{value:parent});
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  const surface=w.document.getElementById('veloSurface');
  Object.defineProperty(surface,'clientWidth',{value:mobile?390:900});
  Object.defineProperty(surface,'clientHeight',{value:490});
  w.requestAnimationFrame=fn=>{frames.set(++serial,fn);return serial;};w.cancelAnimationFrame=id=>frames.delete(id);
  w.IntersectionObserver=class{constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
  const viewer=startVeloViewer({win:w,doc:w.document,rendererFactory:()=>{
    created++;
    if(failure)throw Error('Fixture: unavailable WebGL');
    const value={render:data=>draws.push(data),dispose:()=>disposed++};
    return pending?new Promise(resolve=>finish=()=>resolve(()=>value)):value;
  }});
  return {w,viewer,frames,draws,observers,messages,media,parent,
    get created(){return created;},get disposed(){return disposed;},
    message(data,source=parent){w.dispatchEvent(new w.MessageEvent('message',{data,source}));},
    enter(value=true){observers[0].fn([{isIntersecting:value}]);},
    async flush(){await Promise.resolve();await Promise.resolve();},
    tick(now){const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn(now));},
    hide(value){hidden=value;w.document.dispatchEvent(new w.Event('visibilitychange'));},
    finish:()=>finish?.(),close(){viewer.dispose();w.close();}};
}
const play={type:'pelican-playback',paused:false};
test('no scene download/GPU allocation before both correct parent permission and intersection',async()=>{
  const f=fixture();try{
    f.message(play,{});f.enter();await f.flush();assert.equal(f.created,0);
    f.message({...play,paused:'false'});await f.flush();assert.equal(f.created,0);
    f.enter(false);f.message(play);await f.flush();assert.equal(f.created,0);
    f.enter();await f.flush();assert.equal(f.created,1);assert.equal(f.frames.size,1);
    f.tick(0);assert.equal(f.w.document.documentElement.dataset.scene,'ready');
  }finally{f.close();}
});
test('30 desktop /24 mobile frame ceiling; offscreen, parent pause and background stop rendering',async()=>{
  for(const mobile of [false,true]){
    const f=fixture({mobile});try{
      f.enter();f.message(play);await f.flush();
      for(let t=0;t<1000;t+=5)f.tick(t);
      assert.ok(f.draws.length<=(mobile?24:30));assert.ok(f.draws.length>15);
      f.enter(false);assert.equal(f.frames.size,0);
      f.enter();f.hide(true);assert.equal(f.frames.size,0);
      f.hide(false);f.message({...play,paused:true});assert.equal(f.frames.size,0);
      f.message(play);f.tick(90000);assert.equal(f.draws.at(-1).delta,0);
    }finally{f.close();}
  }
});
test('reduced motion remains static until explicit override, then camera controls remain usable paused',async()=>{
  const f=fixture({reduced:true});try{
    f.enter();f.message(play);await f.flush();assert.equal(f.created,0);
    f.message({...play,reducedMotionOverride:true});await f.flush();f.tick(0);
    f.message({...play,paused:true});
    f.w.document.querySelector('[data-velo-view="side"]').click();
    assert.equal(f.draws.at(-1).view,'side');assert.equal(f.frames.size,0);
    const canvas=f.w.document.querySelector('canvas');
    canvas.dispatchEvent(new f.w.KeyboardEvent('keydown',{key:'ArrowRight',cancelable:true}));
    assert.ok(f.draws.at(-1).yaw>.03);
  }finally{f.close();}
});
test('failure and context loss preserve static poster and disable broken camera buttons',async()=>{
  for(const failure of [false,true]){
    const f=fixture({failure});try{
      f.enter();f.message(play);await f.flush();
      if(!failure)f.w.document.querySelector('canvas').dispatchEvent(new f.w.Event('webglcontextlost',{cancelable:true}));
      assert.equal(f.w.document.documentElement.dataset.scene,'fallback');assert.equal(f.frames.size,0);
      assert.ok([...f.w.document.querySelectorAll('button')].every(b=>b.disabled));
    }finally{f.close();}
  }
});
test('pending async load disposes after teardown, and bfcache pause does not recreate renderer',async()=>{
  const delayed=fixture({pending:true});try{
    delayed.enter();delayed.message(play);delayed.viewer.dispose();delayed.finish();await delayed.flush();
    assert.equal(delayed.disposed,0);assert.equal(delayed.frames.size,0);
  }finally{delayed.close();}
  const f=fixture();try{
    f.enter();f.message(play);await f.flush();
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pagehide',{persisted:true}));assert.equal(f.frames.size,0);
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pageshow',{persisted:true}));assert.equal(f.created,1);
    f.w.dispatchEvent(new f.w.PageTransitionEvent('pagehide',{persisted:false}));assert.equal(f.disposed,1);
  }finally{f.close();}
});
test('an in-flight script download cannot allocate a renderer after scrolling away',async()=>{
  const f=fixture({pending:true});try{
    f.enter();f.message(play);f.enter(false);f.finish();await f.flush();
    assert.equal(f.frames.size,0);assert.equal(f.draws.length,0);
    assert.equal(f.w.document.documentElement.dataset.scene,undefined);
    f.enter();await f.flush();f.tick(0);
    assert.equal(f.created,1);assert.equal(f.draws.length,1);
  }finally{f.close();}
});
