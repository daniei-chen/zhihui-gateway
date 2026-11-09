// Verify the installed homepage source closure before making only the reviewed scoped edits.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {simplifyHome,simplifyController} from './simplify-home.mjs';

const site = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const destination = process.argv[2];
if (!destination || !path.isAbsolute(destination) || fs.existsSync(destination)) throw Error('New absolute output directory required');
const manifest = JSON.parse(fs.readFileSync(path.join(site,'home-import.json'),'utf8'));
const sourcePrefix = 'home/' + manifest.asset_prefix.slice('/home-assets/'.length);
const hash = raw => crypto.createHash('sha256').update(raw).digest('hex');
const retained = [], removed = [];
for (const row of manifest.files) {
  const source = path.resolve(site,row.path);
  if (!source.startsWith(site+path.sep)) throw Error('Unsafe manifest source');
  let raw = fs.readFileSync(source);
  if (hash(raw)!==row.sha256) throw Error('Source drift: '+row.path);
  const name = row.path==='home/index.html' ? 'index.html' : row.path.startsWith(sourcePrefix) ? row.path.slice(sourcePrefix.length) : null;
  if (!name || name.startsWith('/') || name.includes('..')) throw Error('Unexpected source path');
  if (['scripts/coastal-viewer.js','animations/pelican-ride.html'].includes(name)) { removed.push(name);continue; }
  if (/\.(html|js|css|json|webmanifest)$/.test(name)) raw = Buffer.from(raw.toString('utf8').replaceAll(manifest.asset_prefix,'/'));
  if (name==='index.html') raw = Buffer.from(simplifyHome(raw.toString('utf8')));
  if (name==='scripts/upgrade.js') raw = Buffer.from(simplifyController(raw.toString('utf8')));
  const target = path.join(destination,name);
  fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,raw);
  retained.push(name);
}
for (const name of ['scripts/inspiration-viewer.js','animations/inspiration-flow.html','animations/inspiration-poster.webp',
  'vendor/paper-shaders/LICENSE','vendor/paper-shaders/NOTICE','vendor/paper-shaders/PROVENANCE.json']) {
  const target=path.join(destination,name);fs.mkdirSync(path.dirname(target),{recursive:true});
  fs.copyFileSync(path.join(site,'home-source/public',name),target);
}
fs.copyFileSync(path.join(site,'home-source/scenes/color-review.css'),path.join(destination,'styles/color-release.css'));
fs.copyFileSync(path.join(site,'home-source/scenes/ride-controller.js'),path.join(destination,'scripts/ride.js'));
const html=fs.readFileSync(path.join(destination,'index.html'),'utf8');
if (/id="console"|#console|data-open-workspace|coastal-viewer|pelican-ride\.html|比奇堡|海绵宝宝/.test(html)) throw Error('Retired scene/workspace still active');
if ((html.match(/class="public-console-frame"/g)||[]).length!==1 || !html.includes('/home-assets/console-flow-20261030-1/dashboard/')) throw Error('Hero preview changed');
console.log(JSON.stringify({source_tree:manifest.source_tree_sha256,retained:retained.length,removed,production_contacted:false,destination}));
