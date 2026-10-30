(() => {
const root=document.documentElement, media=matchMedia('(prefers-reduced-motion: reduce)');
let paused=parent!==window, override=false, timer=0, step=0;
const values=[321,328,315,337,324,319,331];
function sync(){clearInterval(timer);timer=0;const running=!paused&&(!media.matches||override)&&!document.hidden;root.toggleAttribute('data-motion-override',override);root.toggleAttribute('data-scene-paused',!running);root.dataset.playback=running?'playing':'paused';if(running)timer=setInterval(()=>{document.getElementById('demoThroughput').textContent=values[++step%values.length];},800)}
addEventListener('message',event=>{if(event.source!==parent||event.data?.type!=='task-playback'||typeof event.data.paused!=='boolean')return;paused=event.data.paused;override=event.data.reducedMotionOverride===true;sync()});
media.addEventListener('change',sync);document.addEventListener('visibilitychange',sync);addEventListener('pagehide',()=>{paused=true;sync()});sync();if(parent!==window)parent.postMessage({type:'task-ready'},'*');
})();