"""Render the selected upstream shader offline; this is not browser QA."""
import argparse
import base64
import io
import json
from pathlib import Path
import re
import subprocess

import moderngl
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'public/animations/inspiration-poster.webp')
    args = parser.parse_args()
    source = json.loads(subprocess.check_output([args.node, 'export-inspiration-shader.mjs'], cwd=ROOT))
    ctx = moderngl.create_standalone_context()
    vertex = source['vertex'].replace('#version 300 es', '#version 330')
    fragment = source['fragment'].replace('#version 300 es', '#version 330').replace('precision mediump float;', '')
    program = ctx.program(vertex_shader=vertex, fragment_shader=fragment)
    data = np.array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1], dtype='f4')
    buffer = ctx.buffer(data.tobytes())
    vao = ctx.vertex_array(program, [(buffer, '2f', 'a_position')])
    noise_source = (ROOT / 'vendor/paper-shaders/packages/shaders/src/get-shader-noise-texture.ts').read_text()
    png = re.search(r'data:image/png;base64,([^\']+)', noise_source)[1]
    noise = Image.open(io.BytesIO(base64.b64decode(png))).convert('RGBA')
    texture = ctx.texture(noise.size, 4, noise.tobytes())
    texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
    texture.repeat_x = texture.repeat_y = True
    texture.use(0)
    values = {'u_time': 3.96, 'u_pointer': (0,0), 'u_aspect': (1.2,1),
              'u_colorBack': (.985,.989,.995,1), 'u_colorsCount': 4,
              'u_noiseScale': 1.5, 'u_noiseIterations': 2,
              'u_radius': .29, 'u_thickness': .16, 'u_innerShape': .85, 'u_noiseTexture': 0}
    for key, value in values.items():
        program[key].value = value
    program['u_colors'].write(np.array(source['palette'] + [[0,0,0,0]]*6, dtype='f4').tobytes())
    framebuffer = ctx.simple_framebuffer((960,800), components=4)
    framebuffer.use()
    vao.render(moderngl.TRIANGLES)
    image = Image.frombytes('RGBA', framebuffer.size, framebuffer.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert('RGB')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output, 'WEBP', quality=88, method=6)
    print(json.dumps({'output': str(args.output), 'bytes': args.output.stat().st_size,
                      'renderer': ctx.info['GL_RENDERER'], 'browser_screenshot': False}))
    framebuffer.release(); texture.release(); vao.release(); buffer.release(); program.release(); ctx.release()


if __name__ == '__main__':
    main()
