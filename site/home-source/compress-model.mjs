import { NodeIO } from '@gltf-transform/core';
import { KHRDracoMeshCompression } from '@gltf-transform/extensions';
import draco3d from 'draco3dgltf';
import { stat, readFile, writeFile } from 'node:fs/promises';
const io = new NodeIO().registerExtensions([KHRDracoMeshCompression]).registerDependencies({
  'draco3d.encoder': await draco3d.createEncoderModule(),
  'draco3d.decoder': await draco3d.createDecoderModule(),
});
const document = await io.read('reference-models/thinker-normalized.glb');
const source = document.getRoot().listMeshes()[0].listPrimitives()[0];
const sourcePositions = source.getAttribute('POSITION').getArray().slice();
const sourceIndices = source.getIndices().getArray().slice();
document.createExtension(KHRDracoMeshCompression).setRequired(true).setEncoderOptions({
  method: KHRDracoMeshCompression.EncoderMethod.SEQUENTIAL,
  encodeSpeed: 4, decodeSpeed: 6,
  quantizationBits: { POSITION: 18, NORMAL: 12, TEX_COORD: 14, COLOR: 8, GENERIC: 12 },
});
Object.assign(document.getRoot().getAsset(), { generator: 'Zhihui local scan adaptation', copyright: 'Scan the World, CC BY-SA 4.0; normalized and rendered in white. See ATTRIBUTION.txt.' });
await io.write('public/models/thinker.glb', document);
const decoded = await io.read('public/models/thinker.glb');
const mesh = decoded.getRoot().listMeshes()[0].listPrimitives()[0];
const manifest = JSON.parse(await readFile('public/models/thinker-manifest.json', 'utf8'));
if (mesh.getIndices().getCount() / 3 !== manifest.triangles || mesh.getAttribute('POSITION').getCount() !== manifest.vertices) throw Error('Compression changed mesh counts.');
const positions = mesh.getAttribute('POSITION').getArray(), indices = mesh.getIndices().getArray();
let maxPositionError = 0;
for (let i = 0; i < sourcePositions.length; i++) maxPositionError = Math.max(maxPositionError, Math.abs(sourcePositions[i] - positions[i]));
if (maxPositionError >= .00002 || !indices.every((value, index) => value === sourceIndices[index])) throw Error('Compression failed geometry preservation checks.');
manifest.compressedBytes = (await stat('public/models/thinker.glb')).size;
manifest.decodedTriangles = mesh.getIndices().getCount() / 3;
manifest.decodedVertices = mesh.getAttribute('POSITION').getCount();
manifest.maxPositionError = maxPositionError;
manifest.positionErrorRelativeToHeight = maxPositionError / 2.4;
manifest.indexOrderPreserved = true;
await writeFile('public/models/thinker-manifest.json', JSON.stringify(manifest, null, 2) + '\n');
console.log(`Compressed scan: ${(manifest.compressedBytes / 1048576).toFixed(2)} MiB; all ${manifest.triangles} faces retained.`);
