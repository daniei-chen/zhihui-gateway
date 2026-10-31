// Reduced scene inspired by Pelican / Velo (MIT); no island, audio or post-processing.
import * as THREE from 'three';
import {mergeGeometries} from 'three/addons/utils/BufferGeometryUtils.js';
import {createBike} from './bike.js';
import {createPelican} from './pelican.js';
import {makeSky} from './sky.js';
import {makeOcean} from './ocean.js';

function batchStatic(group, animated) {
  for (const child of [...group.children]) if (child.isGroup) batchStatic(child,animated);
  const buckets = new Map();
  for (const child of [...group.children]) {
    if (!child.isMesh || child.children.length || animated.has(child) || Array.isArray(child.material)) continue;
    const key = child.material;
    if (!buckets.has(key)) buckets.set(key,[]);
    buckets.get(key).push(child);
  }
  for (const [material,meshes] of buckets) {
    if (meshes.length < 2) continue;
    const geometries = meshes.map(mesh => {
      mesh.updateMatrix();
      const geometry = mesh.geometry.index ? mesh.geometry.toNonIndexed() : mesh.geometry.clone();
      geometry.applyMatrix4(mesh.matrix);
      for (const key of Object.keys(geometry.attributes)) if (!['position','normal','uv'].includes(key)) geometry.deleteAttribute(key);
      return geometry;
    });
    const geometry = mergeGeometries(geometries,false);
    geometries.forEach(item=>item.dispose());
    if (!geometry) throw Error('Static geometry attributes differ');
    const merged = new THREE.Mesh(geometry,material);
    merged.castShadow = meshes.some(mesh=>mesh.castShadow);
    merged.receiveShadow = meshes.some(mesh=>mesh.receiveShadow);
    merged.name = 'batched-detail';
    meshes.forEach(mesh=>group.remove(mesh));
    group.add(merged);
  }
}

export const VIEWS = Object.freeze({
  coast: {yaw: .70, pitch:.16, radius:6.2},
  side: {yaw: .03, pitch:.15, radius:6.0},
  front: {yaw:1.20, pitch:.16, radius:6.1},
});

export function createVeloScene() {
  const scene = new THREE.Scene();
  const bike = createBike(), bird = createPelican();
  scene.add(bike.group,bird.root);
  bird.root.name = 'partridge';
  const sky = makeSky(), ocean = makeOcean(scene);
  scene.add(sky.mesh);
  const sunDirection = new THREE.Vector3(-.65,.12,-.65).normalize();
  sky.uniforms.uSunDir.value.copy(sunDirection);
  ocean.uniforms.uSunDir.value.copy(sunDirection);
  scene.add(new THREE.HemisphereLight(0xffedcf,0x42666d,2.0));
  const sun = new THREE.DirectionalLight(0xffd5a1,3.0);
  sun.position.set(-4,7,-3);sun.castShadow=true;
  sun.shadow.mapSize.set(512,512);
  sun.shadow.camera.left=-3;sun.shadow.camera.right=3;
  sun.shadow.camera.top=4;sun.shadow.camera.bottom=-1;
  sun.shadow.camera.near=.5;sun.shadow.camera.far=18;sun.shadow.normalBias=.025;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xe3eff3,1.3);fill.position.set(3,4,6);scene.add(fill);

  const deck = new THREE.Group();scene.add(deck);
  const wood = new THREE.MeshStandardMaterial({color:0x9f7959,roughness:.95});
  const edge = new THREE.MeshStandardMaterial({color:0x725f4a,roughness:.9});
  const plankGeometry = new THREE.BoxGeometry(.58,.12,4);
  const planks = new THREE.InstancedMesh(plankGeometry,wood,48);
  const dummy = new THREE.Object3D();
  for(let i=0;i<48;i++){
    dummy.position.set((i-24)*.6,-.07,0);dummy.updateMatrix();planks.setMatrixAt(i,dummy.matrix);
    planks.setColorAt(i,new THREE.Color().setHSL(.088,.22,.40+(i%5)*.015));
  }
  planks.receiveShadow=true;deck.add(planks);
  const postGeo = new THREE.CylinderGeometry(.035,.045,1.05,6);
  for(let i=-5;i<=5;i++) {
    const post = new THREE.Mesh(postGeo,edge);post.position.set(i*2.6,.45,-1.8);deck.add(post);
  }
  const rail = new THREE.Mesh(new THREE.BoxGeometry(29,.065,.065),edge);
  rail.position.set(0,.95,-1.8);deck.add(rail);
  const lowerRail=rail.clone();lowerRail.position.y=.48;deck.add(lowerRail);
  const flowers = [0xf0b58e,0xe49b8b,0xf3d79e].map(color=>new THREE.MeshStandardMaterial({color,roughness:.8}));
  const leafMat = new THREE.MeshStandardMaterial({color:0x5d8061,roughness:.9});
  for(let i=0;i<5;i++){
    const flower = new THREE.Group();flower.name='basket-flower';
    flower.position.set((i%3-1)*.075,.16+(i%2)*.07,(i%2-.5)*.12);
    const stem = new THREE.Mesh(new THREE.CylinderGeometry(.006,.006,.30,5),leafMat);
    stem.position.y=.11;flower.add(stem);
    for(let petal=0;petal<5;petal++){
      const mesh=new THREE.Mesh(new THREE.SphereGeometry(.035,7,5),flowers[i%flowers.length]);
      mesh.position.set(Math.cos(petal*Math.PI*.4)*.035,.27+Math.sin(petal*Math.PI*.4)*.035,0);
      mesh.scale.z=.5;flower.add(mesh);
    }
    bike.basket.add(flower);
  }
  const ownedGeometries=new Set(),ownedMaterials=new Set();
  scene.traverse(object=>{
    if(object.geometry)ownedGeometries.add(object.geometry);
    if(object.material)for(const material of [object.material].flat())ownedMaterials.add(material);
  });
  const animated = new Set([...bike.animated,...bird.animated]);
  batchStatic(bike.group,animated);batchStatic(bird.root,animated);batchStatic(deck,animated);
  let distance=0;
  function update(dt,time) {
    distance+=dt*1.8;
    bike.update(dt,1.8,distance,0);bird.update(dt,time,1.8,bike.getPedals(),0);
    sky.uniforms.uTime.value=time;ocean.uniforms.uTime.value=time;
    // Only periodic planks wrap; moving the railing would visibly jump.
    planks.position.x=-(distance%.6);
  }
  update(0,0);
  function dispose() {
    const geometries=new Set(ownedGeometries), materials=new Set(ownedMaterials),textures=new Set();
    scene.traverse(object=>{
      if(object.geometry)geometries.add(object.geometry);
      if(object.material) for(const material of [object.material].flat())materials.add(material);
    });
    for(const material of materials) for(const value of Object.values(material)) if(value?.isTexture)textures.add(value);
    textures.forEach(t=>t.dispose());geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());
    sun.shadow.dispose();scene.clear();
  }
  return {scene,bike,bird,sky,ocean,sun,deck,planks,update,dispose};
}
