import {writeFile} from 'node:fs/promises';
import path from 'node:path';
import {createCoastalModel,createCoastalSky} from './src/coastal-model.js';
import {COASTAL_VIEWS} from './src/coastal-camera.js';

const destination=process.argv[2];
if(!destination||!path.isAbsolute(destination))throw Error('Pass an absolute evidence JSON path.');
const model=createCoastalModel(),atmosphere=createCoastalSky(),meshes=[];
model.update(.6);model.group.updateMatrixWorld(true);
model.group.traverse(child=>{
  if(!child.isMesh)return;
  const instances=child.isInstancedMesh?child.count:1;
  for(let i=0;i<instances;i++){
  const geometry=child.geometry.index?child.geometry.toNonIndexed():child.geometry.clone();
  const transform=child.matrixWorld.clone();
  if(child.isInstancedMesh){const local=new child.matrixWorld.constructor();child.getMatrixAt(i,local);transform.multiply(local);}
  geometry.applyMatrix4(transform);
  const material=child.material;
  meshes.push({name:child.name,positions:Array.from(geometry.attributes.position.array),
    normals:Array.from(geometry.attributes.normal.array),color:material.color?.getHex()??null,
    basic:!!material.isMeshBasicMaterial,shader:material.isShaderMaterial?'ocean':null,
    vertex:material.isShaderMaterial?material.vertexShader:null,fragment:material.isShaderMaterial?material.fragmentShader:null,
    opacity:material.opacity,side:material.side,uniforms:material.isShaderMaterial?Object.fromEntries(Object.entries(material.uniforms).map(([k,u])=>[k,u.value?.isColor?u.value.toArray():u.value])):null});
  geometry.dispose();}
});
const subjects=Object.fromEntries(['Patrick Star','Squidward Tentacles'].map(name=>[name,model.neighborhood.landmarks[name].position.toArray()]));
await writeFile(destination,JSON.stringify({meshes,views:COASTAL_VIEWS,subjects,sky:{vertex:atmosphere.sky.material.vertexShader,
  fragment:atmosphere.sky.material.fragmentShader,uniforms:Object.fromEntries(Object.entries(atmosphere.sky.material.uniforms)
    .map(([k,u])=>[k,u.value.toArray()]))}}));
model.dispose();atmosphere.dispose();
console.log('Exported actual scene geometry/shaders for standalone inspection, not browser QA.');
