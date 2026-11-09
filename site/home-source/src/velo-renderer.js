import * as THREE from 'three';
import {createVeloScene,VIEWS} from './velo-lite/model.js';

export const MAX_PIXELS=420000, MOBILE_PIXELS=220000;
export function renderSize(width,height,mobile=false,quality=1) {
  const w=Number.isFinite(width)?Math.max(1,Math.min(4096,width)):1;
  const h=Number.isFinite(height)?Math.max(1,Math.min(4096,height)):1;
  const scale=Math.min(1,Math.sqrt((mobile?MOBILE_PIXELS:MAX_PIXELS)*quality/(w*h)));
  return [Math.max(1,Math.floor(w*scale)),Math.max(1,Math.floor(h*scale))];
}
export function createVeloRenderer(canvas) {
  let renderer,model;
  try {
    renderer=new THREE.WebGLRenderer({canvas,alpha:false,antialias:false,stencil:false,
      preserveDrawingBuffer:false,powerPreference:'low-power'});
    renderer.setPixelRatio(1);
    renderer.outputColorSpace=THREE.SRGBColorSpace;
    renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.05;
    renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFShadowMap;
    model=createVeloScene();
    const camera=new THREE.PerspectiveCamera(38,1,.1,260);
    const target=new THREE.Vector3(0,1.55,0);
    let size='',quality=1,slow=0;
    return {
      render({width,height,time,delta=0,yaw=VIEWS.coast.yaw,pitch=VIEWS.coast.pitch,view='coast',mobile=false,frameMs=0}) {
        renderer.shadowMap.enabled=!mobile;model.sun.castShadow=!mobile;
        if(frameMs>55)slow++;else slow=Math.max(0,slow-1);
        if(slow>45){quality=.65;slow=0;}
        const [w,h]=renderSize(width,height,mobile,quality);
        if(size!==`${w}/${h}`){renderer.setSize(w,h,false);size=`${w}/${h}`;}
        camera.aspect=Math.max(.2,width/Math.max(1,height));camera.updateProjectionMatrix();
        const preset=VIEWS[view]||VIEWS.coast;
        const radius=preset.radius*Math.max(1,.95/camera.aspect);
        camera.position.set(target.x+Math.sin(yaw)*Math.cos(pitch)*radius,
          target.y+Math.sin(pitch)*radius,target.z+Math.cos(yaw)*Math.cos(pitch)*radius);
        camera.lookAt(target);model.update(delta,time);renderer.render(model.scene,camera);
        return {calls:renderer.info.render.calls,triangles:renderer.info.render.triangles,pixels:w*h};
      },
      dispose(){model.dispose();renderer.dispose();renderer.forceContextLoss();},
    };
  }catch(error){model?.dispose();renderer?.dispose();throw error;}
}
