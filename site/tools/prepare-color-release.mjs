// Materialize the approved local design without its local server or credentials.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {reviewHtml} from './color-review-preview.mjs';

const site = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export function productionHtml(source, demoPrefix) {
  if (!/^\/home-assets\/console-[a-z0-9-]+$/.test(demoPrefix)) throw Error('Fixed versioned demo prefix required');
  const html = reviewHtml(source)
    .replaceAll('href="http://127.0.0.1:4189/dashboard/"', 'href="/dashboard/"')
    .replace(/<iframe class="review-console-frame" src="http:\/\/127\.0\.0\.1:4189\/([a-z-]+)\/" title="([^"]+)" loading="lazy"><\/iframe>/g,
      (_, route, title) => `<iframe class="public-console-frame" src="${demoPrefix}/${route}/" title="${title.replace('本地合成数据', '公开只读示例') }" sandbox="allow-scripts" referrerpolicy="no-referrer" loading="lazy"></iframe>`)
    .replaceAll('/review-palette.css', '/styles/color-release.css')
    .replaceAll('/review-paths.js', '/scripts/connected-paths.js')
    .replaceAll('.review-console-frame', '.public-console-frame')
    .replace(/<div class="review-badge">[\s\S]*?<\/div>/, '')
    .replace('控制台原生渲染 · 合成数据', '控制台界面 · 只读示例');
  if (/127\.0\.0\.1|localhost|本地待审/.test(html)) throw Error('Local review reference escaped');
  return html;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [destination, demoPrefix] = process.argv.slice(2);
  if (!destination || !path.isAbsolute(destination)) throw Error('New absolute output directory required');
  if (fs.existsSync(destination)) throw Error('Output exists; inspect instead of overwriting');
  fs.cpSync(path.join(site, 'home-source/public'), destination, {recursive: true});
  const filename = path.join(destination, 'index.html');
  fs.writeFileSync(filename, productionHtml(fs.readFileSync(filename, 'utf8'), demoPrefix));
  fs.copyFileSync(path.join(site, 'home-source/scenes/color-review.css'), path.join(destination, 'styles/color-release.css'));
  fs.copyFileSync(path.join(site, 'home-source/scenes/connected-paths.js'), path.join(destination, 'scripts/connected-paths.js'));
  console.log('Prepared approved design for immutable import; no upload.');
}
