import {build} from 'esbuild';
await build({entryPoints: ['src/coastal-viewer.js'], outfile: 'public/scripts/coastal-viewer.js',
  bundle: true, minify: true, format: 'iife', target: 'es2022', legalComments: 'eof'});
console.log('Bundled original coastal 3D scene. No network assets or deployment.');
