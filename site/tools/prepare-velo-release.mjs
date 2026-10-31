// Prepare a new immutable static candidate. Never modifies production or prior revisions.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {simplifyHome,simplifyController} from './simplify-home.mjs';
const site=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const destination=process.argv[2];
if(!destination||!path.isAbsolute(destination)||fs.existsSync(destination))throw Error('New absolute destination required');
const manifest=JSON.parse(fs.readFileSync(path.join(site,'home-import.json'),'utf8'));
const prefix='home/'+manifest.asset_prefix.slice('/home-assets/'.length);
const hash=raw=>crypto.createHash('sha256').update(raw).digest('hex');
const removed=[],retained=[];
for(const row of manifest.files){
  const source=path.resolve(site,row.path);
  if(!source.startsWith(site+path.sep))throw Error('Unsafe manifest path');
  let raw=fs.readFileSync(source);
  if(hash(raw)!==row.sha256)throw Error('Source drift: '+row.path);
  const name=row.path==='home/index.html'?'index.html':row.path.startsWith(prefix)?row.path.slice(prefix.length):null;
  if(!name||name.startsWith('/')||name.includes('..'))throw Error('Unexpected manifest member');
  if(/^(scripts\/(coastal-viewer|inspiration-viewer)\.js|animations\/(pelican-ride|inspiration-flow)\.html|animations\/inspiration-poster\.webp|vendor\/paper-shaders\/)/.test(name)){
    removed.push(name);continue;
  }
  if(/\.(html|js|css|json|webmanifest)$/.test(name))raw=Buffer.from(raw.toString('utf8').replaceAll(manifest.asset_prefix,'/'));
  if(name==='index.html')raw=Buffer.from(simplifyHome(raw.toString('utf8')));
  if(name==='scripts/upgrade.js')raw=Buffer.from(simplifyController(raw.toString('utf8')));
  const target=path.join(destination,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,raw);retained.push(name);
}
for(const name of ['scripts/velo-viewer.js','scripts/velo-renderer.js','animations/velo-coast.html',
  'animations/velo-coast.webp','vendor/pelican-velo/LICENSE','vendor/pelican-velo/PROVENANCE.json']){
  const target=path.join(destination,name);fs.mkdirSync(path.dirname(target),{recursive:true});
  fs.copyFileSync(path.join(site,'home-source/public',name),target);
}
fs.copyFileSync(path.join(site,'home-source/scenes/color-review.css'),path.join(destination,'styles/color-release.css'));
fs.copyFileSync(path.join(site,'home-source/scenes/ride-controller.js'),path.join(destination,'scripts/ride.js'));
const html=fs.readFileSync(path.join(destination,'index.html'),'utf8');
if(/id="console"|#console|data-open-workspace|inspiration-flow|pelican-ride\.html|海绵宝宝|比奇堡/.test(html))throw Error('Retired content still active');
if((html.match(/class="public-console-frame"/g)||[]).length!==1)throw Error('Hero preview changed');
console.log(JSON.stringify({retained:retained.length,removed,destination,production_contacted:false}));
