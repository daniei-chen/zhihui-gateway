import {build} from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import {gzipSync} from 'node:zlib';
const results=await Promise.all([
  build({entryPoints:['src/velo-viewer.js'],outfile:'public/scripts/velo-viewer.js',bundle:true,minify:true,
    format:'iife',target:'es2022',legalComments:'eof',metafile:true}),
  build({entryPoints:['src/velo-renderer.js'],outfile:'public/scripts/velo-renderer.js',bundle:true,minify:true,
    format:'iife',globalName:'zhVeloRenderer',target:'es2022',legalComments:'eof',metafile:true}),
]);
const files=results.flatMap(result=>Object.keys(result.metafile.outputs));
const rows=files.map(file=>{const raw=fs.readFileSync(file);return {file,bytes:raw.length,gzip:gzipSync(raw,{level:9}).length};});
const total=rows.reduce((sum,row)=>sum+row.gzip,0);
if(total>200000)throw Error(`Scene and Three.js exceed 200KB gzip: ${total}`);
for(const name of ['LICENSE','PROVENANCE.json']){
  const target=path.join('public/vendor/pelican-velo',name);fs.mkdirSync(path.dirname(target),{recursive:true});
  fs.copyFileSync('vendor/pelican-velo/'+name,target);
}
fs.copyFileSync('scenes/velo-coast.html','public/animations/velo-coast.html');
console.log(JSON.stringify({files:rows,total_gzip:total,includes_three:true,network_assets:0,production_deployed:false},null,2));
