import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {createGatewayModel} from '../src/gateway-model.js';

const source=readFileSync(new URL('../public/login/index.html',import.meta.url),'utf8');
const script=readFileSync(new URL('../public/scripts/login.js',import.meta.url),'utf8');
test('login consistently names four gateways without a three-platform claim',()=>{
  const dom=new JSDOM(source);
  try {
    assert.match(dom.window.document.querySelector('.gateway-description').textContent,/四网关/);
    assert.deepEqual([...dom.window.document.querySelectorAll('.tool-names span')].map(node=>node.textContent),['Codex','WorkBuddy','Qoder','Trae']);
    assert.doesNotMatch(source,/三平台|三网关/);
  } finally { dom.window.close(); }
});
function fixture({connected=false,url='http://127.0.0.1:4173/login',reply}={}) {
  const dom=new JSDOM(source,{runScripts:'outside-only',pretendToBeVisual:true,url});
  const w=dom.window,calls=[],states=[];
  if(connected)w.document.documentElement.dataset.authMode='same-origin';
  w.fetch=async(path,options)=>{
    calls.push({path,options});
    return path==='/api/me'?{ok:false}:reply?reply():{ok:false,status:401,json:async()=>({})};
  };
  w.document.addEventListener('zhihui:login-state',event=>states.push(event.detail));
  w.eval(script);
  const $=selector=>w.document.querySelector(selector);
  return {w,$,calls,states,close:()=>w.close(),send:()=>$('#loginForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})),settle:()=>new Promise(resolve=>setImmediate(resolve))};
}

test('local login preview never probes a session or sends credentials, even if marked connected',async()=>{
  for(const connected of [false,true]){
    const f=fixture({connected});
    try{
      assert.equal(f.$('#submitBtn').disabled,false);
      f.$('#username').value='preview-user';f.$('#password').value='synthetic-test-only';f.send();await f.settle();
      assert.equal(f.calls.length,0);
      assert.match(f.$('#formStatus').textContent,/未提交账号或密码/);
      assert.equal(f.$('#loginPreviewNote').hidden,false);
    }finally{f.close();}
  }
});

test('empty field focus, DOM autofill, password visibility and caps lock work',async()=>{
  const f=fixture();
  try{
    f.send();assert.equal(f.w.document.activeElement.id,'username');assert.equal(f.$('#username').getAttribute('aria-invalid'),'true');
    f.$('#username').value='autofilled';f.send();assert.equal(f.w.document.activeElement.id,'password');
    f.$('#password').value='synthetic-test-only';f.$('#togglePassword').click();
    assert.equal(f.$('#password').type,'text');assert.equal(f.$('#togglePassword').getAttribute('aria-pressed'),'true');
    f.$('#password').dispatchEvent(new f.w.KeyboardEvent('keyup',{key:'A',modifierCapsLock:true}));
    assert.equal(f.$('#capsWarning').hidden,false);
    f.$('#password').dispatchEvent(new f.w.Event('blur'));assert.equal(f.$('#capsWarning').hidden,true);
    f.w.dispatchEvent(new f.w.Event('pagehide'));assert.equal(f.$('#password').type,'password');
    f.send();assert.match(f.$('#formStatus').textContent,/未提交/);
    assert.equal(f.$('#username').getAttribute('autocomplete'),'username');
    assert.equal(f.$('#password').getAttribute('autocomplete'),'current-password');
  }finally{f.close();}
});

test('same-origin auth keeps API contract, accepts autofill and prevents duplicate submission',async()=>{
  let resolve;
  const f=fixture({connected:true,url:'https://gateway.example/login?expired=1',reply:()=>new Promise(r=>{resolve=r;})});
  try{
    assert.equal(f.calls[0].path,'/api/me');assert.match(f.$('#formStatus').textContent,/已过期/);
    assert.equal(f.$('#loginPreviewNote').hidden,true);
    f.$('#username').value='  autofill-user  ';f.$('#password').value='  synthetic secret  ';
    f.send();f.send();assert.equal(f.calls.length,2);assert.equal(f.$('#submitBtn').disabled,true);
    assert.equal(f.$('#loginForm').getAttribute('aria-busy'),'true');
    assert.equal(f.calls[1].path,'/api/login');assert.equal(f.calls[1].options.credentials,'same-origin');
    assert.deepEqual(JSON.parse(f.calls[1].options.body),{username:'autofill-user',password:'  synthetic secret  '});
    resolve({ok:false,status:401,json:async()=>({detail:'测试登录失败'})});await f.settle();
    assert.equal(f.$('#password').value,'');assert.equal(f.$('#username').value,'  autofill-user  ');
    assert.equal(f.$('#submitBtn').disabled,false);assert.equal(f.w.document.activeElement.id,'password');
    assert.match(f.$('#formStatus').textContent,/测试登录失败/);assert.ok(f.states.includes('submitting'));assert.ok(f.states.includes('error'));
  }finally{f.close();}
});

test('rate limit, server and network errors are recoverable without saving credentials',async()=>{
  for(const failure of [429,500,'network']){
    const f=fixture({connected:true,url:'https://gateway.example/login',reply:async()=>{
      if(failure==='network')throw Error('offline');return{ok:false,status:failure,json:async()=>({})};
    }});
    try{
      f.$('#username').value='sample';f.$('#password').value='synthetic';f.send();await f.settle();
      assert.equal(f.$('#submitBtn').disabled,false);assert.equal(f.$('#formStatus').dataset.kind,'error');
      assert.match(f.$('#formStatus').textContent,failure===429?/过于频繁/:failure===500?/服务暂时不可用/:/无法连接服务器/);
      assert.equal(f.w.localStorage.length,0);assert.equal(f.w.sessionStorage.length,0);
    }finally{f.close();}
  }
});

test('gateway model uses finite connected geometry and four paths with a small payload',()=>{
  const model=createGatewayModel();let triangles=0;
  assert.equal(model.lanes.length,4);assert.equal(model.particles.length,4);
  model.group.traverse(child=>{
    if(!child.isMesh)return;
    const geometry=child.geometry;
    assert.ok([...geometry.attributes.position.array].every(Number.isFinite));
    assert.ok([...geometry.attributes.normal.array].every(Number.isFinite));
    triangles+=(geometry.index?.count??geometry.attributes.position.count)/3;
  });
  assert.ok(triangles<60000,`Unexpected decorative geometry cost ${triangles}`);
  const renderer=readFileSync(new URL('../public/scripts/gateway-viewer.js',import.meta.url));
  assert.ok(renderer.length<1048576,'Gateway renderer exceeds 1 MiB budget');
});

test('gateway animation waits for idle and pauses for reduced motion, background and offscreen',async()=>{
  const dom=new JSDOM(source,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://127.0.0.1:4173/login'});
  const w=dom.window,calls=[],states=[];let start,onMedia,onVisible,hidden=false;
  const media={matches:true,addEventListener:(_,cb)=>{onMedia=cb;}};
  w.matchMedia=()=>media;
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  w.requestIdleCallback=callback=>{start=callback;};
  w.IntersectionObserver=class{constructor(cb){onVisible=cb;}observe(){}};
  w.__loadGateway=async()=>({createGatewayViewer:()=>({setPlayback:(...args)=>calls.push(args),setState:value=>states.push(value),dispose(){}})});
  const controller=readFileSync(new URL('../public/scripts/gateway.js',import.meta.url),'utf8');
  try{
    w.eval(controller.replace("import('/scripts/gateway-viewer.js')",'window.__loadGateway()'));
    assert.equal(calls.length,0);
    await start();assert.equal(w.document.getElementById('gatewayStage').dataset.state,'ready');
    assert.deepEqual(calls.at(-1),[false,true]);
    w.document.getElementById('gatewayMotion').click();assert.deepEqual(calls.at(-1),[true,true]);
    onVisible([{isIntersecting:false}]);assert.deepEqual(calls.at(-1),[true,false]);
    onVisible([{isIntersecting:true}]);hidden=true;w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.deepEqual(calls.at(-1),[false,true]);
    hidden=false;w.document.dispatchEvent(new w.Event('visibilitychange'));
    w.document.dispatchEvent(new w.CustomEvent('zhihui:login-state',{detail:'focused'}));assert.equal(states.at(-1),'focused');
    onMedia();assert.deepEqual(calls.at(-1),[false,true]);
    w.document.getElementById('gatewayCanvas').dispatchEvent(new w.Event('gateway:error'));
    assert.equal(w.document.getElementById('gatewayStage').dataset.state,'poster');
    assert.equal(w.document.getElementById('gatewayMotion').disabled,true);
  }finally{w.close();}
});
