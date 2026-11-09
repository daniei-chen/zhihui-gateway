// Small boot module; the Three.js scene is not even fetched until playback is allowed.
export const VIEWS={coast:{yaw:.70,pitch:.16},side:{yaw:.03,pitch:.15},front:{yaw:1.20,pitch:.16}};
function loadRenderer(canvas) {
  return new Promise((resolve,reject)=>{
    const script=document.createElement('script');
    script.src='/scripts/velo-renderer.js';script.async=true;
    script.onload=()=>{
      script.remove();
      try {resolve(globalThis.zhVeloRenderer.createVeloRenderer);}catch(error){reject(error);}
    };
    script.onerror=()=>{script.remove();reject(Error('Local scene could not be loaded'));};
    document.head.append(script);
  });
}
export function startVeloViewer({win=window,doc=document,
  rendererFactory=loadRenderer}={}) {
  const surface=doc.getElementById('veloSurface'),canvas=doc.getElementById('veloCanvas');
  const status=doc.getElementById('veloStatus'),buttons=[...doc.querySelectorAll('[data-velo-view]')];
  const media=win.matchMedia('(prefers-reduced-motion: reduce)');
  const standalone=win.parent===win,listeners=[],observers=[];
  let renderer,createRenderer,loading=false,disposed=false,failed=false,paused=!standalone,override=false;
  let visible=!win.IntersectionObserver,pageActive=true,frame=0,last=null,time=0,drag=null;
  let view='coast',yaw=VIEWS.coast.yaw,pitch=VIEWS.coast.pitch;
  const mobile=()=>surface.clientWidth<520;
  const on=(obj,type,fn,opts)=>{obj.addEventListener(type,fn,opts);listeners.push(()=>obj.removeEventListener(type,fn,opts));};
  const allowed=()=>!disposed&&!failed&&!paused&&visible&&pageActive&&!doc.hidden&&(!media.matches||override);
  function stop(){if(frame)win.cancelAnimationFrame(frame);frame=0;last=null;}
  function fail(){
    if(disposed||failed)return;
    failed=true;stop();renderer?.dispose();renderer=null;
    doc.documentElement.dataset.scene='fallback';status.textContent='静态海岸 · 当前设备未启用三维渲染';
    buttons.forEach(button=>button.disabled=true);
  }
  function render(delta=0,frameMs=0){
    if(!renderer||disposed||failed||!visible||!pageActive||doc.hidden)return;
    try{
      renderer.render({width:surface.clientWidth,height:surface.clientHeight,time,delta,frameMs,
        yaw,pitch,view,mobile:mobile()});
      doc.documentElement.dataset.scene='ready';
    }catch{fail();}
  }
  function tick(now){
    frame=0;if(!allowed()||!renderer)return;
    const gap=last===null?0:now-last;
    if(last===null||gap>=1000/(mobile()?24:30)){
      const delta=last===null?0:Math.min(gap/1000,.08);last=now;time+=delta;render(delta,gap);
    }
    if(allowed())frame=win.requestAnimationFrame(tick);
  }
  async function initialize(){
    if(loading||renderer||disposed||failed)return;
    loading=true;
    try{
      createRenderer ||= await rendererFactory(canvas);
      if(disposed||failed||!allowed())return;
      renderer=typeof createRenderer==='function'?createRenderer(canvas):createRenderer;
      buttons.forEach(button=>button.disabled=false);
      status.textContent='拖动转一转 · 上下滑动浏览页面';sync();
    }catch{fail();}finally{loading=false;}
  }
  function sync(){
    if(!allowed()){stop();return;}
    if(!renderer){void initialize();return;}
    if(!frame)frame=win.requestAnimationFrame(tick);
  }
  function setView(value){
    if(!VIEWS[value]||!renderer||failed)return;
    view=value;({yaw,pitch}=VIEWS[value]);
    buttons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.veloView===view)));
    render();
  }
  on(win,'message',event=>{
    if(event.source!==win.parent||event.data?.type!=='pelican-playback'||typeof event.data.paused!=='boolean')return;
    paused=event.data.paused;override=event.data.reducedMotionOverride===true;sync();
  });
  on(doc,'visibilitychange',sync);on(media,'change',sync);
  on(win,'pagehide',event=>{pageActive=false;stop();if(!event.persisted)dispose();});
  on(win,'pageshow',()=>{pageActive=true;sync();});
  on(canvas,'webglcontextlost',event=>{event.preventDefault();fail();});
  const release=()=>{
    if(drag&&canvas.hasPointerCapture?.(drag.id))canvas.releasePointerCapture(drag.id);
    drag=null;
  };
  on(canvas,'pointerdown',event=>{
    if(event.button!==0||!renderer||failed)return;
    drag={id:event.pointerId,x:event.clientX,y:event.clientY,yaw,pitch};
  });
  on(canvas,'pointermove',event=>{
    if(!drag||event.pointerId!==drag.id)return;
    const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
    if(event.pointerType==='touch'&&Math.abs(dy)>Math.abs(dx)*1.2){release();return;}
    if(Math.abs(dx)>6)canvas.setPointerCapture?.(event.pointerId);
    yaw=drag.yaw+dx*.007;pitch=Math.max(.06,Math.min(.65,drag.pitch+dy*.003));render();
  },{passive:true});
  for(const type of ['pointerup','pointercancel','lostpointercapture'])on(canvas,type,release);
  on(win,'pointerup',release);on(win,'blur',release);
  on(canvas,'keydown',event=>{
    if(!renderer||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home'].includes(event.key))return;
    event.preventDefault();
    if(event.key==='Home')return setView('coast');
    if(event.key==='ArrowLeft')yaw-=.14;if(event.key==='ArrowRight')yaw+=.14;
    if(event.key==='ArrowUp')pitch=Math.min(.65,pitch+.08);
    if(event.key==='ArrowDown')pitch=Math.max(.06,pitch-.08);render();
  });
  buttons.forEach(button=>on(button,'click',()=>setView(button.dataset.veloView)));
  if(win.IntersectionObserver){
    const observer=new win.IntersectionObserver(entries=>{visible=entries[0]?.isIntersecting===true;sync();});
    observer.observe(surface);observers.push(observer);
  }
  if(win.ResizeObserver){const observer=new win.ResizeObserver(()=>render());observer.observe(surface);observers.push(observer);}
  function dispose(){
    if(disposed)return;disposed=true;stop();release();renderer?.dispose();renderer=null;
    observers.forEach(observer=>observer.disconnect());listeners.forEach(remove=>remove());
  }
  win.parent.postMessage({type:'pelican-ready'},'*');sync();
  return {dispose,setView};
}
if(typeof window!=='undefined'&&document.getElementById('veloSurface'))startVeloViewer();
