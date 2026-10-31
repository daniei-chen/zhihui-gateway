// Scoped, idempotent edit shared by the authored preview and imported homepage.
import {fileURLToPath} from 'node:url';
import path from 'node:path';
export function simplifyHome(html) {
  // Reuse the sculpted mark beside the kicker, not between the CTA and preview.
  const mark = html.match(/<svg\b[^>]*class="prism-float"[\s\S]*?<\/svg>/);
  if (mark) {
    html = html.replace(mark[0], '');
    html = html.replace(/<p class="hero-kicker">([\s\S]*?)<\/p>/,
      (_, text) => `<div class="hero-signature">${mark[0].replace('class="prism-float"', 'class="hero-signature-mark"')}<p class="hero-kicker">${text}</p></div>`);
  }
  html = html.replace(/<svg\b[^>]*class="hero-beam"[\s\S]*?<\/svg>/, '');
  html = html.replace(/<section class="console-section"[^>]*id="console"[\s\S]*?<\/section>/, '');
  html = html.replaceAll('href="#console"', 'href="#preview"');
  html = html.replace('>工作台预览</a>', '>首屏工作台</a>');
  html = html.replace('09 / GET CONNECTED', '08 / GET CONNECTED');
  html = html.replace(/<button type="button" data-open-workspace="(models|usage)">[\s\S]*?<\/button>/g,
    (_, key) => `<a class="story-link" href="/${key === 'usage' ? 'stats' : 'models'}/">${key === 'usage' ? '查看用量' : '查看模型'} <span aria-hidden="true">↗</span></a>`);
  html = html.replace(/(<iframe\b[^>]*id="pelicanRide"[^>]*src=")[^"]+("[^>]*title=")[^"]+("[^>]*>)/,
    '$1/animations/velo-coast.html$2海岸夕照：鹧鸪骑着带花篮的复古单车，可拖动旋转$3');
  html = html.replace(/<iframe\b[^>]*id="pelicanRide"[^>]*>/, frame => {
    frame=frame.replace('tabindex="-1"','tabindex="0"');
    return frame.includes('loading=')?frame:frame.replace('<iframe','<iframe loading="lazy"');
  });
  html = html.replace('03 / KEEP YOUR FLOW', '03 / IDEAS IN CONFLUENCE')
    .replace('03 / A LITTLE JOY, A LITTLE AI', '03 / IDEAS IN CONFLUENCE')
    .replace('复杂的留给智汇，<br>专注的留给你。', '灵感相遇，<br>创造发生。')
    .replace('带着好奇出发，<br>把快乐做出来。', '灵感相遇，<br>创造发生。')
    .replace('就像顺着海风骑行。<br>方向清楚，每一步都更从容。', '让工具接住灵感，<br>让想法变成作品。')
    .replace('像海绵宝宝一样，<br>在 AI 的世界，创造一点快乐。', '让工具接住灵感，<br>让想法变成作品。')
    .replace('把分散的工具与配置收在一处，从选择入口到查看用量，少一点来回切换，多一点连续的思考。', '不同的工具，各展所长。你只管好奇，享受创造。')
    .replace('让工具接住灵感，让想法变成作品。认真做事，也别忘了享受创造。', '不同的工具，各展所长。你只管好奇，享受创造。')
    .replace('比奇堡的一天 · 拖动旋转', '灵感汇流 · 轻触形态')
    .replace('海风里的鹈鹕', '灵感汇流 · 轻触形态')
    .replaceAll('暂停骑行', '暂停流动').replaceAll('播放骑行', '播放流动');
  html = html.replace(/(<section[^>]+id="ride-preview"[\s\S]*?<\/section>)/, section => section
    .replace(/03 \/ [A-Z ,]+(?=<\/div>)/,'03 / A LITTLE JOY IN MOTION')
    .replace(/(<h2 id="ride-title">)[\s\S]*?(<\/h2>)/,'$1向着海风，<br>轻快创造。$2')
    .replace(/(<p class="story-lead">)[\s\S]*?(<\/p>)/,'$1让工具接住灵感，<br>把快乐，做进作品。$2')
    .replace(/(<p class="story-detail">)[\s\S]*?(<\/p>)/,'$1不同的工具，各展所长。少一点来回，多一点创造。$2')
    .replace(/(<div class="scene-controls"><span>)[\s\S]*?(<\/span>)/,'$1海岸骑行 · 拖动旋转$2')
    .replaceAll('暂停流动','暂停骑行').replaceAll('播放流动','播放骑行'));
  return html;
}

export function simplifyController(script) {
  const start = script.indexOf('  const workspaceCopy =');
  if (start < 0) return script;
  const end = script.indexOf("  all('.faq-question')", start);
  if (end < 0) throw Error('Workspace controller boundary drift');
  return script.slice(0,start) + script.slice(end);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  process.stdout.write(simplifyHome(input));
}
