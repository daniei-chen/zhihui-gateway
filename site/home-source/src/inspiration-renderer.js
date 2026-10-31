import {smokeRingFragmentShader} from '../vendor/paper-shaders/packages/shaders/src/shaders/smoke-ring.ts';
import {getShaderNoiseTexture} from '../vendor/paper-shaders/packages/shaders/src/get-shader-noise-texture.ts';

export const MAX_PIXELS = 360000;
export const PALETTE = [[.21,.49,.96,1], [.29,.69,.65,1], [.96,.72,.45,1], [.85,.92,.98,1]];
export const VERTEX_SHADER = `#version 300 es
in vec2 a_position;
uniform vec2 u_aspect;
uniform vec2 u_pointer;
out vec2 v_objectUV;
void main() {
  v_objectUV = a_position * u_aspect * .52 + u_pointer;
  gl_Position = vec4(a_position, 0., 1.);
}`;

export function renderSize(width, height) {
  const w = Math.max(1, Math.min(4096, Number.isFinite(width) ? width : 1));
  const h = Math.max(1, Math.min(4096, Number.isFinite(height) ? height : 1));
  const ratio = Math.min(1, Math.sqrt(MAX_PIXELS / (w * h)));
  return [Math.max(1, Math.floor(w * ratio)), Math.max(1, Math.floor(h * ratio))];
}

// Only the upstream fragment shader is vendored; no Three.js scene graph or library mount.
export function createInspirationRenderer(canvas) {
  const gl = canvas.getContext('webgl2', {alpha: false, antialias: false,
    depth: false, stencil: false, preserveDrawingBuffer: false, powerPreference: 'low-power'});
  if (!gl) throw Error('WebGL2 unavailable');
  let program, buffer, texture, image, disposed = false;
  const shaders = [];
  const uniform = {};
  function dispose() {
    if (disposed) return;
    disposed = true;
    if (image) { image.onload = null; image.onerror = null; }
    if (texture) gl.deleteTexture(texture);
    if (buffer) gl.deleteBuffer(buffer);
    if (program) gl.deleteProgram(program);
    for (const shader of shaders) gl.deleteShader(shader);
  }
  try {
    function compile(type, source) {
      const shader = gl.createShader(type);
      if (!shader) throw Error('Shader allocation failed');
      shaders.push(shader); gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw Error('Shader compilation failed');
      return shader;
    }
    program = gl.createProgram();
    if (!program) throw Error('Program allocation failed');
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX_SHADER));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, smokeRingFragmentShader));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error('Shader linking failed');
    gl.useProgram(program);
    buffer = gl.createBuffer(); texture = gl.createTexture();
    if (!buffer || !texture) throw Error('GPU allocation failed');
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
    const attribute = gl.getAttribLocation(program, 'a_position');
    gl.enableVertexAttribArray(attribute); gl.vertexAttribPointer(attribute, 2, gl.FLOAT, false, 0, 0);
    for (const key of ['time','aspect','pointer','colorBack','colors[0]','colorsCount',
      'thickness','radius','innerShape','noiseScale','noiseIterations','noiseTexture']) {
      uniform[key] = gl.getUniformLocation(program, 'u_' + key);
    }
    gl.uniform4fv(uniform.colorBack, [.985,.989,.995,1]);
    gl.uniform4fv(uniform['colors[0]'], PALETTE.flat());
    gl.uniform1f(uniform.colorsCount, PALETTE.length);
    gl.uniform1f(uniform.noiseScale, 1.5);
    gl.uniform1f(uniform.noiseIterations, 2);
    gl.uniform1i(uniform.noiseTexture, 0);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([127,127,127,255]));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
    image = getShaderNoiseTexture();
    return {
      ready: new Promise((resolve, reject) => {
        if (!image) return reject(Error('Noise source unavailable'));
        const load = () => {
          if (disposed) return;
          try {
            gl.bindTexture(gl.TEXTURE_2D, texture);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
            resolve();
          } catch { reject(Error('Noise texture unavailable')); }
        };
        image.onload = load; image.onerror = () => reject(Error('Noise texture unavailable'));
        if (image.complete && image.naturalWidth) load();
      }),
      render({width, height, time, pointer, mode}) {
        if (disposed) return;
        const [w,h] = renderSize(width,height);
        if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
        gl.viewport(0,0,w,h);
        gl.uniform2f(uniform.aspect, w / Math.min(w,h), h / Math.min(w,h));
        gl.uniform2f(uniform.pointer, ...pointer);
        gl.uniform1f(uniform.time, time);
        gl.uniform1f(uniform.radius, mode === 'open' ? .34 : mode === 'focus' ? .24 : .29);
        gl.uniform1f(uniform.thickness, mode === 'focus' ? .2 : .16);
        gl.uniform1f(uniform.innerShape, mode === 'focus' ? 1.15 : .85);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      },
      dispose,
    };
  } catch (error) { dispose(); throw error; }
}
