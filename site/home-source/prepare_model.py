"""Normalize the verified scan without simplifying or posing the mesh."""
from pathlib import Path
import hashlib
import json
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'reference-models/thinker-source.stl'
TARGET = ROOT / 'public/models'
TARGET.mkdir(exist_ok=True)
data = SOURCE.read_bytes()
if hashlib.sha1(data).hexdigest() != '486307df3882858a36cbb5812beaf5f881b800c7':
    raise SystemExit('Source scan does not match the Wikimedia file checksum.')
mesh = trimesh.load(SOURCE, force='mesh')
if not mesh.is_watertight or not mesh.is_winding_consistent:
    raise SystemExit('Expected a closed mesh with consistent winding.')
if not np.isfinite(mesh.vertices).all() or not mesh.nondegenerate_faces().all():
    raise SystemExit('Invalid geometry in source scan.')
source_bounds = mesh.bounds.copy()
mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
mesh.apply_scale(2.4 / mesh.extents[1])
mesh.apply_translation([-mesh.bounds.mean(axis=0)[0], -mesh.bounds[0, 1], -mesh.bounds.mean(axis=0)[2]])
mesh.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
    name='Warm white plaster', baseColorFactor=[250, 249, 245, 255], metallicFactor=0, roughnessFactor=.84))
(ROOT / 'reference-models/thinker-normalized.glb').write_bytes(mesh.export(file_type='glb', include_normals=True))
np.savez_compressed(ROOT / 'reference-models/thinker-normalized.npz',
                    vertices=np.asarray(mesh.vertices, dtype='f4'),
                    normals=np.asarray(mesh.vertex_normals, dtype='f4'),
                    faces=np.asarray(mesh.faces, dtype='u4'))
metadata = {
    'title': 'The Thinker', 'sculptor': 'Auguste Rodin', 'scanAuthor': 'Scan the World',
    'sourcePage': 'https://commons.wikimedia.org/wiki/File:Scan_the_World_-_The_Thinker_(Auguste_Rodin).stl',
    'sourceSHA1': hashlib.sha1(data).hexdigest(), 'sourceSHA256': hashlib.sha256(data).hexdigest(),
    'license': 'CC-BY-SA-4.0', 'licenseURL': 'https://creativecommons.org/licenses/by-sa/4.0/',
    'sourceBytes': len(data), 'triangles': len(mesh.faces), 'vertices': len(mesh.vertices),
    'sourceBounds': source_bounds.tolist(), 'bounds': mesh.bounds.tolist(),
    'watertight': bool(mesh.is_watertight), 'consistentWinding': bool(mesh.is_winding_consistent),
    'degenerateFaces': int((~mesh.nondegenerate_faces()).sum()),
    'changes': ['Rigid axis rotation, translation and uniform scale to height 2.4.',
                'Smooth vertex normals and warm white display material.',
                'Draco compression, position quantization 18 bits; no face decimation or pose edits.'],
    'validationLimits': 'Topology checks do not prove absence of all self intersections or scan defects. Browser visual QA remains separate.'
}
(TARGET / 'thinker-manifest.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
print(f'Normalized {len(mesh.faces):,} triangles / {len(mesh.vertices):,} vertices; original pose retained.')
