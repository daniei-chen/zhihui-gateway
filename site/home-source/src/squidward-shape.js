import * as THREE from 'three';

// One continuous neck/jaw/forehead surface, rather than overlapping spheres.
// Cross-sections describe width and profile depth independently of the camera.
export const SQUID_HEAD_PROFILE = Object.freeze([
  [.108,1.42,.10], [.115,1.56,.105], [.13,1.70,.12], [.20,1.79,.15],
  [.31,1.85,.19], [.33,1.92,.195], [.28,2.02,.19], [.33,2.16,.225],
  [.405,2.31,.255], [.41,2.43,.255], [.35,2.56,.225],
  [.24,2.66,.16], [.115,2.72,.08], [0,2.74,0],
]);

export function squidHeadGeometry() {
  const profile = new THREE.CatmullRomCurve3(SQUID_HEAD_PROFILE.map(p => new THREE.Vector3(...p)), false, 'centripetal');
  const positions = [], indices = [], rows = 48, sides = 32;
  for (let row = 0; row < rows; row++) {
    const section = profile.getPoint(row / rows), width = Math.max(0, section.x), depth = Math.max(0, section.z);
    for (let side = 0; side < sides; side++) {
      const angle = side / sides * Math.PI * 2;
      positions.push(Math.sin(angle) * width, section.y, -.015 + Math.cos(angle) * depth);
      if (row < rows - 1) {
        const n = row * sides + side, next = row * sides + (side + 1) % sides;
        indices.push(n,next,n+sides,next,next+sides,n+sides);
      }
    }
  }
  // Shared poles and wrapped rings keep the silhouette smooth under culling.
  const top = positions.length / 3, bottom = top + 1;
  positions.push(0,2.74,-.015,0,SQUID_HEAD_PROFILE[0][1],-.015);
  for(let side=0;side<sides;side++) {
    const next=(side+1)%sides,last=(rows-1)*sides;
    indices.push(last+side,last+next,top,bottom,next,side);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions,3));
  geometry.setIndex(indices); geometry.computeVertexNormals();
  return geometry;
}

export function squidNoseGeometry() {
  const points = [[0,2.27,.19],[0,2.17,.275],[0,2.03,.39],[0,1.90,.455],[0,1.82,.45],[0,1.80,.415]];
  const path = new THREE.CatmullRomCurve3(points.map(p=>new THREE.Vector3(...p)));
  const positions=[],indices=[],rows=32,sides=20,frames=path.computeFrenetFrames(rows,false);
  for(let row=0;row<rows;row++) {
    const t=row/rows,p=path.getPointAt(t);
    const radius=(.062+.049*Math.sin(t*Math.PI*.8))*Math.sqrt(Math.max(0,1-Math.pow(t,10)));
    for(let side=0;side<sides;side++) {
      const angle=side/sides*Math.PI*2;
      const offset=frames.normals[row].clone().multiplyScalar(Math.cos(angle)*radius)
        .addScaledVector(frames.binormals[row],Math.sin(angle)*radius);
      positions.push(p.x+offset.x,p.y+offset.y,p.z+offset.z);
      if(row<rows-1){const n=row*sides+side,next=row*sides+(side+1)%sides;indices.push(n,next,n+sides,next,next+sides,n+sides);}
    }
  }
  const tip=positions.length/3,base=tip+1;
  positions.push(...path.getPointAt(1).toArray(),...path.getPointAt(0).toArray());
  for(let side=0;side<sides;side++) {
    const next=(side+1)%sides,last=(rows-1)*sides;
    indices.push(last+side,last+next,tip,base,next,side);
  }
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
  geometry.setIndex(indices);geometry.computeVertexNormals();return geometry;
}
