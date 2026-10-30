"""Standalone OpenGL geometry preview, not a browser or webpage screenshot."""
from pathlib import Path
import math
import numpy as np
from PIL import Image, ImageDraw
import moderngl

ROOT = Path(__file__).resolve().parent
evidence = ROOT / 'evidence/sculpture'
evidence.mkdir(parents=True, exist_ok=True)
asset = np.load(ROOT / 'reference-models/thinker-normalized.npz')
ctx = moderngl.create_standalone_context()
program = ctx.program(vertex_shader='''
#version 330
uniform mat4 mvp;
in vec3 in_position;
in vec3 in_normal;
out vec3 normal;
out vec3 world;
void main(){normal=in_normal;world=in_position;gl_Position=mvp*vec4(in_position,1.0);}
''', fragment_shader='''
#version 330
in vec3 normal;
in vec3 world;
uniform vec3 camera;
out vec4 color;
void main(){
 vec3 n=normalize(normal), v=normalize(camera-world);
 vec3 key=normalize(vec3(-3.5,6,5));
 vec3 fill=normalize(vec3(4,2,1));
 vec3 rim=normalize(vec3(1,4,-4));
 float a=.19+.13*(n.y*.5+.5);
 float diffuse=.70*max(dot(n,key),0.)+.12*max(dot(n,fill),0.)+.19*max(dot(n,rim),0.);
 float spec=.09*pow(max(dot(n,normalize(key+v)),0.),24.);
 vec3 linear=vec3(.955,.948,.922)*(a+diffuse)+spec;
 color=vec4(pow(linear,vec3(1./2.2)),1.);
}
''')
vbo = ctx.buffer(np.hstack([asset['vertices'], asset['normals']]).astype('f4').tobytes())
ibo = ctx.buffer(asset['faces'].tobytes())
vao = ctx.vertex_array(program, [(vbo, '3f 3f', 'in_position', 'in_normal')], ibo, index_element_size=4)
ctx.enable(moderngl.DEPTH_TEST | moderngl.CULL_FACE)


def normalize(vector):
    return vector / np.linalg.norm(vector)


def render(angle=0, width=1000, height=1160, detail=False):
    target = np.array([0, 1.77, .05] if detail else [0, 1.20, 0])
    eye = np.array([1.65, 2.16, 2.45] if detail else [2.95, 2.13, 4.5])
    theta = math.radians(angle)
    rotation = np.array([[math.cos(theta), 0, math.sin(theta)], [0, 1, 0], [-math.sin(theta), 0, math.cos(theta)]])
    eye = target + rotation @ (eye-target)
    forward = normalize(target-eye)
    side = normalize(np.cross(forward, [0, 1, 0]))
    up = np.cross(side, forward)
    view = np.eye(4)
    view[:3, :3] = np.array([side, up, -forward])
    view[:3, 3] = -view[:3, :3] @ eye
    f = 1 / math.tan(math.radians(34)/2)
    near, far = .05, 40
    projection = np.array([[f/(width/height),0,0,0],[0,f,0,0],[0,0,(far+near)/(near-far),2*far*near/(near-far)],[0,0,-1,0]])
    framebuffer = ctx.simple_framebuffer((width, height), components=4)
    framebuffer.use()
    framebuffer.clear(247/255, 247/255, 243/255, 1)
    program['mvp'].write((projection @ view).astype('f4').T.tobytes())
    program['camera'].value = tuple(eye)
    vao.render()
    image = Image.frombytes('RGBA', (width,height), framebuffer.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert('RGB')
    framebuffer.release()
    return image


views = []
for angle in [0, 90, 180, 270]:
    image = render(angle, 600, 696)
    ImageDraw.Draw(image).text((18, 18), f'OpenGL scan preview / angle {angle}', fill='#77776e')
    image.save(evidence / f'geometry-{angle}.jpg', quality=93)
    views.append(image)
sheet = Image.new('RGB', (1200,1392), '#f7f7f3')
for index,image in enumerate(views):
    sheet.paste(image, ((index%2)*600,(index//2)*696))
sheet.save(evidence / 'geometry-contact-sheet.jpg', quality=93)
render().save(ROOT / 'public/models/thinker-poster.webp', quality=90)
render(detail=True).save(evidence / 'geometry-detail.jpg', quality=94)
print('Standalone OpenGL model renders created; these are not browser QA evidence.')
