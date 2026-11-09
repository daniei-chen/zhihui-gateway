import * as THREE from 'three';
import fs from 'node:fs';
import path from 'node:path';
import {createVeloScene,VIEWS} from './src/velo-lite/model.js';
const destination=process.argv[2];
if(!destination||!path.isAbsolute(destination))throw Error('Absolute diagnostic output required');
const model=createVeloScene(),meshes=[];
model.update(.06,.6);model.scene.updateMatrixWorld(true);
const uniforms=material=>Object.fromEntries(Object.entries(material.uniforms).map(([key,{value}])=>
  [key,value?.toArray?value.toArray():value]));
model.scene.traverse(child=>{
  if((!child.isMesh&&!child.isLineSegments)||child===model.sky.mesh)return;
  const count=child.isInstancedMesh?child.count:1;
  for(let i=0;i<count;i++){
    const geometry=child.geometry.index?child.geometry.toNonIndexed():child.geometry.clone();
    const matrix=child.matrixWorld.clone();
    const color=child.material.color?.clone();
    if(child.isInstancedMesh){const local=new THREE.Matrix4();child.getMatrixAt(i,local);matrix.multiply(local);
      if(child.instanceColor&&color){const tint=new THREE.Color();child.getColorAt(i,tint);color.multiply(tint);}}
    geometry.applyMatrix4(matrix);
    const material=child.material;
    meshes.push({positions:Array.from(geometry.attributes.position.array),normals:geometry.attributes.normal?Array.from(geometry.attributes.normal.array):null,
      color:color?.toArray()||null,opacity:material.opacity,side:material.side,lines:!!child.isLineSegments,
      shadow:child.castShadow,basic:!!material.isMeshBasicMaterial,
      shader:!!material.isShaderMaterial,vertex:material.vertexShader,fragment:material.fragmentShader,
      uniforms:material.isShaderMaterial?uniforms(material):null});geometry.dispose();
  }
});
const triangles=meshes.filter(m=>!m.lines).reduce((sum,m)=>sum+m.positions.length/9,0);
fs.mkdirSync(path.dirname(destination),{recursive:true});
fs.writeFileSync(destination,JSON.stringify({meshes,views:VIEWS,sky:{vertex:model.sky.mesh.material.vertexShader,
  fragment:model.sky.mesh.material.fragmentShader,uniforms:uniforms(model.sky.mesh.material)},triangles}));
console.log(JSON.stringify({meshes:meshes.length,triangles,browser_qa:false,destination}));model.dispose();
