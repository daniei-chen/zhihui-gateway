import * as THREE from 'three';

// Iris/pupil patches follow the actual ellipsoid instead of floating on a
// tangent plane. The same surface remains connected when viewed in profile.
export function surfacePatch({mesh, parent, center, radii, material, name,
  longitude = 0, latitude = 0, angles = [.4, .4], lift = .002}) {
  const normal = new THREE.Vector3(Math.sin(longitude) * Math.cos(latitude), Math.sin(latitude), Math.cos(longitude) * Math.cos(latitude));
  const horizontal = new THREE.Vector3(Math.cos(longitude), 0, -Math.sin(longitude));
  const vertical = normal.clone().cross(horizontal).normalize();
  const positions = normal.toArray().map((value, axis) => center[axis] + value * (radii[axis] + lift));
  const indices = [], rings = 4, sides = 16;
  for (let ring = 1; ring <= rings; ring++) for (let side = 0; side < sides; side++) {
    const angle = side / sides * Math.PI * 2, distance = ring / rings;
    const direction = normal.clone().addScaledVector(horizontal, Math.tan(angles[0]) * Math.cos(angle) * distance)
      .addScaledVector(vertical, Math.tan(angles[1]) * Math.sin(angle) * distance).normalize();
    positions.push(...direction.toArray().map((value, axis) => center[axis] + value * (radii[axis] + lift)));
    const n = 1 + (ring - 1) * sides + side, next = 1 + (ring - 1) * sides + (side + 1) % sides;
    if (ring === 1) indices.push(0,n,next);
    if (ring < rings) indices.push(n,n+sides,next,next,n+sides,next+sides);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setIndex(indices); geometry.computeVertexNormals();
  return mesh(geometry, material, null, parent, name);
}

export function characterEye({mesh, egg, parent, center, radii, iris = 'pupil',
  white = 'white', gaze = 0, name = 'Integrated rounded eye'}) {
  egg(center, radii, white, parent, name);
  surfacePatch({mesh, parent, center, radii, material: iris, longitude: gaze, angles: [.39, .43], name: iris === 'iris' ? 'Blue iris' : 'Curved iris'});
  if (iris !== 'pupil') surfacePatch({mesh, parent, center, radii, material: 'pupil', longitude: gaze,
    angles: [.19, .26], lift: .004, name: 'Surface-bound pupil'});
  surfacePatch({mesh, parent, center, radii, material: 'white', longitude: gaze - .12,
    latitude: .19, angles: [.065, .08], lift: .006, name: 'Eye catchlight'});
}
