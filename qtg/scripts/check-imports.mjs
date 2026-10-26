import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2];
let errors = 0;
const files = [];
function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.js')) files.push(p);
  }
}
walk(root);

const exportsOf = new Map();
const importsOf = new Map();

function parseExports(src) {
  const names = new Set();
  const re = /export\s+(?:(?:async\s+)?function\s+([A-Za-z0-9_$]+)|(?:const|let|var)\s+([A-Za-z0-9_$]+)|class\s+([A-Za-z0-9_$]+))/g;
  let m;
  while ((m = re.exec(src))) names.add(m[1] || m[2] || m[3]);
  if (/export\s+default/.test(src)) names.add('default');
  const re2 = /export\s*\{([^}]+)\}/g;
  while ((m = re2.exec(src))) for (const part of m[1].split(',')) {
    const n = part.trim().split(/\s+as\s+/).pop().trim();
    if (n) names.add(n);
  }
  return names;
}

for (const f of files) {
  const src = fs.readFileSync(f, 'utf8');
  exportsOf.set(path.resolve(f), parseExports(src));
  const imps = [];
  const re = /import\s+(?:([^'";]+?)\s+from\s+)?['"](\.[^'"]+)['"]/g;
  let m;
  while ((m = re.exec(src))) imps.push({ clause: (m[1] || '').trim(), spec: m[2] });
  importsOf.set(path.resolve(f), imps);
}

for (const [file, imps] of importsOf) {
  for (const imp of imps) {
    const target = path.resolve(path.dirname(file), imp.spec);
    if (!fs.existsSync(target)) {
      console.error(`MISSING MODULE: ${path.relative(root, file)} -> ${imp.spec}`);
      errors++;
      continue;
    }
    const ex = exportsOf.get(target) || new Set();
    const clause = imp.clause;
    if (!clause) continue;
    const named = clause.match(/\{([^}]*)\}/);
    const def = clause.startsWith('*') ? '' : clause.replace(/\{[^}]*\}/, '').replace(/,/g, '').trim();
    if (def && !ex.has('default')) {
      console.error(`MISSING DEFAULT EXPORT: ${path.relative(root, file)} imports '${def}' from ${imp.spec}`);
      errors++;
    }
    if (named) {
      for (const part of named[1].split(',')) {
        const n = part.trim().split(/\s+as\s+/)[0].trim();
        if (!n) continue;
        if (!ex.has(n)) {
          console.error(`MISSING EXPORT '${n}': ${path.relative(root, file)} <- ${imp.spec}`);
          errors++;
        }
      }
    }
  }
}
console.log(`checked ${files.length} modules, ${errors} error(s)`);
process.exit(errors ? 1 : 0);
