// Production-like URL/CSP fixture. Never proxies credentials or business traffic.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

export const siteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const homeCsp = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'";
export const sceneCsp = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'none'";
const loginSource = fs.readFileSync(path.join(siteRoot, 'login/index.html'), 'utf8');
const loginHashes = [...loginSource.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
  .map(match => `'sha256-${crypto.createHash('sha256').update(match[1]).digest('base64')}'`);
export const loginCsp = `default-src 'self'; script-src 'self' ${loginHashes.join(' ')}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'none'`;
const loginGuard = `window.fetch=async function(input){var path=new URL(String(input),location.href).pathname;return path==='/api/me'?new Response('{}',{status:401,headers:{'Content-Type':'application/json'}}):new Response(JSON.stringify({detail:'本地原版页面预览，未提交账号或密码。'}),{status:400,headers:{'Content-Type':'application/json'}})};`;
const types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8', '.ico': 'image/x-icon'};

export function createPreviewServer({degrade3d = false} = {}) {
  return http.createServer((request, response) => {
    if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); return response.end(); }
    let pathname;
    try { pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname); }
    catch { response.writeHead(400); return response.end(); }
    if (pathname === '/__preview__/login-guard.js') {
      response.writeHead(200, {'Content-Type': types['.js'], 'Cache-Control': 'no-store'});
      return response.end(request.method === 'HEAD' ? '' : loginGuard);
    }
    if (/^\/(api|v1|qoder|trae|codex)\//.test(pathname)) { response.writeHead(403); return response.end('No business services in preview'); }
    if (pathname === '/dashboard/') {
      response.writeHead(200, {'Content-Type': types['.html'], 'Cache-Control': 'no-store'});
      return response.end('<!doctype html><html lang="zh-CN"><title>智汇 · 入口验证</title><h1>控制台入口验证</h1><p>仅验证导航，不连接真实业务。</p></html>');
    }
    const login = ['/login', '/login/'].includes(pathname);
    const favicon = pathname.startsWith('/favicon/');
    const root = path.join(siteRoot, login ? 'login' : favicon ? 'favicon' : 'home');
    const relative = pathname === '/' || login ? 'index.html' : favicon ? pathname.slice('/favicon/'.length) : pathname.startsWith('/home-assets/') ? pathname.slice('/home-assets/'.length) : null;
    if (!relative || relative.includes('\\') || relative.includes('\0')) { response.writeHead(404); return response.end(); }
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep) || (degrade3d && /(?:gateway|sculpture)-viewer\.js$/.test(file))) { response.writeHead(404); return response.end(); }
    fs.stat(file, (error, stat) => {
      if (error || !stat.isFile()) { response.writeHead(404); return response.end(); }
      response.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('X-Content-Type-Options', 'nosniff');
      if (login || pathname === '/') response.setHeader('Content-Security-Policy', login ? loginCsp : homeCsp);
      if (/\/animations\/(?:task-workspace|pelican-ride)\.html$/.test(pathname)) {
        response.setHeader('X-Frame-Options', 'SAMEORIGIN');
        response.setHeader('Content-Security-Policy', sceneCsp);
      }
      if (login) response.setHeader('X-Frame-Options', 'DENY');
      response.writeHead(200);
      if (request.method === 'HEAD') return response.end();
      if (login) return response.end(loginSource.replace('<script>', '<script src="/__preview__/login-guard.js"></script><script>'));
      fs.createReadStream(file).pipe(response);
    });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const portIndex = process.argv.indexOf('--port');
  const port = portIndex >= 0 ? Number(process.argv[portIndex + 1]) : 4184;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port');
  createPreviewServer({degrade3d: process.argv.includes('--degrade-3d')}).listen(port, '127.0.0.1', () => console.log(`Local: http://127.0.0.1:${port}/`));
}
