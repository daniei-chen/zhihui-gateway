import { createGatewayModel } from './src/gateway-model.js';
import { mkdir,writeFile } from 'node:fs/promises';
const model=createGatewayModel();
const meshes=[];
model.group.updateMatrixWorld(true);
model.group.traverse(child => {
  if (!child.isMesh) return;
  const geometry=child.geometry.index ? child.geometry.toNonIndexed() : child.geometry.clone();
  geometry.applyMatrix4(child.matrixWorld);
  const material=child.material;
  meshes.push({name:child.name,positions:Array.from(geometry.attributes.position.array),normals:Array.from(geometry.attributes.normal.array),color:material.color.getHex(),opacity:material.opacity ?? 1});
  geometry.dispose();
});
await mkdir('reference-login',{recursive:true});
await writeFile('reference-login/gateway-geometry.json',JSON.stringify(meshes));
console.log('Exported gateway geometry for standalone model rendering.');
