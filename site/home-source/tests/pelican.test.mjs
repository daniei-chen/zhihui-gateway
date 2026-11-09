import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import * as THREE from 'three';
import {createCoastalModel, createCoastalSky} from '../src/coastal-model.js';
import {startCoastalViewer} from '../src/coastal-viewer.js';
import {COASTAL_VIEWS, coastalRadius} from '../src/coastal-camera.js';
import {BIKINI_LAYOUT} from '../src/bikini-layout.js';
import {surfacePatch} from '../src/character-surface.js';
import {squidHeadGeometry,squidNoseGeometry} from '../src/squidward-shape.js';

// Retired scene remains covered for a source-level rollback, but is not loaded by the homepage.
const html = readFileSync(new URL('../scenes/pelican-ride.html', import.meta.url), 'utf8');
test('SpongeBob fan-art model and bicycle have finite geometry and articulated moving parts', () => {
  const model=createCoastalModel();let vertices=0,meshes=0;
  try {
    model.group.traverse(item=>{if(!item.isMesh)return;meshes++;const a=item.geometry.getAttribute('position');vertices+=a.count;
      for(const n of a.array)assert.ok(Number.isFinite(n));});
    assert.ok(meshes>120);assert.ok(meshes<260,meshes);
    // Keep the original performance ceiling while adding street-depth detail.
    let triangles=0;model.group.traverse(o=>{if(o.isMesh)triangles+=(o.geometry.index?.count??o.geometry.attributes.position.count)/3;});
    assert.ok(vertices<550000,vertices);assert.ok(triangles<210000,triangles);
    assert.equal(model.wheels.length,2);assert.equal(model.legs.length,2);
    const before=model.legs[0].foot.position.clone();model.update(1.2);
    assert.notDeepEqual(model.legs[0].foot.position.toArray(),before.toArray());
    assert.notEqual(model.wheels[0].rotation.z,0);
    assert.equal(model.materials.frame.color.getHex(),0x579b92);
    assert.match(model.rider.name,/SpongeBob/);
    assert.ok(model.rider.getObjectByName('Two front teeth'));
    assert.ok(model.rider.getObjectByName('Blue iris'));
    assert.equal(model.reflections.length,24);
    const reflection=model.reflections[0].scale.x;model.update(3.5);
    assert.notEqual(model.reflections[0].scale.x,reflection);
  } finally {model.dispose();}
});

test('rider faces the bicycle direction, grips the bars and keeps both feet on actual opposed pedals',()=>{
  const model=createCoastalModel();
  try{
    model.group.updateMatrixWorld(true);
    const forward=new THREE.Vector3(0,0,1).applyQuaternion(model.cyclist.torso.getWorldQuaternion(new THREE.Quaternion()));
    assert.ok(forward.dot(new THREE.Vector3(1,0,0))>.99999);
    const hands=[];model.rider.traverse(o=>{if(o.name==='Hand on handlebar')hands.push(o);});
    assert.equal(hands.length,2);
    for(const hand of hands){const p=model.cyclist.cycle.worldToLocal(hand.getWorldPosition(new THREE.Vector3()));
      assert.ok(Math.abs(p.x-.72)<1e-9);assert.ok(Math.abs(Math.abs(p.z)-.31)<1e-9);}
    for(let i=0;i<120;i++){
      model.update(i*.07);
      const [a,b]=model.cyclist.pedals.map(p=>p.position);
      assert.ok(Math.abs(a.x+b.x)<1e-9);assert.ok(Math.abs(a.y+b.y-1.30)<1e-9);
      for(let j=0;j<2;j++){
        const pedal=model.cyclist.pedals[j].position,foot=model.legs[j].foot.position;
        assert.ok(Math.abs(foot.y-pedal.y-.064)<1e-9);assert.ok(Math.abs(foot.x-pedal.x-.040)<1e-9);
        assert.ok(Math.abs(foot.z-pedal.z)<1e-9);
      }
    }
  }finally{model.dispose();}
});

test('street uses a shared human-scale contract, not house-sized bicycles or miniature neighbors',()=>{
  const model=createCoastalModel();
  try {
    const bounds=item=>new THREE.Box3().setFromObject(item),size=item=>bounds(item).getSize(new THREE.Vector3());
    const {landmarks}=model.neighborhood,cycle=model.cyclist.cycle,bikeHeight=1.95*BIKINI_LAYOUT.cyclist.scale;
    const riderHeight=size(cycle).y,houseHeight=size(landmarks['Pineapple house']).y;
    assert.equal(cycle.scale.x,.48);
    assert.ok(houseHeight/riderHeight>4.4 && houseHeight/riderHeight<5.3);
    assert.ok(size(landmarks['Squidward house']).y/riderHeight>4);
    for(const name of ['Patrick Star','Squidward Tentacles','Mr Krabs']) {
      assert.ok(size(landmarks[name]).y>bikeHeight*1.4,name);
      assert.ok(Math.abs(bounds(landmarks[name]).min.y-.08)<.012,`${name} feet must meet sand`);
    }
    const squidHead=landmarks['Squidward Tentacles'].getObjectByName('Continuous Squidward head and neck');
    const local=squidHead.userData.localBounds;
    const headSize=new THREE.Vector3(...local.max).sub(new THREE.Vector3(...local.min));
    assert.ok(headSize.z>.5&&headSize.z/headSize.x<.7,'Volumetric head must be shallower than its width, not a spherical rear skull');
    const plankton=size(landmarks.Plankton).y;
    assert.ok(plankton>.15 && plankton<riderHeight*.17,'Plankton is intentionally tiny, not adult-sized');
    assert.ok(Math.abs(bounds(cycle).min.y-.102)<.006,'Tires must meet the actual road');
    assert.ok(landmarks['Pineapple house'].position.x>landmarks['Squidward house'].position.x);
    assert.ok(landmarks['Squidward house'].position.x>landmarks['Patrick rock house'].position.x);
    assert.ok(landmarks['Krusty Krab'].position.z<landmarks['Pineapple house'].position.z-4);
    assert.ok(model.group.getObjectByName('Background neighborhood'));
    assert.ok(model.group.getObjectByName('Foreground coral garden'));
    assert.ok(size(landmarks['Conch Street']).z>17,'The road must turn into the town, not just run horizontally');
    for(const [house,door] of [['Pineapple house','Pineapple front door'],['Squidward house','Moai front door'],['Krusty Krab','Krusty Krab double doors']]) {
      const anchor=landmarks[house].getObjectByName(door);
      assert.ok(anchor);
      anchor.updateWorldMatrix(true,false);
      const local=anchor.userData.localBounds;
      const geometryBounds=new THREE.Box3(new THREE.Vector3(...local.min),new THREE.Vector3(...local.max));
      const doorHeight=geometryBounds.applyMatrix4(anchor.matrixWorld).getSize(new THREE.Vector3()).y;
      assert.ok(doorHeight>1.7 && doorHeight<2.6,`${house}: actual door height ${doorHeight} must fit the adult cast`);
    }
  } finally {model.dispose();}
});

test('Bikini Bottom has named landmarks, recognizable character details and separately moving palms',()=>{
  const model=createCoastalModel();
  try{
    for(const name of ['Conch Street','Pineapple house','Squidward house','Patrick rock house','Krusty Krab','Patrick Star','Squidward Tentacles','Mr Krabs','Plankton'])
      assert.ok(model.neighborhood.landmarks[name],name);
    for(const name of ['Pineapple shell','Long stone nose','Barrel vaulted roof','Clamshell restaurant sign','Five-point starfish silhouette','Long drooping nose','Tall eye stalk','Single yellow eye','Twin curved fork blades','76 animated chain links on closed drivetrain','Bowden brake cable','Pedal reflector'])
      assert.ok(model.group.getObjectByName(name),name);
    assert.equal(model.cyclist.chain.isInstancedMesh,true);assert.equal(model.cyclist.chain.count,76);
    const palm=model.neighborhood.palms[0].children.find(o=>o.isGroup),before=palm.rotation.z;
    const matrix=new THREE.Matrix4();model.cyclist.chain.getMatrixAt(0,matrix);const previous=matrix.toArray();
    model.update(4);assert.notEqual(palm.rotation.z,before);model.cyclist.chain.getMatrixAt(0,matrix);assert.notDeepEqual(matrix.toArray(),previous);
  }finally{model.dispose();}
});

test('profile details use surface-bound curved color instead of protruding pupil and trunks plates',()=>{
  const patches=[];
  const center=[.1,2,.3],radii=[.2,.3,.15],lift=.002;
  surfacePatch({mesh:geometry=>{patches.push(geometry);return{};},parent:{},center,radii,lift,material:'pupil'});
  try {
    const geometry=patches[0],positions=geometry.attributes.position,normals=geometry.attributes.normal;
    for(let i=0;i<positions.count;i++){
      const x=(positions.getX(i)-center[0])/(radii[0]+lift),y=(positions.getY(i)-center[1])/(radii[1]+lift),z=(positions.getZ(i)-center[2])/(radii[2]+lift);
      assert.ok(Math.abs(x*x+y*y+z*z-1)<.00001);
      assert.ok(normals.getZ(i)>0,'Surface winding must face out, not disappear under backface culling');
    }
  }finally{patches.forEach(g=>g.dispose());}
  const model=createCoastalModel();
  try {
    assert.ok(model.rider.getObjectByName('Integrated smile cheek'));
    assert.ok(model.neighborhood.landmarks['Patrick Star'].getObjectByName('Surface-following swimwear flower'));
    assert.ok(model.neighborhood.landmarks['Squidward Tentacles'].getObjectByName('Continuous Squidward head and neck'));
    const eye=model.rider.getObjectByName('Large white eye');
    assert.ok(eye.scale.z>.14,'Eye needs embedded spherical volume rather than a flat decal disk');
    for(const name of ['Front wicker basket with fresh flowers','Basket floor','Basket stem bracket','Basket fork stay','Fresh flower stem','Flower petal'])assert.ok(model.group.getObjectByName(name),name);
    const basket=model.group.getObjectByName('Front wicker basket with fresh flowers');
    const bounds=new THREE.Box3().setFromObject(basket),wheelTop=(.59+.593)*BIKINI_LAYOUT.cyclist.scale+BIKINI_LAYOUT.cyclist.position[1];
    // Fork stays intentionally reach down; only the basket floor must clear the tire.
    const floor=basket.getObjectByName('Basket floor');floor.updateWorldMatrix(true,false);
    const local=floor.userData.localBounds;
    const floorBounds=new THREE.Box3(new THREE.Vector3(...local.min),new THREE.Vector3(...local.max)).applyMatrix4(floor.matrixWorld);
    assert.ok(floorBounds.min.y>wheelTop+.06);assert.ok(bounds.max.y<1.2,'Bouquet must not obscure the rider face');
  }finally{model.dispose();}
});

test('Patrick arms face outward under real backface culling, rather than appearing hollow in profile',()=>{
  const model=createCoastalModel();
  try{
    const patrick=model.neighborhood.landmarks['Patrick Star'];
    const pinkMeshes=[];patrick.traverse(o=>{if(o.isMesh&&o.material===model.materials.pink)pinkMeshes.push(o);});
    let front=0,back=0;
    for(const item of pinkMeshes){const p=item.geometry.attributes.position,n=item.geometry.attributes.normal;
      for(let i=0;i<p.count;i++)if(p.getX(i)>.56&&p.getY(i)>1.26&&p.getY(i)<1.55){
        if(p.getZ(i)>.04){assert.ok(n.getZ(i)>0);front++;}
        if(p.getZ(i)<-.04){assert.ok(n.getZ(i)<0);back++;}
      }
    }
    assert.ok(front>12&&back>12,'Both sides of the arm need actual outward surface normals');
  }finally{model.dispose();}
});

test('Squidward head and nose are closed continuous outward-facing surfaces, not stacked spheres',()=>{
  const head=squidHeadGeometry(),nose=squidNoseGeometry();
  try {
    for(const geometry of [head,nose]) {
      const p=geometry.attributes.position,n=geometry.attributes.normal,indices=geometry.index,edges=new Map();
      let volume=0;
      for(let i=0;i<p.count;i++) {
        assert.ok([p.getX(i),p.getY(i),p.getZ(i)].every(Number.isFinite));
        assert.ok(Math.abs(new THREE.Vector3().fromBufferAttribute(n,i).length()-1)<1e-5);
      }
      for(let i=0;i<indices.count;i+=3) {
        const ids=[indices.getX(i),indices.getX(i+1),indices.getX(i+2)];
        const [a,b,c]=ids.map(id=>new THREE.Vector3().fromBufferAttribute(p,id));
        const face=b.clone().sub(a).cross(c.clone().sub(a));
        assert.ok(face.length()>1e-9,'No collapsed pole triangles');
        volume+=a.dot(b.clone().cross(c))/6;
        for(let j=0;j<3;j++) {
          const u=ids[j],v=ids[(j+1)%3],key=`${Math.min(u,v)}:${Math.max(u,v)}`;
          const edge=edges.get(key)||{count:0,direction:0};
          edge.count++;edge.direction+=u<v?1:-1;edges.set(key,edge);
        }
      }
      assert.ok(volume>.005,'Closed volume must face outward under backface culling');
      for(const edge of edges.values()) {assert.equal(edge.count,2);assert.equal(edge.direction,0);}
    }
    const p=head.attributes.position,n=head.attributes.normal;
    let neckWidth=0,neckDepth=0,crownWidth=0,crownDepth=0;
    for(let i=0;i<p.count;i++) {
      const y=p.getY(i),x=p.getX(i),z=p.getZ(i)+.015;
      if(y>1.43&&y<1.70) {neckWidth=Math.max(neckWidth,Math.abs(x));neckDepth=Math.max(neckDepth,Math.abs(z));}
      if(y>2.30&&y<2.50) {crownWidth=Math.max(crownWidth,Math.abs(x));crownDepth=Math.max(crownDepth,Math.abs(z));}
      if(y>1.44&&y<2.70) assert.ok(n.getX(i)*x+n.getZ(i)*z>0,'Both profiles need actual outward radial normals');
    }
    assert.ok(crownWidth>neckWidth*3&&crownDepth>neckDepth*2);
    assert.ok(crownDepth/crownWidth<.65,'Independent depth avoids the bowling-ball silhouette');
    head.computeBoundingBox();nose.computeBoundingBox();
    assert.ok(head.boundingBox.min.y<1.5,'Neck embeds into the polo rather than floating above it');
    assert.ok(nose.boundingBox.max.z>head.boundingBox.max.z+.28);
    assert.ok(nose.boundingBox.min.y>1.74&&nose.boundingBox.max.y<2.32);
  }finally{head.dispose();nose.dispose();}
});

test('default close view separates Squidward from SpongeBob at phone and desktop aspects throughout idle animation',()=>{
  const model=createCoastalModel();
  const projectBounds=(object,camera)=>{
    const b=new THREE.Box3().setFromObject(object),result=new THREE.Box2();
    for(const x of [b.min.x,b.max.x])for(const y of [b.min.y,b.max.y])for(const z of [b.min.z,b.max.z]) {
      const p=new THREE.Vector3(x,y,z).project(camera);result.expandByPoint(new THREE.Vector2(p.x,p.y));
    }
    return result;
  };
  try {
    for(const time of [0,.6,2,5,10,17,25]) {
      model.update(time);
      for(const aspect of [.55,.8,1.4,2]) {
        const p=COASTAL_VIEWS.side,r=coastalRadius('side',aspect),camera=new THREE.PerspectiveCamera(35,aspect,.1,300);
        const target=new THREE.Vector3(...p.target);
        camera.position.set(target.x+Math.sin(p.yaw)*Math.cos(p.pitch)*r,target.y+Math.sin(p.pitch)*r,target.z+Math.cos(p.yaw)*Math.cos(p.pitch)*r);
        camera.lookAt(target);camera.updateMatrixWorld(true);
        const squid=projectBounds(model.neighborhood.landmarks['Squidward Tentacles'],camera);
        const sponge=projectBounds(model.cyclist.torso,camera);
        assert.ok(sponge.min.x-squid.max.x>.06,`Character silhouette gap: aspect ${aspect}, time ${time}`);
        for(const b of [squid,sponge]) {
          assert.ok(b.min.x>-.98&&b.max.x<.98&&b.min.y>-.98&&b.max.y<.98,'Both characters must remain in frame');
        }
      }
    }
  }finally{model.dispose();}
});

test('all five camera presets fit their intended span on both portrait and landscape viewports',()=>{
  for(const [name,preset] of Object.entries(COASTAL_VIEWS))for(const aspect of [.55,.8,1.4,2]){
    const radius=coastalRadius(name,aspect),halfHeight=(radius-preset.depth)*Math.tan(35*Math.PI/360);
    assert.ok(Number.isFinite(radius));assert.ok(halfHeight*2>=preset.height-1e-9);
    assert.ok(halfHeight*2*aspect>=preset.width-1e-9);
  }
});

test('actual landmark corners and the riding rig stay within every preset at phone and wide-screen aspects',()=>{
  const model=createCoastalModel();
  try {
    for(const name of Object.keys(COASTAL_VIEWS)) {
      const names=name==='town'?['Patrick rock house','Squidward house','Pineapple house','Krusty Krab']
        :name==='homes'?['Patrick rock house','Squidward house','Pineapple house']:name==='restaurant'?['Krusty Krab']:[];
      const bounds=new THREE.Box3();
      for(const item of names)bounds.union(new THREE.Box3().setFromObject(model.neighborhood.landmarks[item]));
      if(!names.length)bounds.setFromObject(model.cyclist.cycle);
      for(const aspect of [.55,.8,1.4,2]) {
        const p=COASTAL_VIEWS[name],r=coastalRadius(name,aspect),camera=new THREE.PerspectiveCamera(35,aspect,.1,300);
        const target=new THREE.Vector3(...p.target);
        camera.position.set(target.x+Math.sin(p.yaw)*Math.cos(p.pitch)*r,target.y+Math.sin(p.pitch)*r,target.z+Math.cos(p.yaw)*Math.cos(p.pitch)*r);
        camera.lookAt(target);camera.updateMatrixWorld(true);
        for(const x of [bounds.min.x,bounds.max.x])for(const y of [bounds.min.y,bounds.max.y])for(const z of [bounds.min.z,bounds.max.z]) {
          const projected=new THREE.Vector3(x,y,z).project(camera);
          assert.ok(Math.abs(projected.x)<.98&&Math.abs(projected.y)<.98,`${name}, aspect ${aspect}: geometry clipped`);
        }
      }
    }
  } finally {model.dispose();}
});

test('coast is continuous with upward sand normals and distant directional sunset, not a plinth or sun ball', () => {
  const model=createCoastalModel(),atmosphere=createCoastalSky();
  try {
    const sand=model.group.getObjectByName('Unbroken sandy shore');
    sand.geometry.computeBoundingBox();
    assert.ok(sand.geometry.boundingBox.max.x-sand.geometry.boundingBox.min.x>100);
    const normals=sand.geometry.getAttribute('normal');
    for(let i=0;i<normals.count;i++)assert.ok(normals.getY(i)>.99);
    assert.ok(model.ocean.material.isShaderMaterial);
    model.update(2.3);assert.equal(model.ocean.material.uniforms.time.value,2.3);
    assert.ok(atmosphere.sky.material.isShaderMaterial);
    assert.equal(atmosphere.sky.material.depthWrite,false);
    assert.equal(atmosphere.sky.material.side,THREE.BackSide);
    assert.ok(atmosphere.sky.material.uniforms.sunDirection.value.y<.05);
  }finally{model.dispose();atmosphere.dispose();}
});

test('pedaling keeps equal-length articulated legs and disposes all model resources exactly once', () => {
  const model=createCoastalModel(),resources=new Set();
  model.group.traverse(o=>{if(o.geometry)resources.add(o.geometry);if(o.material)resources.add(o.material);});
  const calls=new Map();resources.forEach(o=>o.addEventListener('dispose',()=>calls.set(o,(calls.get(o)||0)+1)));
  for(let i=0;i<100;i++){
    model.update(i*.1);
    for(const leg of model.legs){
      assert.ok(Math.abs(leg.upper.scale.y-.68)<1e-10);
      assert.ok(Math.abs(leg.lower.scale.y-.68)<1e-10);
      assert.ok(leg.foot.position.toArray().every(Number.isFinite));
    }
  }
  model.dispose();assert.equal(calls.size,resources.size);assert.ok([...calls.values()].every(n=>n===1));
});

function fixture(failure=false,width=720,height=540){
  const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;
  const frames=new Map(),observers=[];let serial=0,hidden=false,change,rendered=0,disposed=false;
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  Object.defineProperty(w.document.getElementById('coastalViewport'),'clientWidth',{value:width});
  Object.defineProperty(w.document.getElementById('coastalViewport'),'clientHeight',{value:height});
  w.matchMedia=()=>({matches:false,addEventListener:(_,fn)=>{change=fn;},removeEventListener(){}});
  w.requestAnimationFrame=fn=>{frames.set(++serial,fn);return serial;};w.cancelAnimationFrame=id=>frames.delete(id);
  w.ResizeObserver=class{observe(){}disconnect(){}};
  w.IntersectionObserver=class{constructor(fn){observers.push(fn);}observe(){}disconnect(){}};
  const canvas=w.document.createElement('canvas');
  const viewer=startCoastalViewer({win:w,doc:w.document,rendererFactory:()=>{
    if(failure)throw Error('fixture-no-WebGL');
    return{domElement:canvas,shadowMap:{},setPixelRatio(){},setClearColor(){},setSize(){},
      render(){rendered++;},dispose(){disposed=true;}};
  }});
  return{w,viewer,canvas,frames,observers,get rendered(){return rendered;},get disposed(){return disposed;},
    tick(now){const callbacks=[...frames.values()];frames.clear();callbacks.forEach(fn=>fn(now));},
    hidden(value){hidden=value;w.document.dispatchEvent(new w.Event('visibilitychange'));}};
}

test('viewer drag/key controls render and pause, offscreen and teardown stop every frame',()=>{
  const f=fixture();
  try{
    assert.equal(f.w.document.documentElement.dataset.scene,'ready');assert.equal(f.frames.size,1);
    f.tick(100);f.tick(150);assert.ok(f.rendered>1);
    f.w.dispatchEvent(new f.w.MessageEvent('message',{source:f.w,data:{type:'pelican-playback',paused:true}}));
    assert.equal(f.frames.size,0);const before=f.rendered;
    f.canvas.dispatchEvent(new f.w.KeyboardEvent('keydown',{key:'ArrowLeft',cancelable:true}));assert.ok(f.rendered>before);
    assert.equal(f.canvas.style.touchAction,'pan-y');
    f.w.dispatchEvent(new f.w.MessageEvent('message',{source:f.w,data:{type:'pelican-playback',paused:false}}));
    f.hidden(true);assert.equal(f.frames.size,0);f.hidden(false);assert.equal(f.frames.size,1);
    f.observers[0]([{isIntersecting:false}]);assert.equal(f.frames.size,0);
    f.observers[0]([{isIntersecting:true}]);assert.equal(f.frames.size,1);
    f.w.document.getElementById('coastRotate').click();assert.equal(f.w.document.getElementById('coastRotate').getAttribute('aria-pressed'),'true');
    for(const name of Object.keys(COASTAL_VIEWS)){
      f.w.document.querySelector(`[data-coast-view="${name}"]`).click();
      assert.equal(f.w.document.querySelector(`[data-coast-view="${name}"]`).getAttribute('aria-pressed'),'true');
      assert.equal(f.w.document.getElementById('coastRotate').getAttribute('aria-pressed'),'false');
    }
    f.viewer.dispose();assert.equal(f.frames.size,0);assert.equal(f.disposed,true);assert.equal(f.w.document.querySelector('canvas'),null);
  }finally{f.viewer.dispose();f.w.close();}
});
test('portrait scene starts on the cyclist rather than shrinking the whole street to unreadable miniatures',()=>{
  const f=fixture(false,320,600);
  try{
    assert.equal(f.w.document.querySelector('[data-coast-view="side"]').getAttribute('aria-pressed'),'true');
    f.w.document.querySelector('[data-coast-view="town"]').click();
    assert.equal(f.w.document.querySelector('[data-coast-view="town"]').getAttribute('aria-pressed'),'true');
  }finally{f.viewer.dispose();f.w.close();}
});
test('desktop also opens and resets to the close side view with neighborhood in the background',()=>{
  const f=fixture(false,900,540);
  try{
    assert.equal(f.w.document.querySelector('[data-coast-view="side"]').getAttribute('aria-pressed'),'true');
    f.w.document.querySelector('[data-coast-view="town"]').click();f.w.document.querySelector('#coastReset').click();
    assert.equal(f.w.document.querySelector('[data-coast-view="side"]').getAttribute('aria-pressed'),'true');
  }finally{f.viewer.dispose();f.w.close();}
});
test('WebGL failure reports honestly with disabled controls, not an empty fake rendering',()=>{
  const f=fixture(true);try{assert.equal(f.w.document.documentElement.dataset.scene,'error');
    assert.equal(f.frames.size,0);assert.ok([...f.w.document.querySelectorAll('button')].every(b=>b.disabled));
    assert.match(f.w.document.getElementById('coastalStatus').textContent,/WebGL/);
  }finally{f.viewer.dispose();f.w.close();}
});
