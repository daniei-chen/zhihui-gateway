import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {attachConnectedPaths,attachNavigation,connect,point} from '../scenes/connected-paths.js';

const html=readFileSync(new URL('../public/index.html',import.meta.url),'utf8');

test('review navigation cancels the source centering transform instead of translating an already centered bar',()=>{
  const dom=new JSDOM(html),doc=dom.window.document;
  try{
    for(const link of [...doc.querySelectorAll('link[rel="stylesheet"]')]){
      const style=doc.createElement('style');
      style.textContent=readFileSync(new URL('../public'+link.getAttribute('href'),import.meta.url),'utf8');
      link.replaceWith(style);
    }
    const override=doc.createElement('style');
    override.textContent=readFileSync(new URL('../scenes/color-review.css',import.meta.url),'utf8');doc.head.append(override);
    const result=dom.window.getComputedStyle(doc.querySelector('.navigation-wrap'));
    assert.equal(result.transform,'none');assert.equal(result.left,'0px');assert.equal(result.right,'0px');
    assert.equal(result.marginInline,'auto');
    // Layout arithmetic for the declared desktop/mobile widths, not browser pixel QA.
    for(const viewport of [320,390,768,1440,1920]){
      const width=viewport<=760?viewport-28:Math.min(1170,viewport-48);
      const left=(viewport-width)/2;
      assert.ok(left>=14);assert.equal(left+width/2,viewport/2);
      assert.ok(override.textContent.includes(viewport<=760?'width:calc(100% - 28px)':'width:min(1170px,calc(100% - 48px))'));
    }
  }finally{dom.window.close();}
});

function fixture(width=900) {
  const dom=new JSDOM(html,{pretendToBeVisual:true}),w=dom.window,frames=new Map(),intersections=[];
  let id=0,hidden=false,resize;
  w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  w.requestAnimationFrame=fn=>{frames.set(++id,fn);return id;};w.cancelAnimationFrame=id=>frames.delete(id);
  w.ResizeObserver=class{constructor(fn){resize=fn;}observe(){}disconnect(){}};
  w.IntersectionObserver=class{constructor(fn){intersections.push(fn);}observe(){}disconnect(){}};
  w.SVGElement.prototype.getTotalLength=()=>500;
  w.SVGElement.prototype.getPointAtLength=p=>({x:p,y:100+p*.1});
  let scale=1;
  function box(x,y,width,height){return{left:x*scale,top:y*scale,right:(x+width)*scale,bottom:(y+height)*scale,width:width*scale,height:height*scale};}
  for(const selector of ['.signal-diagram','.network']){
    const host=w.document.querySelector(selector);host.getBoundingClientRect=()=>box(20,40,width,360);
    host.querySelector(selector==='.signal-diagram'?'.signal-wires':'svg').getBoundingClientRect=()=>box(20,40,width,360);
    const signal=selector==='.signal-diagram';
    const source=host.querySelector(signal?'.signal-client svg':'.network-label.input');source.getBoundingClientRect=()=>box(45,180,60,35);
    host.querySelector(signal?'.signal-core-inner':'.network-core').getBoundingClientRect=()=>box(width*.45,180,65,65);
    const targets=host.querySelectorAll(signal?'[data-hero-platform]':'[data-platform-target]');
    targets.forEach((target,i)=>{target.getBoundingClientRect=()=>box(width*.76,75+i*120,120,48);});
  }
  return{w,dom,frames,intersections,resize:()=>resize(),scale(value){scale=value;},
    tick(now){const values=[...frames.values()];frames.clear();values.forEach(fn=>fn(now));},
    hidden(value){hidden=value;w.document.dispatchEvent(new w.Event('visibilitychange'));}};
}

test('all route endpoints stay attached to the actual boxes at desktop and phone sizes',()=>{
  for(const width of [320,390,768,1100]){
    const f=fixture(width),attached=attachConnectedPaths(f.w.document,f.w);
    try{
      for(const [selector,signal]of [['.signal-diagram',true],['.network',false]]){
        const host=f.w.document.querySelector(selector),bounds=host.getBoundingClientRect();
        const paths=host.querySelectorAll(signal?'[data-signal-route]':'.lines');
        const targets=host.querySelectorAll(signal?'[data-hero-platform]':'[data-platform-target]');
        paths.forEach((path,i)=>{const end=point(targets[i].getBoundingClientRect(),bounds,'left');assert.ok(path.getAttribute('d').endsWith(`${end.x} ${end.y}`),width+':'+path.id);});
      }
      const path=f.w.document.querySelector('[data-signal-route=workbuddy]'),old=path.getAttribute('d');
      f.scale(.7);attached.layout();assert.notEqual(path.getAttribute('d'),old);
      f.w.document.querySelector('.signal-diagram').dataset.selectedPlatform='qoder';
      f.w.document.dispatchEvent(new f.w.Event('zhihui:platform-selected'));
      assert.equal(f.w.document.querySelector('[data-signal-route=qoder]').style.stroke,'rgb(21, 157, 141)');
      assert.ok(connect({x:1,y:2},{x:8,y:9}).startsWith('M1 2 C'));
    }finally{attached.dispose();f.w.close();}
  }
});

test('packets move along the selected route and leave no animation after pause, offscreen or disposal',()=>{
  const f=fixture(),attached=attachConnectedPaths(f.w.document,f.w);
  try{
    const dot=f.w.document.querySelector('.connected-packet');f.tick(100);const before=dot.getAttribute('cx');f.tick(200);assert.notEqual(dot.getAttribute('cx'),before);
    f.w.document.documentElement.setAttribute('data-home-flow-paused','');f.w.document.dispatchEvent(new f.w.Event('zhihui:motion-change'));assert.equal(f.frames.size,0);assert.equal(dot.style.display,'none');
    f.w.document.documentElement.removeAttribute('data-home-flow-paused');f.w.document.dispatchEvent(new f.w.Event('zhihui:motion-change'));assert.equal(f.frames.size,1);
    f.hidden(true);assert.equal(f.frames.size,0);f.hidden(false);assert.equal(f.frames.size,1);
    f.intersections[0]([...f.w.document.querySelectorAll('.signal-diagram,.network')].map(target=>({target,isIntersecting:false})));assert.equal(f.frames.size,0);
    attached.dispose();assert.equal(f.frames.size,0);assert.equal(f.w.document.querySelector('.connected-port'),null);
  }finally{attached.dispose();f.w.close();}
});

test('navigation marker follows hovered and current sections without a perpetual loop',()=>{
  const f=fixture(),doc=f.w.document;
  doc.querySelector('.nav-center').getBoundingClientRect=()=>({left:200,top:10,width:400,height:34});
  const links=[...doc.querySelectorAll('.nav-center a')];
  links.forEach((link,i)=>{link.getBoundingClientRect=()=>({left:200+i*85,top:10,width:75,height:34});doc.querySelector(link.getAttribute('href')).getBoundingClientRect=()=>({top:i===0?100:500});});
  const nav=attachNavigation(doc,f.w);
  try{
    assert.equal(links[0].getAttribute('aria-current'),'location');
    doc.querySelector(links[1].getAttribute('href')).getBoundingClientRect=()=>({top:160});
    f.w.dispatchEvent(new f.w.Event('scroll'));f.tick(100);
    assert.equal(links[1].getAttribute('aria-current'),'location');
    doc.querySelector(links[1].getAttribute('href')).getBoundingClientRect=()=>({top:500});
    f.w.dispatchEvent(new f.w.Event('scroll'));f.tick(200);
    links[2].dispatchEvent(new f.w.Event('pointerenter'));assert.equal(doc.querySelector('.nav-current-marker').style.transform,'translate(170px,0px)');
    links[2].dispatchEvent(new f.w.Event('pointerleave'));assert.equal(doc.querySelector('.nav-current-marker').style.transform,'translate(0px,0px)');
    assert.equal(f.frames.size,0);nav.dispose();assert.equal(doc.querySelector('.nav-current-marker'),null);
  }finally{nav.dispose();f.w.close();}
});
