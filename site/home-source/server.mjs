import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const base = path.join(path.dirname(fileURLToPath(import.meta.url)), 'public');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const port = Number(option('--port', process.env.PORT || '4173'));
const host = option('--host', '127.0.0.1');
const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8', '.ico': 'image/x-icon' };
http.createServer((request, response) => {
  if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); return response.end(); }
  let pathname;
  try { pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname); }
  catch { response.writeHead(400); return response.end(); }
  const pagePath = pathname === '/' ? '/index.html' : ['/login','/login/'].includes(pathname) ? '/login/index.html' : pathname;
  const file = path.resolve(base, '.' + pagePath);
  if (!file.startsWith(base + path.sep)) { response.writeHead(403); return response.end(); }
  fs.stat(file, (error, stat) => {
    if (error || !stat.isFile()) { response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }); return response.end('Not found'); }
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    if (request.method === 'HEAD') return response.end();
    fs.createReadStream(file).pipe(response);
  });
}).listen(port, host, () => console.log(`Local: http://${host}:${port}/`));
