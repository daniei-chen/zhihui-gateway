import * as THREE from 'three';

function roundedPath(path, width, height, radius) {
  const x = -width / 2, y = -height / 2;
  path.moveTo(x + radius, y); path.lineTo(x + width - radius, y);
  path.quadraticCurveTo(x + width, y, x + width, y + radius);
  path.lineTo(x + width, y + height - radius);
  path.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  path.lineTo(x + radius, y + height);
  path.quadraticCurveTo(x, y + height, x, y + height - radius);
  path.lineTo(x, y + radius); path.quadraticCurveTo(x, y, x + radius, y);
  return path;
}

export function createGatewayModel() {
  const group = new THREE.Group();
  group.name = 'Zhihui porcelain gateway';
  const frameShape = roundedPath(new THREE.Shape(), 2.72, 3.24, .38);
  frameShape.holes.push(roundedPath(new THREE.Path(), 2.22, 2.74, .2));
  const frameGeometry = new THREE.ExtrudeGeometry(frameShape, { depth: .25, steps: 1, bevelEnabled: true, bevelThickness: .065, bevelSize: .065, bevelSegments: 5, curveSegments: 20 });
  frameGeometry.translate(0, 1.85, -.125);
  const porcelain = new THREE.MeshPhysicalMaterial({ color: 0xfcfdff, metalness: .02, roughness: .24, clearcoat: .55, clearcoatRoughness: .2 });
  const frame = new THREE.Mesh(frameGeometry, porcelain);
  frame.name = 'Continuous beveled porcelain frame'; frame.castShadow = frame.receiveShadow = true; group.add(frame);
  const liningShape = roundedPath(new THREE.Shape(), 2.06, 2.58, .16);
  liningShape.holes.push(roundedPath(new THREE.Path(), 1.99, 2.51, .13));
  const liningGeometry = new THREE.ExtrudeGeometry(liningShape, { depth: .13, bevelEnabled: true, bevelThickness: .012, bevelSize: .012, bevelSegments: 3, curveSegments: 20 });
  liningGeometry.translate(0, 1.85, .055);
  const glass = new THREE.MeshPhysicalMaterial({ color: 0x91b4ff, roughness: .1, metalness: .05, transmission: .45, thickness: .12, ior: 1.45, transparent: true, opacity: .85, clearcoat: 1 });
  const lining = new THREE.Mesh(liningGeometry, glass); lining.name = 'Blue glass inner reveal'; group.add(lining);
  // Rounded turns follow the existing Z's open three-stroke brand geometry.
  const points = [new THREE.Vector3(-.59, .48, 0), new THREE.Vector3(.59, .48, 0), new THREE.Vector3(-.59, -.48, 0), new THREE.Vector3(.59, -.48, 0)];
  const zPath = new THREE.CurvePath(); let previous = points[0];
  for (let i = 1; i < points.length - 1; i++) {
    const point = points[i], before = point.clone().lerp(points[i - 1], .12), after = point.clone().lerp(points[i + 1], .08);
    zPath.add(new THREE.LineCurve3(previous, before));
    zPath.add(new THREE.QuadraticBezierCurve3(before, point, after)); previous = after;
  }
  zPath.add(new THREE.LineCurve3(previous, points.at(-1)));
  const blue = new THREE.MeshPhysicalMaterial({ color: 0x6388ef, roughness: .25, metalness: .15, clearcoat: .6, emissive: 0x3f65c6, emissiveIntensity: .08 });
  const mark = new THREE.Group(); mark.name = 'Suspended brand Z'; mark.position.set(0, 1.88, .27);
  mark.add(new THREE.Mesh(new THREE.TubeGeometry(zPath, 96, .085, 12, false), blue));
  for (const point of [points[0], points.at(-1)]) { const cap = new THREE.Mesh(new THREE.SphereGeometry(.085, 16, 12), blue); cap.position.copy(point); mark.add(cap); }
  mark.traverse(child => { if (child.isMesh) child.castShadow = true; }); group.add(mark);
  const base = new THREE.Mesh(new THREE.CylinderGeometry(1.88, 1.94, .12, 96), new THREE.MeshStandardMaterial({ color: 0xf1f6ff, roughness: .55, metalness: .03 }));
  base.scale.z = .65; base.position.y = .08; base.castShadow = base.receiveShadow = true; group.add(base);
  const pedestal = new THREE.Mesh(new THREE.CylinderGeometry(1.78, 1.8, .035, 96), porcelain);
  pedestal.scale.z = .65; pedestal.position.y = .153; pedestal.castShadow = pedestal.receiveShadow = true; group.add(pedestal);
  const lanes = [], particles = [];
  const starts = [[-2.5,.21,1.7],[-2.05,.19,2.1],[2.5,.21,1.7],[2.05,.19,2.1]];
  const ends = [[-.85,1.04,-.28],[-.4,.72,-.4],[.85,1.04,-.28],[.4,.72,-.4]];
  const laneMaterial = new THREE.MeshBasicMaterial({ color: 0x9bbaf6, transparent: true, opacity: .42 });
  const pulseMaterial = new THREE.MeshBasicMaterial({ color: 0x76a1ff, transparent: true, opacity: .8 });
  for (let i = 0; i < starts.length; i++) {
    const start = new THREE.Vector3(...starts[i]), end = new THREE.Vector3(...ends[i]);
    const curve = new THREE.CubicBezierCurve3(start, new THREE.Vector3(start.x * .6,.28,1.2), new THREE.Vector3(end.x,.2,.65), end);
    const lane = new THREE.Mesh(new THREE.TubeGeometry(curve, 64, .007, 6, false), laneMaterial); lane.name = `Entry light ${i + 1}`; group.add(lane); lanes.push(curve);
    const pulse = new THREE.Mesh(new THREE.SphereGeometry(.026, 12, 8), pulseMaterial); pulse.position.copy(curve.getPoint(i / 4)); group.add(pulse); particles.push(pulse);
  }
  group.rotation.y = -.25;
  return { group, mark, lanes, particles, blue };
}
