// Export the exact bundled GLSL for an offline render, not a browser screenshot.
import {build} from 'esbuild';
import {execFileSync} from 'node:child_process';
const result = await build({stdin:{contents:`
  import {smokeRingFragmentShader as fragment} from './vendor/paper-shaders/packages/shaders/src/shaders/smoke-ring.ts';
  import {VERTEX_SHADER as vertex, PALETTE as palette} from './src/inspiration-renderer.js';
  console.log(JSON.stringify({fragment,vertex,palette}));`, resolveDir:process.cwd()},
  bundle:true, write:false, platform:'node', format:'cjs'});
process.stdout.write(execFileSync(process.execPath, ['-e', result.outputFiles[0].text]));
