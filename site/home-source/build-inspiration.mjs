import {build} from 'esbuild';
import fs from 'node:fs';
import {gzipSync} from 'node:zlib';

await build({entryPoints:['src/inspiration-viewer.js'], outfile:'public/scripts/inspiration-viewer.js',
  bundle:true, minify:true, format:'iife', target:'es2022', legalComments:'eof'});
fs.copyFileSync('scenes/inspiration-flow.html', 'public/animations/inspiration-flow.html');
for (const name of ['LICENSE','NOTICE','PROVENANCE.json']) {
  fs.mkdirSync('public/vendor/paper-shaders', {recursive:true});
  fs.copyFileSync('vendor/paper-shaders/' + name, 'public/vendor/paper-shaders/' + name);
}
const bytes = fs.readFileSync('public/scripts/inspiration-viewer.js');
if (bytes.length > 55000 || gzipSync(bytes).length > 28000) throw Error('Inspiration bundle exceeded budget');
console.log(JSON.stringify({bundle_bytes:bytes.length,gzip_bytes:gzipSync(bytes).length,network_assets:0,draw_calls_per_frame:1,max_pixels:360000,max_fps:24}));
