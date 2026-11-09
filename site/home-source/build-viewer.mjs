import { build } from 'esbuild';
import { mkdir, copyFile } from 'node:fs/promises';
await mkdir('public/vendor/draco', { recursive: true });
await build({ entryPoints: ['src/sculpture-viewer.js'], outfile: 'public/scripts/sculpture-viewer.js', bundle: true, minify: true, format: 'esm', target: 'es2022', legalComments: 'eof' });
await build({ entryPoints: ['src/gateway-viewer.js'], outfile: 'public/scripts/gateway-viewer.js', bundle: true, minify: true, format: 'esm', target: 'es2022', legalComments: 'eof' });
for (const name of ['draco_wasm_wrapper.js', 'draco_decoder.wasm', 'draco_decoder.js']) {
  await copyFile(`node_modules/three/examples/jsm/libs/draco/gltf/${name}`, `public/vendor/draco/${name}`);
}
await copyFile('node_modules/three/LICENSE', 'public/vendor/THREE-LICENSE.txt');
await copyFile('licenses/DRACO-LICENSE.txt', 'public/vendor/draco/LICENSE.txt');
console.log('Bundled local Three.js viewer and Draco decoder.');
