// Anchors come from the actual rendered node boxes, never a second CSS grid.
export function point(box, bounds, side) {
  return {x: (side === 'left' ? box.left : side === 'right' ? box.right : (box.left+box.right)/2)-bounds.left,
    y: (box.top+box.bottom)/2-bounds.top};
}
export function connect(a,b) {
  const distance=Math.max(16,Math.abs(b.x-a.x)*.48);
  return `M${a.x} ${a.y} C${a.x+distance} ${a.y} ${b.x-distance} ${b.y} ${b.x} ${b.y}`;
}
export function through(a,core,b) {
  return connect(a,core)+' '+connect(core,b).replace(/^M[^C]+C/,'C');
}

export function attachConnectedPaths(doc=document,win=window) {
  const ns='http://www.w3.org/2000/svg', root=doc.documentElement;
  const media=win.matchMedia('(prefers-reduced-motion: reduce)');
  const definitions=[
    {host:'.signal-diagram',svg:'.signal-wires',source:'.signal-client svg',core:'.signal-core-inner',
      targets:['[data-hero-platform="workbuddy"]','[data-hero-platform="qoder"]','[data-hero-platform="trae"]'],
      paths:['[data-signal-route="workbuddy"]','[data-signal-route="qoder"]','[data-signal-route="trae"]'],signal:true},
    {host:'.network',svg:'svg',source:'.network-label.input',core:'.network-core',
      targets:['[data-platform-target="workbuddy"]','[data-platform-target="qoder"]','[data-platform-target="trae"]'],
      paths:['#netA','#netC','#netB'],signal:false},
  ];
  let raf=0,layoutFrame=0,last=0,time=0,active=true,disposed=false;
  const make=(tag,attrs)=>{const node=doc.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))node.setAttribute(k,v);return node;};
  const stages=definitions.map(config=>{
    const host=doc.querySelector(config.host);if(!host)return null;
    const svg=host.querySelector(config.svg),paths=config.paths.map(selector=>host.querySelector(selector));
    if(!svg||paths.some(p=>!p))return null;
    // Retire the old stub/dash effect, not the user-triggered request tracer.
    if(config.signal)svg.querySelectorAll('.signal-travel, circle').forEach(node=>node.remove());
    const layer=make('g',{'aria-hidden':'true'});svg.append(layer);
    const ports=Array.from({length:5},()=>{const node=make('circle',{r:3,class:'connected-port'});layer.append(node);return node;});
    const packets=[0,1].map(()=>{
      const halo=make('circle',{r:8,fill:'#3683fc',opacity:'.12',class:'connected-packet'});
      const dot=make('circle',{r:3,fill:'#3683fc',class:'connected-packet'});layer.append(halo,dot);return{halo,dot};
    });
    const receipt=config.signal?null:make('path',{'aria-hidden':'true',fill:'none',stroke:'#91bdb0','stroke-width':1.2,'stroke-dasharray':'3 5'});
    if(receipt){receipt.style.fill='none';receipt.style.stroke='#91bdb0';svg.prepend(receipt);}
    return{config,host,svg,paths,ports,packets,receipt,layer,visible:true,flow:null};
  }).filter(Boolean);
  function selected(){return ['workbuddy','qoder','trae'].indexOf(doc.querySelector('.signal-diagram')?.dataset.selectedPlatform||'workbuddy');}
  function layout(){
    layoutFrame=0;
    for(const stage of stages){
      const {host,svg,config,ports,paths}=stage,bounds=svg.getBoundingClientRect();
      if(!bounds.width||!bounds.height)continue;
      svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);
      const source=host.querySelector(config.source),core=host.querySelector(config.core);
      if(!source||!core)continue;
      const a=point(source.getBoundingClientRect(),bounds,'right');
      const c=point(core.getBoundingClientRect(),bounds,'center');
      const start=point(core.getBoundingClientRect(),bounds,'right');
      const end=point(core.getBoundingClientRect(),bounds,'left');
      if(config.signal)host.querySelector('.signal-input')?.setAttribute('d',connect(a,end));
      const coords=[a,end];
      paths.forEach((path,index)=>{
        const target=host.querySelector(config.targets[index]);if(!target)return;
        const b=point(target.getBoundingClientRect(),bounds,'left');coords.push(b);
        path.setAttribute('d',config.signal?connect(start,b):through(a,c,b));
        path.style.stroke=index===Math.max(0,selected())?['#3683fc','#159d8d','#cf8b32'][index]:'';
      });
      coords.forEach((p,i)=>{ports[i].setAttribute('cx',p.x);ports[i].setAttribute('cy',p.y);});
      // A separate invisible full route gives a single unbroken packet journey.
      const i=Math.max(0,selected()),target=host.querySelector(config.targets[i]);
      if(!target)continue;
      if(!stage.flow){stage.flow=make('path',{'aria-hidden':'true'});stage.flow.style.fill='none';stage.flow.style.stroke='none';svg.append(stage.flow);}
      stage.flow.setAttribute('d',through(a,c,point(target.getBoundingClientRect(),bounds,'left')));
      if(!config.signal){
        const trail=host.querySelector('#zh23Trail');
        if(trail&&doc.querySelector('.zh23-route-result')?.dataset.state!=='running')trail.setAttribute('d',paths[i].getAttribute('d'));
        const result=host.querySelector('.network-label.result');
        if(result){const b=point(result.getBoundingClientRect(),bounds,'right');
          stage.receipt.setAttribute('d',`M${c.x} ${c.y+30} C${c.x} ${b.y} ${b.x+48} ${b.y} ${b.x} ${b.y}`);}
      }
    }
  }
  function queueLayout(){if(!layoutFrame&&!disposed)layoutFrame=win.requestAnimationFrame(()=>{layout();sync();});}
  function allowed(){return active&&!doc.hidden&&!root.hasAttribute('data-home-flow-paused')&&!root.hasAttribute('data-reduced-motion')&&(!media.matches||root.hasAttribute('data-motion-override'));}
  function draw(){
    const i=Math.max(0,selected()),color=['#3683fc','#159d8d','#cf8b32'][i];
    for(const stage of stages){
      const show=allowed()&&stage.visible&&stage.flow&&!(stage.config.signal===false&&doc.querySelector('.zh23-route-result')?.dataset.state==='running');
      stage.packets.forEach(({dot,halo},index)=>{
        dot.style.display=halo.style.display=show?'':'none';if(!show)return;
        const length=stage.flow.getTotalLength(),phase=(time/3.7+index*.48)%1;
        const p=stage.flow.getPointAtLength(length*phase);
        for(const node of [dot,halo]){node.setAttribute('cx',p.x);node.setAttribute('cy',p.y);node.setAttribute('fill',color);}
      });
    }
  }
  function frame(now){raf=0;if(!allowed()||disposed||!stages.some(s=>s.visible))return;if(!last||now-last>=33){time+=last?Math.min((now-last)/1000,.07):0;last=now;draw();}raf=win.requestAnimationFrame(frame);}
  function sync(){win.cancelAnimationFrame(raf);raf=0;last=0;draw();if(allowed()&&stages.some(s=>s.visible)&&!disposed)raf=win.requestAnimationFrame(frame);}
  const resize=win.ResizeObserver?new win.ResizeObserver(queueLayout):null;
  stages.forEach(stage=>{resize?.observe(stage.host);for(const selector of [stage.config.source,stage.config.core,...stage.config.targets]){const el=stage.host.querySelector(selector);if(el)resize?.observe(el);}});
  const observer=win.IntersectionObserver?new win.IntersectionObserver(entries=>{for(const entry of entries){const stage=stages.find(s=>s.host===entry.target);if(stage)stage.visible=entry.isIntersecting;}sync();}):null;
  stages.forEach(stage=>observer?.observe(stage.host));
  const change=()=>{layout();sync();};
  doc.addEventListener('zhihui:platform-selected',change);doc.addEventListener('zhihui:motion-change',sync);
  doc.addEventListener('visibilitychange',sync);media.addEventListener('change',sync);win.addEventListener('resize',queueLayout);
  const hide=()=>{active=false;sync();},show=()=>{active=true;queueLayout();};win.addEventListener('pagehide',hide);win.addEventListener('pageshow',show);
  layout();sync();doc.fonts?.ready.then(()=>{if(!disposed)queueLayout();});
  return{layout,dispose(){disposed=true;win.cancelAnimationFrame(raf);win.cancelAnimationFrame(layoutFrame);resize?.disconnect();observer?.disconnect();
    doc.removeEventListener('zhihui:platform-selected',change);doc.removeEventListener('zhihui:motion-change',sync);doc.removeEventListener('visibilitychange',sync);
    media.removeEventListener('change',sync);win.removeEventListener('resize',queueLayout);win.removeEventListener('pagehide',hide);win.removeEventListener('pageshow',show);
    stages.forEach(stage=>{stage.layer.remove();stage.flow?.remove();stage.receipt?.remove();});}};
}

export function attachNavigation(doc=document,win=window) {
  const wrapper=doc.querySelector('.navigation-wrap'),nav=doc.querySelector('.nav-center');
  if(!wrapper||!nav)return{dispose(){}};
  const links=[...nav.querySelectorAll('a[href^="#"]')];
  const marker=doc.createElement('span');marker.className='nav-current-marker';marker.setAttribute('aria-hidden','true');nav.prepend(marker);
  let frame=0,active=null,disposed=false;
  function place(link){
    if(!link){marker.style.opacity='0';return;}
    const box=link.getBoundingClientRect(),bounds=nav.getBoundingClientRect();
    if(!bounds.width){marker.style.opacity='0';return;}
    marker.style.opacity='1';marker.style.width=box.width+'px';
    marker.style.transform=`translate(${box.left-bounds.left}px,${box.top-bounds.top}px)`;
  }
  function update(){
    frame=0;if(disposed)return;
    wrapper.toggleAttribute('data-scrolled',win.scrollY>18);
    // Match the existing scroll controller so both agree on section ownership.
    const candidate=links.filter(link=>doc.querySelector(link.getAttribute('href'))?.getBoundingClientRect().top<180).at(-1);
    active=candidate||null;
    links.forEach(link=>{if(link===active)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');});
    place(active);
  }
  function queue(){if(!frame&&!disposed)frame=win.requestAnimationFrame(update);}
  const handlers=links.map(link=>{
    const enter=()=>place(link),leave=()=>place(active);
    link.addEventListener('pointerenter',enter);link.addEventListener('focus',enter);
    link.addEventListener('pointerleave',leave);link.addEventListener('blur',leave);
    return()=>{link.removeEventListener('pointerenter',enter);link.removeEventListener('focus',enter);link.removeEventListener('pointerleave',leave);link.removeEventListener('blur',leave);};
  });
  win.addEventListener('scroll',queue,{passive:true});win.addEventListener('resize',queue);
  doc.fonts?.ready.then(()=>{if(!disposed)queue();});update();
  return{dispose(){disposed=true;win.cancelAnimationFrame(frame);handlers.forEach(fn=>fn());marker.remove();win.removeEventListener('scroll',queue);win.removeEventListener('resize',queue);}};
}
if(typeof document!=='undefined'){attachConnectedPaths();attachNavigation();}
