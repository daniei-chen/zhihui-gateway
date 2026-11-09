"""Render actual geometry and shaders offline, not browser QA or an FPS benchmark."""
import argparse
import json
import math
from pathlib import Path
import moderngl
import numpy as np
from PIL import Image

p=argparse.ArgumentParser()
p.add_argument('geometry',type=Path);p.add_argument('output',type=Path)
p.add_argument('--view',choices=['coast','side','front'],default='coast')
p.add_argument('--width',type=int,default=1000);p.add_argument('--height',type=int,default=800)
a=p.parse_args()
if not (240<=a.width<=1600 and 240<=a.height<=1600):p.error('bounded dimensions required')
data=json.loads(a.geometry.read_text(encoding='utf-8'))
ctx=moderngl.create_standalone_context()
def unit(v):return np.array(v)/np.linalg.norm(v)
def look(eye,target):
    f=unit(target-eye);s=unit(np.cross(f,[0,1,0]));u=np.cross(s,f)
    m=np.eye(4);m[:3,:3]=[s,u,-f];m[:3,3]=-m[:3,:3]@eye
    return m,f,s,u
v=data['views'][a.view];aspect=a.width/a.height
r=v['radius']*max(1,.95/aspect);yaw=v['yaw'];pitch=v['pitch']
target=np.array([0,1.55,0]);eye=target+np.array([math.sin(yaw)*math.cos(pitch)*r,math.sin(pitch)*r,math.cos(yaw)*math.cos(pitch)*r])
view,forward,side,up=look(eye,target)
f=1/math.tan(math.radians(38)/2);n,z=.1,260
proj=np.array([[f/aspect,0,0,0],[0,f,0,0],[0,0,(z+n)/(n-z),2*z*n/(n-z)],[0,0,-1,0]])
fbo=ctx.simple_framebuffer((a.width,a.height),components=4);fbo.use();fbo.clear(.8,.7,.6,1)
def frag(text):return '#version 330\nout vec4 pixel;\n'+text.replace('precision highp float;','').replace('varying ','in ').replace('gl_FragColor','pixel').replace(
    '#include <tonemapping_fragment>','pixel.rgb=clamp((pixel.rgb*(2.51*pixel.rgb+.03))/(pixel.rgb*(2.43*pixel.rgb+.59)+.14),0.,1.);').replace(
    '#include <colorspace_fragment>','pixel.rgb=pow(max(pixel.rgb,vec3(0.)),vec3(1./2.2));')
def apply(program,values):
    for name,value in values.items():
        if name in program:program[name].value=tuple(value) if isinstance(value,list) else value
sky=data['sky'];fragment=frag(sky['fragment']).replace('in vec3 vDir;','in vec3 ray;').replace('normalize(vDir)','normalize(ray)')
program=ctx.program(vertex_shader='''#version 330
in vec2 position;out vec3 ray;uniform vec3 forward;uniform vec3 side;uniform vec3 up;uniform vec2 tangent;
void main(){ray=forward+side*position.x*tangent.x+up*position.y*tangent.y;gl_Position=vec4(position,0.,1.);}
''',fragment_shader=fragment)
apply(program,sky['uniforms'])
program['tangent'].value=(1/f*aspect,1/f)
for key,value in [('forward',forward),('side',side),('up',up)]:program[key].value=tuple(value)
buffer=ctx.buffer(np.array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1],dtype='f4').tobytes())
ctx.vertex_array(program,[(buffer,'2f','position')]).render()
ctx.enable(moderngl.DEPTH_TEST|moderngl.BLEND);ctx.blend_func=moderngl.SRC_ALPHA,moderngl.ONE_MINUS_SRC_ALPHA
lit=ctx.program(vertex_shader='''#version 330
uniform mat4 mvp;in vec3 position;in vec3 normal;out vec3 N;out vec3 world;
void main(){N=normal;world=position;gl_Position=mvp*vec4(position,1.);}
''',fragment_shader='''#version 330
in vec3 N;in vec3 world;uniform vec3 albedo;uniform float opacity;out vec4 color;
vec3 aces(vec3 v){return clamp((v*(2.51*v+.03))/(v*(2.43*v+.59)+.14),0.,1.);}
void main(){vec3 n=normalize(N);float sun=max(dot(n,normalize(vec3(-4.,7.,-3.))),0.);
float fill=max(dot(n,normalize(vec3(3.,4.,6.))),0.);
vec3 light=mix(vec3(.20,.32,.36),vec3(.8,.76,.64),n.y*.5+.5)+sun*vec3(1.1,.85,.56)+fill*.5;
color=vec4(pow(aces(albedo*light),vec3(1./2.2)),opacity);}
''')
lit['mvp'].write((proj@view).astype('f4').T.tobytes())
for mesh in data['meshes']:
    if mesh['side']==2:ctx.disable(moderngl.CULL_FACE)
    else:ctx.enable(moderngl.CULL_FACE);ctx.cull_face='back'
    pos=np.array(mesh['positions'],dtype='f4').reshape(-1,3)
    if mesh['shader']:
        ocean=ctx.program(vertex_shader='''#version 330
uniform mat4 mvp;in vec3 position;out vec3 vWorldPos;out vec3 vNormalW;out float vHeight;out vec2 vGrid;
void main(){vWorldPos=position;vNormalW=vec3(0.,1.,0.);vHeight=0.;vGrid=position.xz;gl_Position=mvp*vec4(position,1.);}
''',fragment_shader=frag('uniform vec3 cameraPosition;\n'+mesh['fragment']))
        ocean['mvp'].write((proj@view).astype('f4').T.tobytes());ocean['cameraPosition'].value=tuple(eye)
        apply(ocean,mesh['uniforms']);b=ctx.buffer(pos.tobytes());vao=ctx.vertex_array(ocean,[(b,'3f','position')])
    else:
        normals=np.array(mesh['normals'],dtype='f4').reshape(-1,3) if mesh['normals'] else np.tile([0,1,0],(len(pos),1))
        b=ctx.buffer(np.hstack([pos,normals]).astype('f4').tobytes());vao=ctx.vertex_array(lit,[(b,'3f 3f','position','normal')])
        lit['albedo'].value=tuple(mesh['color']);lit['opacity'].value=mesh['opacity']
    vao.render(mode=moderngl.LINES if mesh['lines'] else moderngl.TRIANGLES);vao.release();b.release()
image=Image.frombytes('RGBA',(a.width,a.height),fbo.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
a.output.parent.mkdir(parents=True,exist_ok=True);image.convert('RGB').save(a.output,quality=88)
ctx.release();print(json.dumps({'output':str(a.output),'geometry':True,'browser_qa':False,'shadows':'not reproduced in offline inspector'}))
