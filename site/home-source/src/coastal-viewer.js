import * as THREE from 'three';
import {createCoastalModel, createCoastalSky} from './coastal-model.js';
import {COASTAL_VIEWS, coastalRadius} from './coastal-camera.js';

export function startCoastalViewer({win = window, doc = document, rendererFactory} = {}) {
  const host = doc.getElementById('coastalViewport'), root = doc.documentElement;
  const status = doc.getElementById('coastalStatus'), media = win.matchMedia('(prefers-reduced-motion: reduce)');
  let renderer, model, atmosphere, observer, raf = 0, previous = 0, elapsed = 0, disposed = false, failed = false;
  let paused = win.parent !== win, override = false, active = true, visible = true, rotating = false;
  let selectedView = 'side', yaw = COASTAL_VIEWS.side.yaw, pitch = COASTAL_VIEWS.side.pitch, drag = null;
  const listeners = [];
  function listen(target, type, fn, options) { target.addEventListener(type, fn, options); listeners.push(() => target.removeEventListener(type, fn, options)); }
  const controls = [...doc.querySelectorAll('[data-coast-view],#coastReset,#coastRotate')];
  function fail() {
    failed = true; win.cancelAnimationFrame(raf); raf = 0;
    root.dataset.scene = 'error'; status.hidden = false;
    status.textContent = '当前设备暂未开启三维视图。可在支持 WebGL 的浏览器中拖动查看海边骑行场景。';
    controls.forEach(button => { button.disabled = true; });
  }
  try {
    renderer = rendererFactory ? rendererFactory() : new THREE.WebGLRenderer({alpha: true, antialias: true, powerPreference: 'low-power'});
    renderer.setPixelRatio(Math.min(win.devicePixelRatio || 1, 1.5));
    renderer.setClearColor(0xc3e2d9, 1); renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFShadowMap;
    const canvas = renderer.domElement; canvas.tabIndex = 0;
    canvas.setAttribute('aria-label', '比奇堡三维同人街区：骑行的海绵宝宝、派大星、章鱼哥、蟹老板与痞老板。拖动或方向键旋转，按钮切换街区和骑行近景，Home 复位。');
    canvas.setAttribute('role', 'img'); canvas.style.touchAction = 'pan-y'; host.append(canvas);
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(35, 1, .1, 300);
    scene.fog = new THREE.FogExp2(0xb3d9db, .008);
    model = createCoastalModel(); scene.add(model.group);
    atmosphere = createCoastalSky(); scene.add(atmosphere.sky);
    scene.add(new THREE.HemisphereLight(0xf4fff5, 0x9ba999, 1.6));
    const key = new THREE.DirectionalLight(0xffefd4, 2.5); key.position.set(-7, 12, 8); key.castShadow = true;
    key.shadow.mapSize.set(1024,1024); key.shadow.camera.left = key.shadow.camera.bottom = -25;
    key.shadow.camera.right = key.shadow.camera.top = 25; key.shadow.camera.far = 70; key.shadow.normalBias = .025;
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xe4f0ee, 1.3); fill.position.set(3, 3, 6); scene.add(fill);
    const target = new THREE.Vector3(...COASTAL_VIEWS.side.target);
    let radius = coastalRadius(selectedView, 1), initialFraming = true;
    function view() {
      camera.position.set(target.x+Math.sin(yaw)*Math.cos(pitch)*radius, target.y+Math.sin(pitch)*radius, target.z+Math.cos(yaw)*Math.cos(pitch)*radius);
      camera.lookAt(target);
    }
    function render() { if (!disposed && !failed && visible && active && !doc.hidden) { view(); renderer.render(scene,camera); } }
    function allowed() {return !paused && (!media.matches || override) && active && visible && !doc.hidden && !failed && !disposed;}
    function frame(now) {
      raf = 0; if (!allowed()) {previous = 0; return;}
      if (!previous || now-previous >= 1000/30) {
        const delta = previous ? Math.min((now-previous)/1000,.06) : 0; previous = now; elapsed += delta;
        if (rotating && !drag) yaw += delta*.10;
        model.update(elapsed); render();
      }
      raf = win.requestAnimationFrame(frame);
    }
    function sync() {
      win.cancelAnimationFrame(raf); raf=0;previous=0;
      root.dataset.playback = allowed() ? 'playing' : 'paused';
      if (allowed()) raf=win.requestAnimationFrame(frame); else render();
    }
    function resize() {
      if (disposed || failed) return;
      const width=host.clientWidth, height=host.clientHeight; if(!width||!height)return;
      renderer.setSize(width,height,false);camera.aspect=width/height;
      if(initialFraming){
        initialFraming=false;selectedView='side';
        const preset=COASTAL_VIEWS[selectedView];target.set(...preset.target);yaw=preset.yaw;pitch=preset.pitch;
        doc.querySelectorAll('[data-coast-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.coastView===selectedView)));
      }
      radius=coastalRadius(selectedView,camera.aspect);
      camera.updateProjectionMatrix();render();
    }
    function stopOrbit() {
      rotating=false;
      const button=doc.getElementById('coastRotate');
      button.setAttribute('aria-pressed','false');button.textContent='环绕查看';
    }
    function setView(value) {
      stopOrbit();
      selectedView=COASTAL_VIEWS[value]?value:'side';
      const preset=COASTAL_VIEWS[selectedView];
      yaw=preset.yaw;pitch=preset.pitch;target.set(...preset.target);radius=coastalRadius(selectedView,camera.aspect);
      doc.querySelectorAll('[data-coast-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.coastView===value)));
      render();
    }
    const rotate=doc.getElementById('coastRotate');
    listen(rotate,'click',()=>{
      rotating=!rotating;rotate.setAttribute('aria-pressed',String(rotating));
      rotate.textContent=rotating?'停止环绕':'环绕查看';
      if(rotating&&(media.matches&&!override||paused)) {
        // Explicit motion request, still subject to the parent page's pause policy.
        win.parent.postMessage({type:'pelican-motion-request'},'*');
        if(win.parent===win){override=true;paused=false;}
      }
      sync();
    });
    doc.querySelectorAll('[data-coast-view]').forEach(button=>listen(button,'click',()=>setView(button.dataset.coastView)));
    listen(doc.getElementById('coastReset'),'click',()=>setView('side'));
    listen(canvas,'pointerdown',event=>{if(event.button!==0)return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,yaw,pitch};rotating=false;rotate.setAttribute('aria-pressed','false');rotate.textContent='环绕查看';});
    listen(canvas,'pointermove',event=>{
      if(!drag||event.pointerId!==drag.id)return;
      const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
      if(event.pointerType==='touch'&&Math.abs(dy)>Math.abs(dx)*1.2){drag=null;return;}
      if(Math.abs(dx)>5)canvas.setPointerCapture?.(event.pointerId);
      yaw=drag.yaw+dx*.008;pitch=Math.max(.12,Math.min(.9,drag.pitch+dy*.004));render();
    });
    const release=()=>{
      if(drag&&canvas.hasPointerCapture?.(drag.id))canvas.releasePointerCapture(drag.id);
      drag=null;
    };
    listen(canvas,'pointerup',release);listen(canvas,'pointercancel',release);listen(canvas,'lostpointercapture',release);
    listen(win,'pointerup',release);listen(win,'blur',release);
    listen(canvas,'keydown',event=>{
      if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home'].includes(event.key))return;
      event.preventDefault();stopOrbit();
      if(event.key==='Home')return setView('side');
      if(event.key==='ArrowLeft')yaw-=.14;if(event.key==='ArrowRight')yaw+=.14;
      if(event.key==='ArrowUp')pitch=Math.min(.9,pitch+.08);if(event.key==='ArrowDown')pitch=Math.max(.12,pitch-.08);render();
    });
    listen(canvas,'webglcontextlost',event=>{event.preventDefault();fail();});
    observer=new win.ResizeObserver(resize);observer.observe(host);
    if(win.IntersectionObserver){const io=new win.IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();});io.observe(host);listeners.push(()=>io.disconnect());}
    listen(win,'message',event=>{
      if(event.source!==win.parent||event.data?.type!=='pelican-playback'||typeof event.data.paused!=='boolean')return;
      paused=event.data.paused;override=event.data.reducedMotionOverride===true;sync();
    });
    listen(media,'change',sync);listen(doc,'visibilitychange',sync);
    listen(win,'pagehide',()=>{active=false;sync();});listen(win,'pageshow',()=>{active=true;sync();});
    resize();root.dataset.scene='ready';status.hidden=true;controls.forEach(button=>{button.disabled=false;});sync();
    if(win.parent!==win)win.parent.postMessage({type:'pelican-ready'},'*');
    return {setView,dispose(){if(disposed)return;disposed=true;win.cancelAnimationFrame(raf);observer.disconnect();listeners.forEach(fn=>fn());model.dispose();atmosphere.dispose();key.shadow.dispose();renderer.dispose();canvas.remove();}};
  } catch {
    fail();observer?.disconnect();listeners.forEach(fn=>fn());model?.dispose();atmosphere?.dispose();renderer?.dispose();
    return {dispose(){disposed=true;}};
  }
}

if (typeof document !== 'undefined' && document.getElementById('coastalViewport')) startCoastalViewer();
