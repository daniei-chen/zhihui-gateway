import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
fs.cpSync(path.join(root, 'public'), path.join(root, 'dist'), { recursive: true });
console.log('Static build ready in dist/. No deployment performed.');
