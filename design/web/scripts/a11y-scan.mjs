#!/usr/bin/env node
/**
 * K38 可访问性静态扫描：找出**没有可访问名称的交互控件**。
 *
 * 为什么用脚本而不是全靠人工过一遍：`<Button><RefreshCw /></Button>` 这种纯图标
 * 按钮在视觉上很清楚，读屏软件只会念「按钮」；而它是不是问题，取决于有没有
 * `aria-label`/`title`/隐藏文本。仓里有 200+ 个按钮，人眼看会漏，脚本不会。
 *
 * 扫描的是一个**启发式**子集（宁可漏报也不误报成灾）：
 *   · 只查交互元素：button / a / Link / 常见触发器（TabsTrigger、SelectTrigger…）；
 *   · 「可访问名称」的判定：aria-label、aria-labelledby、title、或子节点里有
 *     可见文本（JSX 表达式里的图标不算文本）；
 *   · 子节点只有图标/表达式 → 报 `icon-only-no-label`；
 *   · 元素是自闭合且没有 aria-label/title → 报 `self-closing-no-label`。
 *
 * 用法：
 *   node design/web/scripts/a11y-scan.mjs [--root <web 目录>] [--json] [--strict]
 * 默认 root 为 `src/three-platform/web`（相对仓库根）。`--strict` 时有发现即退出码 1。
 *
 * 已知局限（写出来，避免把「扫描通过」当成无障碍验收通过）：
 *   · 不判断 label 是否**有意义**（`aria-label="x"` 也通过）；
 *   · 不覆盖自定义组件（如 `<CopyButton>` 内部实现），需要人工看组件实现；
 *   · 不检查对比度、焦点顺序、读屏语义——那些需要真实浏览器 + axe。
 */
import {readdirSync, readFileSync, statSync} from 'node:fs';
import {join, relative, resolve, sep} from 'node:path';
import {pathToFileURL} from 'node:url';

const INTERACTIVE = new Set([
  // 原生
  'button',
  'a',
  // 本项目的 Button 组件与常见触发器/菜单项
  'Button',
  'Link',
  'SelectTrigger',
  'TabsTrigger',
  'Toggle',
  'ToggleGroupItem',
  'Switch',
  'Checkbox',
  'DialogTrigger',
  'AlertDialogTrigger',
  'DropdownMenuTrigger',
  'PopoverTrigger',
  'TooltipTrigger',
  'AccordionTrigger',
  'SheetTrigger',
  'DrawerTrigger',
  'MenuItem',
]);

/** 有可访问名称的显式属性 */
const NAME_ATTRS = ['aria-label', 'aria-labelledby', 'title=', 'aria-describedby'];

export function scanSource(source, file = '<inline>') {
  const findings = [];
  const lines = source.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const open = /<([A-Za-z][\w.]*)(\s|>|\/|$)/.exec(line);
    if (!open) continue;
    const tag = open[1];
    if (!INTERACTIVE.has(tag)) continue;

    // 把整段开标签拼出来（可能跨多行）；最多看 20 行，避免大 JSX 拖死
    let head = line;
    let j = i;
    while (!/\/?>/.test(head.replace(/\{[\s\S]*?\}/g, '')) && j < Math.min(lines.length - 1, i + 20)) {
      j += 1;
      head += `\n${lines[j]}`;
    }
    const selfClosing = /\/>\s*$/.test(head.trim().split('\n').pop()?.trim() ?? '');
    const hasName = NAME_ATTRS.some((attr) => head.includes(attr));
    if (hasName) continue;

    /**
     * 表单控件（Switch/Checkbox）的可访问名称通常来自 `<label htmlFor="id">`。
     * 只要同一个文件里有对应的 `htmlFor`，就不算缺名称——这是扫描器最容易
     * 误报的一类，漏掉会把「按 label 关联」的正确写法报成问题。
     */
    if (tag === 'Switch' || tag === 'Checkbox') {
      const idMatch = /\bid="([^"]+)"/.exec(head);
      if (idMatch && source.includes(`htmlFor="${idMatch[1]}"`)) continue;
    }

    if (selfClosing) {
      findings.push({file, line: i + 1, tag, reason: 'self-closing-no-label'});
      continue;
    }

    // 收集子节点：从开标签结束后到同名闭合标签（不处理嵌套同名，够用）
    const afterOpen = head.slice(head.indexOf('>') + 1);
    let body = afterOpen;
    if (!body.includes(`</${tag}>`)) {
      for (let k = j + 1; k < Math.min(lines.length, j + 40); k += 1) {
        body += `\n${lines[k]}`;
        if (body.includes(`</${tag}>`)) break;
      }
    }
    const inner = body.slice(0, body.indexOf(`</${tag}>`) === -1 ? undefined : body.indexOf(`</${tag}>`));
    /**
     * 去掉标签以后还剩什么？
     *   · 剩空 → 子节点全是元素（图标），没有可见文本 → 需要 aria-label；
     *   · 剩 `{...}` 表达式 → 可能是 `{t('save')}`／`{label}` 这类**文本**，
     *     宁可放过也不误报（误报会让扫描结果被无视）；表达式里的图标几乎都写成
     *     `<Icon />`，已经被上面的去标签吃掉了。
     */
    const text = inner
      .replace(/<[^>]*>/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (text.length === 0) {
      findings.push({file, line: i + 1, tag, reason: 'icon-only-no-label'});
    }
  }
  return findings;
}

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === '.next' || name === 'out') continue;
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) walk(full, acc);
    else if (name.endsWith('.tsx') || name.endsWith('.jsx')) acc.push(full);
  }
  return acc;
}

export function scanDir(root) {
  const files = walk(root);
  const findings = [];
  for (const file of files) {
    findings.push(...scanSource(readFileSync(file, 'utf8'), file));
  }
  return findings;
}

function main() {
  const args = process.argv.slice(2);
  const json = args.includes('--json');
  const strict = args.includes('--strict');
  const rootIdx = args.indexOf('--root');
  const repoRoot = resolve(process.cwd());
  const root = rootIdx >= 0 ? resolve(args[rootIdx + 1]) : join(repoRoot, 'src/three-platform/web');
  let findings;
  try {
    findings = scanDir(root);
  } catch (e) {
    console.error(`扫描失败：${e.message}`);
    process.exit(2);
  }
  if (json) {
    console.log(JSON.stringify({root, count: findings.length, findings}, null, 2));
  } else {
    console.log(`root: ${root}`);
    console.log(`发现 ${findings.length} 处交互元素缺少可访问名称：`);
    for (const f of findings) {
      const rel = relative(repoRoot, f.file).split(sep).join('/');
      console.log(`  ${rel}:${f.line}  <${f.tag}>  ${f.reason}`);
    }
  }
  process.exit(strict && findings.length > 0 ? 1 : 0);
}

// 作为脚本直接运行时才扫描（被测试 import 时不执行）
const invokedDirectly =
  !!process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (invokedDirectly) main();
