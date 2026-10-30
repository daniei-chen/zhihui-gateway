"""Inspect actual mesh geometry and compile scene shaders in standalone OpenGL.

This is deliberately not a browser screenshot or evidence of browser/mobile QA.
"""
import argparse
import json
import math
from pathlib import Path

import moderngl
import numpy as np
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument('geometry', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--yaw', type=float, default=None)
parser.add_argument('--subject', choices=['patrick','squidward'], default=None)
parser.add_argument('--view', choices=['town', 'side', 'front', 'homes', 'restaurant'], default='town')
parser.add_argument('--width', type=int, default=1260)
parser.add_argument('--height', type=int, default=900)
args = parser.parse_args()
if not (240 <= args.width <= 2560 and 240 <= args.height <= 2560):
    parser.error('Diagnostic dimensions must be between 240 and 2560')
data = json.loads(args.geometry.read_text(encoding='utf-8'))
ctx = moderngl.create_standalone_context()
width, height = args.width, args.height
preset = data['views'][args.view]
position, yaw, pitch = preset['target'], preset['yaw'], preset['pitch']
span, vertical, depth = preset['width'], preset['height'], preset['depth']
if args.subject:
    position = list(data['subjects']['Patrick Star' if args.subject=='patrick' else 'Squidward Tentacles'])
    position[1] = .76 if args.subject=='patrick' else 1.04
    span, vertical, depth, pitch = 2.0, 2.35, .2, .07
if args.yaw is not None:
    yaw = args.yaw
radius = max(vertical/2, span/(2*(width/height))) / math.tan(math.radians(35)/2) + depth
target = np.array(position)
eye = target + np.array([math.sin(yaw)*math.cos(pitch)*radius, math.sin(pitch)*radius, math.cos(yaw)*math.cos(pitch)*radius])


def unit(v):
    return v / np.linalg.norm(v)


forward = unit(target-eye)
side = unit(np.cross(forward, [0, 1, 0]))
up = np.cross(side, forward)
view = np.eye(4)
view[:3, :3] = [side, up, -forward]
view[:3, 3] = -view[:3, :3]@eye
f, near, far = 1/math.tan(math.radians(35)/2), .1, 300
projection = np.array([[f/(width/height), 0, 0, 0], [0, f, 0, 0],
                       [0, 0, (far+near)/(near-far), 2*far*near/(near-far)], [0, 0, -1, 0]])
vertex = '''#version 330
uniform mat4 mvp; in vec3 position;in vec3 normal;out vec3 n;out vec3 world;
void main(){n=normal;world=position;gl_Position=mvp*vec4(position,1.);}
'''
program = ctx.program(vertex_shader=vertex, fragment_shader='''#version 330
in vec3 n;in vec3 world;uniform vec3 albedo;uniform vec3 eye;uniform float opacity;uniform bool basic;
out vec4 color;
vec3 aces(vec3 v){return clamp((v*(2.51*v+.03))/(v*(2.43*v+.59)+.14),0.,1.);}
void main(){vec3 normal=normalize(n);float key=max(dot(normal,normalize(vec3(-7.,12.,8.))),0.);
float fill=max(dot(normal,normalize(vec3(3.,3.,6.))),0.);
vec3 light=vec3(.50,.53,.49)+key*vec3(.55,.50,.39)+fill*vec3(.32,.36,.35);
vec3 lit=basic?albedo:aces(albedo*light*1.05);
float fog=1.-exp(-pow(length(world-eye)*.008,2.));
lit=mix(lit,pow(vec3(.70,.85,.86),vec3(2.2)),fog);
color=vec4(pow(lit,vec3(1./2.2)),opacity);}
''')
program['mvp'].write((projection@view).astype('f4').T.tobytes())
program['eye'].value = tuple(eye)
framebuffer = ctx.simple_framebuffer((width, height), components=4)
framebuffer.use()
framebuffer.clear(.7, .8, .85, 1)

# Render the exact sky fragment shader with camera-derived rays. Compile the
# Three vertex shader too; final colors here are not Three's browser pipeline.
sky = data['sky']


def fragment(source):
    return '#version 330\nout vec4 pixel;\n' + source.replace('varying ', 'in ').replace('gl_FragColor', 'pixel').replace(
        '#include <colorspace_fragment>', 'pixel.rgb=pow(max(pixel.rgb,vec3(0.)),vec3(1./2.2));').replace(
        '#include <tonemapping_fragment>', '')


ctx.program(vertex_shader='#version 330\nuniform mat4 projectionMatrix;uniform mat4 viewMatrix;in vec3 position;\n'
            + sky['vertex'].replace('varying ', 'out '), fragment_shader=fragment(sky['fragment'])).release()
sky_program = ctx.program(vertex_shader='''#version 330
in vec2 position;out vec3 ray;uniform vec3 forward;uniform vec3 side;uniform vec3 up;uniform vec2 tangent;
void main(){ray=forward+side*position.x*tangent.x+up*position.y*tangent.y;gl_Position=vec4(position,0.,1.);}
''', fragment_shader=fragment(sky['fragment']))
sky_program['tangent'].value = (math.tan(math.radians(35)/2)*width/height, math.tan(math.radians(35)/2))
for name, value in [('forward', forward), ('side', side), ('up', up)]:
    sky_program[name].value = tuple(value)
for name, value in sky['uniforms'].items():
    sky_program[name].value = tuple(value)
buffer = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1], dtype='f4').tobytes())
ctx.vertex_array(sky_program, [(buffer, '2f', 'position')]).render()
ctx.enable(moderngl.DEPTH_TEST | moderngl.BLEND)
ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
shader_programs = []
for mesh in sorted(data['meshes'], key=lambda m: m['opacity'], reverse=True):
    if mesh.get('side', 0) == 2:
        ctx.disable(moderngl.CULL_FACE)
    else:
        ctx.enable(moderngl.CULL_FACE)
        ctx.cull_face = 'front' if mesh.get('side', 0) == 1 else 'back'
    values = np.hstack([np.array(mesh['positions']).reshape(-1, 3), np.array(mesh['normals']).reshape(-1, 3)]).astype('f4')
    if mesh['shader']:
        custom = ctx.program(vertex_shader='#version 330\nuniform mat4 modelMatrix;uniform mat4 viewMatrix;uniform mat4 projectionMatrix;in vec3 position;\n'
                             + mesh['vertex'].replace('varying ', 'out '),
                             fragment_shader=fragment('uniform vec3 cameraPosition;\n'+mesh['fragment']))
        # Geometry is already transformed; compile the exact vertex contract,
        # then render world-space positions without re-applying the ocean tilt.
        custom.release()
        custom = ctx.program(vertex_shader='''#version 330
uniform mat4 mvp;in vec3 position;out vec3 world;
void main(){world=position;gl_Position=mvp*vec4(position,1.);}
''', fragment_shader=fragment('uniform vec3 cameraPosition;\n'+mesh['fragment']))
        custom['mvp'].write((projection@view).astype('f4').T.tobytes())
        custom['cameraPosition'].value = tuple(eye)
        for name, value in mesh['uniforms'].items():
            custom[name].value = tuple(value) if isinstance(value, list) else value
        vbo = ctx.buffer(values[:, :3].tobytes())
        vao = ctx.vertex_array(custom, [(vbo, '3f', 'position')])
        shader_programs.append(custom)
    else:
        color = mesh['color']
        rgb = np.array([(color>>16)&255, (color>>8)&255, color&255])/255
        program['albedo'].value = tuple(np.power(rgb, 2.2))
        program['opacity'].value = mesh['opacity']
        program['basic'].value = mesh['basic']
        vbo = ctx.buffer(values.tobytes())
        vao = ctx.vertex_array(program, [(vbo, '3f 3f', 'position', 'normal')])
    vao.render()
    vao.release()
    vbo.release()
image = Image.frombytes('RGBA', (width, height), framebuffer.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
image.convert('RGB').save(args.output, quality=94)
ctx.release()
print(json.dumps({'standalone_geometry_render': str(args.output), 'size': [width, height],
                  'sky_and_ocean_glsl_compiled': True, 'browser_qa': False}))
