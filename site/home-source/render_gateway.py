"""Render the actual gateway geometry with standalone OpenGL, not a browser."""
from pathlib import Path
import json
import math
import numpy as np
from PIL import Image
import moderngl

ROOT = Path(__file__).resolve().parent
meshes = json.loads((ROOT / 'reference-login/gateway-geometry.json').read_text())
ctx = moderngl.create_standalone_context()
program = ctx.program(vertex_shader='''
#version 330
uniform mat4 mvp;
in vec3 position;
in vec3 normal;
out vec3 n;
out vec3 world;
void main(){n=normal;world=position;gl_Position=mvp*vec4(position,1.0);}
''', fragment_shader='''
#version 330
in vec3 n;
in vec3 world;
uniform vec3 albedo;
uniform float opacity;
out vec4 color;
void main(){
vec3 normal=normalize(n);
float key=max(dot(normal,normalize(vec3(-3.8,6.5,4.5))),0.);
float fill=max(dot(normal,normalize(vec3(4,3,-2))),0.);
vec3 v=normalize(vec3(4.2,3,7.8)-world);
float spec=pow(max(dot(normal,normalize(normalize(vec3(-3.8,6.5,4.5))+v)),0.),48.);
vec3 light=vec3(.43)+key*.65+fill*vec3(.22,.29,.45);
color=vec4(pow(albedo*light+spec*.13,vec3(1./2.2)),opacity);
}
''')


def unit(v):
    return v / np.linalg.norm(v)


width,height=1100,1000
eye=np.array([4.2,3.0,7.8]); target=np.array([0,1.6,0])
forward=unit(target-eye); side=unit(np.cross(forward,[0,1,0])); up=np.cross(side,forward)
view=np.eye(4); view[:3,:3]=[side,up,-forward]; view[:3,3]=-view[:3,:3]@eye
f=1/math.tan(math.radians(34)/2); near,far=.1,60
projection=np.array([[f/(width/height),0,0,0],[0,f,0,0],[0,0,(far+near)/(near-far),2*far*near/(near-far)],[0,0,-1,0]])
program['mvp'].write((projection@view).astype('f4').T.tobytes())
framebuffer=ctx.simple_framebuffer((width,height),components=4)
framebuffer.use(); framebuffer.clear(0,0,0,0)
ctx.enable(moderngl.DEPTH_TEST | moderngl.BLEND)
ctx.blend_func=moderngl.SRC_ALPHA,moderngl.ONE_MINUS_SRC_ALPHA
for mesh in sorted(meshes,key=lambda m:m['opacity'],reverse=True):
    values=np.hstack([np.array(mesh['positions']).reshape(-1,3),np.array(mesh['normals']).reshape(-1,3)]).astype('f4')
    buffer=ctx.buffer(values.tobytes()); vao=ctx.vertex_array(program,[(buffer,'3f 3f','position','normal')])
    color=mesh['color']; channels=np.array([(color>>16)&255,(color>>8)&255,color&255])/255
    program['albedo'].value=tuple(np.power(channels,2.2)); program['opacity'].value=mesh['opacity']
    vao.render(); vao.release(); buffer.release()
image=Image.frombytes('RGBA',(width,height),framebuffer.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
image.save(ROOT/'public/models/gateway-poster.webp',quality=94)
evidence=ROOT/'evidence/login';evidence.mkdir(exist_ok=True,parents=True)
background=Image.new('RGBA',(width,height),'#edf5ff');background.alpha_composite(image)
background.convert('RGB').save(evidence/'gateway-geometry-preview.jpg',quality=94)
print('Gateway poster and independent geometry preview rendered; not webpage QA.')
