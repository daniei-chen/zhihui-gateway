import * as THREE from 'three';
import {BIKINI_LAYOUT} from './bikini-layout.js';
import {characterEye, surfacePatch} from './character-surface.js';
import {squidHeadGeometry,squidNoseGeometry} from './squidward-shape.js';

// Procedural fan art: no downloaded character, building or texture assets.
export function buildBikiniNeighborhood({group, materials, mesh, egg, box, link, curve, mergeStatic}) {
  const landmarks = {}, motion = [];
  for (const [key, color] of Object.entries({
    pineapple: 0xe9a33d, pineappleSeam: 0xbf7737, pineappleLight: 0xf0bf58,
    leaf: 0x579563, leafLight: 0x8ab56a, trunk: 0xad8358, stone: 0x728f9e,
    stoneShade: 0x506f80, glass: 0x81c8d2, windowDark: 0x28526a, brass: 0xc5a064,
    wood: 0xac7350, woodLight: 0xd2a270, roof: 0x87614d, door: 0x4c8385,
    road: 0xa8a7ad, curb: 0xd9d6cf, pink: 0xf0a09b, pinkShade: 0xd68083,
    trunks: 0xa5c65e, flower: 0x9769a7, squid: 0xa6c6b4, squidLid: 0x7eac9d,
    squidShirt: 0xb38a55, squidEye: 0xf1e9bc, squidPupil: 0x873c35,
    crab: 0xcf6652, crabShade: 0xa7493c, crabShirt: 0x9bcee0, crabPants: 0x88719b,
    plankton: 0x759b58, planktonShade: 0x43644b, planktonEye: 0xe4d48b,
    sign: 0xf2cfad, coral: 0xe4aa98, seaweed: 0x5e9b89,
    distantBlue: 0x7bb4b5, distantPurple: 0xa29fc2, distantOchre: 0xc9b668,
    chimney: 0x54727a, sidewalk: 0xd7d4bd, shellPink: 0xdfa9a2,
  })) materials[key] = new THREE.MeshStandardMaterial({color, roughness: .8});

  function root(name, position, scale = 1) {
    const item = new THREE.Group(); item.name = name; item.position.set(...position); item.scale.setScalar(scale);
    group.add(item); landmarks[name] = item; return item;
  }
  function placed(name, key) {
    const {position, scale} = BIKINI_LAYOUT[key]; return root(name, position, scale);
  }
  function torus(position, radius, tube, material, parent, name) {
    return mesh(new THREE.TorusGeometry(radius, tube, 6, 36), material, position, parent, name);
  }
  function leaf(points, width, material, parent) {
    const positions = [], indices = [];
    const path = new THREE.CatmullRomCurve3(points.map(p => new THREE.Vector3(...p)));
    for (let i = 0; i <= 9; i++) {
      const t = i / 9, p = path.getPoint(t), tangent = path.getTangent(t);
      const side = new THREE.Vector3(-tangent.z, 0, tangent.x).normalize();
      const w = width * Math.pow(Math.sin(Math.PI * (.02 + t * .98)), .65);
      for (const sign of [-1, 1]) positions.push(p.x + side.x * w * sign, p.y, p.z + side.z * w * sign);
      positions.push(p.x, p.y + w * .22, p.z);
      if (i < 9) {const a = i * 3; indices.push(a, a + 3, a + 2, a + 2, a + 3, a + 5, a + 2, a + 5, a + 1, a + 1, a + 5, a + 4);}
    }
    const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices); geometry.computeVertexNormals();
    const item = mesh(geometry, material, null, parent); item.material.side = THREE.DoubleSide; return item;
  }
  function porthole(parent, x, y, z, radius = .24) {
    const glass = mesh(new THREE.CircleGeometry(radius, 32), 'glass', [x, y, z], parent, 'Porthole glass');
    torus([x, y, z + .012], radius, .036, 'metal', parent, 'Bolted porthole');
    for (let i = 0; i < 8; i++) {
      const a = i * Math.PI / 4;
      egg([x + Math.cos(a) * radius, y + Math.sin(a) * radius, z + .046], [.016, .016, .014], 'brass', parent);
    }
    link([x - radius * .55, y + radius * .30, z + .018], [x + radius * .25, y + radius * .55, z + .018], .012, 'white', parent);
    return glass;
  }
  function tapered(points, radii, material, parent, name) {
    const path = new THREE.CatmullRomCurve3(points.map(p => new THREE.Vector3(...p)));
    const segments = 22, sides = 12, frames = path.computeFrenetFrames(segments, false), positions = [], indices = [];
    for (let i = 0; i <= segments; i++) {
      const t = i / segments, p = path.getPointAt(t), index = Math.min(radii.length - 2, Math.floor(t * (radii.length - 1)));
      const f = t * (radii.length - 1) - index, radius = THREE.MathUtils.lerp(radii[index], radii[index + 1], f);
      for (let j = 0; j <= sides; j++) {
        const a = j / sides * Math.PI * 2;
        const offset = frames.normals[i].clone().multiplyScalar(Math.cos(a) * radius).addScaledVector(frames.binormals[i], Math.sin(a) * radius);
        positions.push(p.x + offset.x, p.y + offset.y, p.z + offset.z);
        if (i < segments && j < sides) {const n = i * (sides + 1) + j; indices.push(n, n + 1, n + sides + 1, n + 1, n + sides + 2, n + sides + 1);}
      }
    }
    const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices); geometry.computeVertexNormals();return mesh(geometry, material, null, parent, name);
  }

  const road = root('Conch Street', [0, .078, 0]);
  const street = new THREE.CatmullRomCurve3([[-27,0,4.4],[-11,0,4.4],[1,0,4.4],[7,0,3.0],[11,0,-.8],[15,0,-5.8],[25,0,-11]].map(p=>new THREE.Vector3(...p)));
  function ribbon(width, material, elevation, name) {
    const vertices=[], indices=[];
    for(let i=0;i<=90;i++) {
      const t=i/90,p=street.getPoint(t),dir=street.getTangent(t),side=new THREE.Vector3(-dir.z,0,dir.x).normalize();
      for(const sign of [-1,1]) vertices.push(p.x+side.x*width/2*sign,elevation,p.z+side.z*width/2*sign);
      if(i<90){const n=i*2;indices.push(n,n+1,n+2,n+1,n+3,n+2);}
    }
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));geometry.setIndex(indices);geometry.computeVertexNormals();
    mesh(geometry,material,null,road,name).castShadow=false;
  }
  ribbon(3.5,'sidewalk',.006,'Continuous curved sidewalk');
  ribbon(2.5,'road',.024,'Continuous Conch Street roadway');
  for (let i = 0; i <= 65; i++) {
    const t=i/65,p=street.getPointAt(t),dir=street.getTangentAt(t),side=new THREE.Vector3(-dir.z,0,dir.x).normalize();
    for(const sign of [-1,1]){
      const curb=box([p.x+side.x*1.33*sign,.036,p.z+side.z*1.33*sign],[.68,.055,.12],.018,'curb',road);
      curb.rotation.y=-Math.atan2(dir.z,dir.x);
    }
    if (i%2===0) {const dash=box([p.x,.030,p.z],[.55,.009,.035],.005,'white',road);dash.rotation.y=-Math.atan2(dir.z,dir.x);}
  }
  for (const [x,z,end] of [[-8.1,2.8,1.0],[-3.1,2.8,-.2],[2.3,2.7,-.7],[10.4,-2.6,-4.6]]) {
    for(let j=0;j<6;j++) egg([x+Math.sin(j*2)*.10,.024,z+(end-z)*j/5],[.25,.025,.17],'curb',road);
  }
  mergeStatic(road);

  const pineapple = placed('Pineapple house', 'pineapple');
  egg([0, 1.61, 0], [1.12, 1.65, .96], 'pineapple', pineapple, 'Pineapple shell');
  // Crossed helices follow the shell surface, rather than floating off the fruit.
  for (const direction of [-1, 1]) for (let i = 0; i < 12; i++) {
    const points = [];
    for (let j = 0; j <= 30; j++) {
      const v = .11 + j / 30 * (Math.PI - .22), a = i * Math.PI / 6 + direction * v * 1.7;
      points.push([Math.sin(v) * 1.126 * Math.cos(a), 1.61 - Math.cos(v) * 1.654, Math.sin(v) * .966 * Math.sin(a)]);
    }
    curve(points, .014, 'pineappleSeam', pineapple);
  }
  for (let row = 0; row < 5; row++) for (let i = 0; i < 12; i++) {
    const v = .5 + row * .49, a = i * Math.PI / 6 + row * .25;
    egg([Math.sin(v) * 1.13 * Math.cos(a), 1.61 - Math.cos(v) * 1.65, Math.sin(v) * .97 * Math.sin(a)], [.027, .047, .027], 'pineappleLight', pineapple);
  }
  for (let i = 0; i < 15; i++) {
    const a = i * 2.4, length = 1 + i % 3 * .23, spread = .6 + i % 4 * .15;
    const p = t => [Math.cos(a) * spread * t, 3.03 + Math.sin(t * 1.7) * length, Math.sin(a) * spread * t];
    leaf([[0, 3, 0], p(.4), p(.7), p(1)], .19, i % 2 ? 'leaf' : 'leafLight', pineapple);
  }
  box([0, .57, .925], [.72, 1.10, .16], .32, 'door', pineapple, 'Pineapple front door');
  torus([0, .61, 1.02], .29, .035, 'metal', pineapple);
  porthole(pineapple, 0, .72, 1.04, .18);
  egg([.24, .44, 1.03], [.035, .035, .035], 'brass', pineapple);
  porthole(pineapple, -.68, 1.63, .78, .24); porthole(pineapple, .60, 2.11, .75, .21);
  curve([[.98, 1.94, 0], [1.25, 1.94, 0], [1.28, 2.33, 0], [1.48, 2.33, 0]], .066, 'metal', pineapple);
  mergeStatic(pineapple);

  const moai = placed('Squidward house', 'moai');
  const profile=[[0,0],[.79,0],[.86,.28],[.79,1.2],[.76,2.1],[.71,3.0],[.63,3.65],[.50,3.77],[0,3.79]].map(p=>new THREE.Vector2(...p));
  const head=mesh(new THREE.LatheGeometry(profile,12),'stone',[0,0,0],moai,'Easter Island head');head.scale.z=.88;
  box([0, 2.91, .57], [1.45, .25, .31], .09, 'stoneShade', moai, 'Continuous heavy Moai brow');
  for (const side of [-1, 1]) {
    box([side * .84, 1.84, -.08], [.34, 1.25, .72], .14, 'stoneShade', moai, 'Moai ear');
    porthole(moai, side * .37, 2.62, .65, .24);
  }
  box([0, 2.17, .83], [.35, 1.13, .63], .11, 'stoneShade', moai, 'Long stone nose');
  box([0, 1.55, .71], [.63, .16, .28], .05, 'stoneShade', moai);
  box([0, .60, .76], [.58, 1.15, .13], .22, 'wood', moai, 'Moai front door');
  for (let i = -1; i <= 1; i++) link([i * .15, .15, .84], [i * .15, 1.04, .84], .012, 'metal', moai);
  egg([.20, .55, .86], [.035, .035, .025], 'brass', moai);
  mergeStatic(moai);

  const rock = placed('Patrick rock house', 'rock');
  mesh(new THREE.SphereGeometry(1, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), 'wood', [0, 0, 0], rock).scale.set(1.13, .72, .98);
  torus([0, .035, 0], 1.04, .035, 'roof', rock).rotation.x = Math.PI / 2;
  link([0, .64, 0], [0, 1.03, 0], .022, 'metal', rock);
  link([-.18, 1.01, 0], [.18, 1.01, 0], .018, 'metal', rock);
  mergeStatic(rock);

  const krusty = placed('Krusty Krab', 'restaurant');
  box([0, .12, 0], [4.35, .25, 2.65], .08, 'wood', krusty, 'Restaurant deck');
  box([0, 1.09, 0], [3.75, 1.94, 2.22], .08, 'woodLight', krusty, 'Barrel restaurant walls');
  const roofShape = new THREE.Shape(); roofShape.moveTo(-2.04, 1.85);
  roofShape.absellipse(0, 1.85, 2.04, 1.22, Math.PI, 0, true, 0); roofShape.lineTo(-2.04, 1.85);
  const roofGeometry = new THREE.ExtrudeGeometry(roofShape, {depth: 2.48, bevelEnabled: false, curveSegments: 28});
  mesh(roofGeometry, 'roof', [0, 0, -1.24], krusty, 'Barrel vaulted roof');
  for (let i = -9; i <= 9; i++) {
    const x = i * .20, height = Math.sqrt(Math.max(0, 1 - x * x / (2.04 ** 2))) * 1.22;
    link([x, 1.86, 1.253], [x, 1.85 + height, 1.253], .011, 'woodLight', krusty, 'Front barrel plank seam');
  }
  for (let i = 0; i < 19; i++) {
    const a = .055 + i / 18 * (Math.PI - .11);
    link([Math.cos(a) * 2.05, 1.85 + Math.sin(a) * 1.23, -1.25], [Math.cos(a) * 2.05, 1.85 + Math.sin(a) * 1.23, 1.25], .018, 'woodLight', krusty);
  }
  for (const z of [-1.10, -.52, .52, 1.10]) curve(Array.from({length: 25}, (_, i) => {
    const a = i / 24 * Math.PI; return [2.07 * Math.cos(a), 1.85 + 1.25 * Math.sin(a), z];
  }), .047, 'metal', krusty, 'Barrel roof binding');
  for (let i = 0; i < 12; i++) {
    const x = -1.75 + i * .32;
    link([x, .24, 1.13], [x, 1.84, 1.13], .012, 'wood', krusty);
  }
  box([0, .95, 1.18], [.82, 1.58, .10], .035, 'door', krusty, 'Krusty Krab double doors');
  for (const side of [-1, 1]) {
    box([side * 1.17, 1.16, 1.18], [1.05, 1.05, .07], .035, 'windowDark', krusty, 'Nautical window');
    for (const offset of [-.32, 0, .32]) {
      link([side * 1.17 + offset, .70, 1.235], [side * 1.17 + offset, 1.63, 1.235], .023, 'white', krusty);
      link([side * 1.17 - .47, 1.16 + offset, 1.235], [side * 1.17 + .47, 1.16 + offset, 1.235], .023, 'white', krusty);
    }
    porthole(krusty, side * .20, 1.17, 1.25, .125);
    link([side * .07, .72, 1.27], [side * .07, .98, 1.27], .018, 'brass', krusty);
  }
  curve([[-1.88, 2.26, 1.30], [-.9, 2.05, 1.34], [0, 2.00, 1.36], [.9, 2.05, 1.34], [1.88, 2.26, 1.30]], .014, 'sole', krusty);
  for (let i = 0; i < 7; i++) {
    const x = -1.50 + i * .50, y = 2.04 + Math.pow(x / 1.5, 2) * .14;
    const geometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x - .14, y, 1.37), new THREE.Vector3(x + .14, y, 1.37), new THREE.Vector3(x, y - .33, 1.37)]);
    geometry.computeVertexNormals(); mesh(geometry, ['tie', 'white', 'frame', 'pineappleLight'][i % 4], null, krusty, 'Signal flag');
  }
  const sign = new THREE.Group(); sign.position.set(-2.67, 0, .65); krusty.add(sign);
  link([0, .04, 0], [0, 3.82, 0], .07, 'wood', sign);
  const shell = new THREE.Shape(); shell.moveTo(-.75, 3.1); shell.bezierCurveTo(-1.23, 3.54, -.88, 4.25, 0, 4.20);
  shell.bezierCurveTo(.88, 4.25, 1.23, 3.54, .75, 3.1); shell.quadraticCurveTo(0, 2.92, -.75, 3.1);
  mesh(new THREE.ExtrudeGeometry(shell, {depth: .12, bevelEnabled: true, bevelSegments: 2, steps: 1, bevelSize: .025, bevelThickness: .025}), 'sign', [0, 0, -.06], sign, 'Clamshell restaurant sign');
  for (let i = -2; i <= 2; i++) curve([[0, 3.12, .085], [i * .24, 3.76, .085], [i * .30, 3.98 - Math.abs(i) * .04, .085]], .012, 'coral', sign);
  const glyphs = {K:['101','110','100','110','101'],R:['110','101','110','101','101'],U:['101','101','101','101','111'],S:['111','100','111','001','111'],T:['111','010','010','010','010'],Y:['101','101','010','010','010'],A:['010','101','111','101','101'],B:['110','101','110','101','110']};
  function word(text, y) {
    const size = .055, start = -(text.length * 4 - 1) * size / 2;
    for (let n = 0; n < text.length; n++) for (let row = 0; row < 5; row++) for (let col = 0; col < 3; col++) {
      if (glyphs[text[n]][row][col] === '1') box([start + (n * 4 + col) * size, y - row * size, .13], [size * .88, size * .88, .018], .005, 'crabShade', sign);
    }
  }
  word('KRUSTY', 3.78); word('KRAB', 3.42);
  mergeStatic(krusty);

  function eyes(parent, x, y, z, size, pupil = 'pupil', eye = 'white') {
    for (const side of [-1, 1]) {
      characterEye({mesh,egg,parent,center:[x+side*size*.65,y,z],radii:[size*.66,size,size*.56],white:eye,iris:pupil});
    }
  }
  const patrick = placed('Patrick Star', 'patrick');
  const starProfile=[[.48,.85],[.49,1.00],[.44,1.23],[.33,1.44],[.23,1.67],[.14,1.90],[.065,2.06],[0,2.12]];
  const starCurve=new THREE.SplineCurve(starProfile.map(p=>new THREE.Vector2(...p)));
  const star=mesh(new THREE.LatheGeometry(starCurve.getPoints(40),28),'pink',[0,0,0],patrick,'Five-point starfish silhouette');star.scale.z=.72;
  for (const side of [-1, 1]) {
    tapered([[side * .30, 1.20, -.01], [side * .54, 1.30, 0], [side * .79, 1.49, 0], [side * .94, 1.59, 0]], [.16, .145, .08, .002], 'pink', patrick, 'Tapered starfish arm');
    tapered([[side * .23, .60, 0], [side * .29, .39, .02], [side * .42, .13, .08], [side * .48, .09, .11]], [.145, .15, .075, .003], 'pink', patrick, 'Rounded pointed leg');
  }
  const shortsProfile=[[0,.46],[.25,.48],[.34,.53],[.42,.65],[.47,.78],[.48,.85]];
  const shorts=mesh(new THREE.LatheGeometry(shortsProfile.map(p=>new THREE.Vector2(...p)),28),'trunks',[0,0,0],patrick,'Green swimming trunks');shorts.scale.z=.72;
  function shortsRadius(y){
    for(let i=1;i<shortsProfile.length;i++)if(y<=shortsProfile[i][1]){
      const [r0,y0]=shortsProfile[i-1],[r1,y1]=shortsProfile[i];return THREE.MathUtils.lerp(r0,r1,(y-y0)/(y1-y0));
    }
    return .48;
  }
  for(const [longitude,y]of [[-.65,.72],[.52,.65],[2.4,.72]]) {
    const vertices=[],indices=[];
    const point=(a,h)=>{const r=shortsRadius(h)+.002;return[Math.sin(a)*r,h,Math.cos(a)*r*.72];};
    vertices.push(...point(longitude,y));
    for(let i=0;i<=40;i++){
      const a=i/40*Math.PI*2,r=.10*(.75+.25*Math.cos(a*5));
      vertices.push(...point(longitude+Math.cos(a)*r*2.8,y+Math.sin(a)*r));
      if(i<40)indices.push(0,i+1,i+2);
    }
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));geometry.setIndex(indices);geometry.computeVertexNormals();
    mesh(geometry,'flower',null,patrick,'Surface-following swimwear flower');
  }
  eyes(patrick, 0, 1.65, .205, .14);
  for (const side of [-1, 1]) curve([[side * .15, 1.82, .106], [side * .05, 1.84, .125]], .011, 'smile', patrick, 'Patrick eyebrow');
  curve([[-.19, 1.33, .260], [-.10,1.29,.292], [0, 1.28, .307], [.10,1.29,.292], [.19, 1.33, .260]], .011, 'smile', patrick);
  egg([0, 1.00, .351], [.018, .029, .005], 'pinkShade', patrick, 'Belly button');
  mergeStatic(patrick); motion.push(time => {patrick.rotation.y = -.12 + Math.sin(time * .5) * .07;});

  const squid = placed('Squidward Tentacles', 'squidward');
  mesh(squidHeadGeometry(),'squid',[0,0,0],squid,'Continuous Squidward head and neck');
  const poloProfile=[[0,0],[.24,0],[.27,.16],[.26,.42],[.19,.63],[0,.65]].map(p=>new THREE.Vector2(...p));
  const polo=mesh(new THREE.LatheGeometry(poloProfile,16),'squidShirt',[0,.94,0],squid,'Brown polo shirt');polo.scale.z=.85;
  for(const side of [-1,1]) {
    const collar=new THREE.Shape();collar.moveTo(0,0);collar.lineTo(side*.16,-.02);collar.lineTo(side*.11,-.17);collar.closePath();
    mesh(new THREE.ExtrudeGeometry(collar,{depth:.023,bevelEnabled:false}),'squidShirt',[0,1.57,.19],squid,'Folded polo collar');
  }
  for(const side of [-1,1]) {
    characterEye({mesh,egg,parent:squid,center:[side*.115,2.155,.217],radii:[.113,.205,.070],white:'squidEye',iris:'squidPupil'});
  }
  for (const side of [-1, 1]) {
    egg([side * .115, 2.27, .228], [.116, .073, .059], 'squid', squid, 'Drooping eyelid');
    egg([side*.25,1.42,0],[.105,.13,.105],'squidShirt',squid,'Closed polo sleeve');
    curve([[side * .25, 1.42, 0], [side * .44, 1.10, .07], [side * .35, .90, .21]], .075, 'squid', squid);
    egg([side*.35,.90,.21],[.078,.085,.072],'squid',squid,'Rounded tentacle hand');
    for (const z of [-.15, .15]) {
      tapered([[side*.13,.98,z],[side*.18,.50,z],[side*.29,.14,z+.10],[side*.47,.12,z+.16]],[.095,.075,.09,.04],'squid',squid,'Four walking tentacles');
      egg([side*.38,.125,z+.15],[.18,.095,.13],'squid',squid,'Rounded tentacle foot');
    }
  }
  mesh(squidNoseGeometry(),'squid',[0,0,0],squid,'Long drooping nose');
  curve([[-.245,1.885,.111],[-.14,1.862,.156],[0,1.857,.168],[.14,1.862,.156],[.245,1.885,.111]],.008,'stoneShade',squid,'Surface-following mouth');
  for(const x of [-.14,0,.14]) {
    const z=-.015+Math.sqrt(1-(x/.36)**2)*.232;
    egg([x,2.545,z],[.026,.034,.005],'squidLid',squid,'Forehead spot');
  }
  mergeStatic(squid); motion.push(time => {squid.rotation.y = -.22 + Math.sin(time * .32) * .06;});

  const krabs = placed('Mr Krabs', 'krabs');
  egg([0, 1.03, 0], [.54, .57, .35], 'crab', krabs, 'Crab shell');
  egg([0, .87, .015], [.56, .36, .37], 'crabShirt', krabs, 'Blue collared shirt');
  box([0, .49, 0], [.70, .32, .49], .12, 'crabPants', krabs);
  box([0, .60, .27], [.69, .065, .04], .01, 'belt', krabs);
  box([0, .60, .30], [.13, .09, .035], .01, 'brass', krabs);
  for (const side of [-1, 1]) {
    curve([[side * .19, .39, 0], [side * .23, .17, 0], [side * .39, .11, .08]], .075, 'crab', krabs);
    link([side * .19, 1.39, 0], [side * .23, 1.97, 0], .055, 'crab', krabs);
    egg([side * .23, 2.00, .015], [.112, .25, .11], 'white', krabs, 'Tall eye stalk');
    egg([side * .23, 2.03, .114], [.035, .085, .025], 'pupil', krabs);
    curve([[side * .45, 1.05, 0], [side * .68, 1.02, 0], [side * .86, 1.36, .05]], .065, 'crab', krabs);
    const claw = new THREE.Group(); claw.position.set(side * .88, 1.48, .05); claw.rotation.z = side * -.30; krabs.add(claw);
    egg([0, 0, 0], [.24, .28, .16], 'crab', claw, 'Large red claw');
    egg([-.13, .21, 0], [.11, .19, .15], 'crab', claw); egg([.13, .21, 0], [.11, .19, .15], 'crab', claw);
    curve([[0, .12, .155], [0, .27, .155], [0, .35, .12]], .012, 'crabShade', claw);
  }
  curve([[-.27, 1.29, .32], [0, 1.20, .36], [.27, 1.29, .32]], .018, 'crabShade', krabs);
  mergeStatic(krabs); motion.push(time => {krabs.rotation.y = -.20 + Math.sin(time * .7) * .045;});

  const plankton = placed('Plankton', 'plankton');
  egg([0, .36, 0], [.13, .27, .10], 'plankton', plankton, 'Plankton capsule body');
  egg([0, .43, .091], [.093, .10, .025], 'planktonEye', plankton, 'Single yellow eye');
  egg([.008, .43, .118], [.034, .049, .009], 'crabShade', plankton);
  curve([[-.09, .51, .11], [0, .50, .13], [.085, .54, .11]], .012, 'planktonShade', plankton, 'Angry eyebrow');
  curve([[-.05, .31, .10], [0, .28, .11], [.06, .31, .10]], .009, 'planktonShade', plankton);
  for (const side of [-1, 1]) {
    curve([[side * .065, .58, 0], [side * .10, .76, 0], [side * .17, .89, 0]], .009, 'planktonShade', plankton, 'Long antenna');
    curve([[side * .09, .37, 0], [side * .20, .32, .02], [side * .23, .40, .02]], .015, 'plankton', plankton);
    link([side * .05, .15, 0], [side * .08, .06, .04], .018, 'planktonShade', plankton);
  }
  mergeStatic(plankton); motion.push(time => {plankton.position.y = BIKINI_LAYOUT.plankton.position[1] + Math.max(0, Math.sin(time * 2)) * .01;});

  const palms = [];
  for (const [x, z, scale] of [[-12.8, -1.6, 1.3], [6.4, -13.2, 1.2], [15.4, -9.5, 1.15]]) {
    const palm = root(`Palm ${palms.length + 1}`, [x, .08, z], scale);
    curve([[0, 0, 0], [.12, 1.2, 0], [.35, 2.4, .1], [.45, 3.6, .04]], .12, 'trunk', palm);
    for (let i = 0; i < 13; i++) {
      const y = .25 + i * .25;
      torus([.12 * y, y, .02], .124 - y * .006, .012, 'wood', palm).rotation.x = Math.PI / 2;
    }
    const crown = new THREE.Group(); crown.position.set(.45, 3.6, .04); palm.add(crown);
    for (let i = 0; i < 9; i++) {
      const a = i * Math.PI * 2 / 9, dx = Math.cos(a), dz = Math.sin(a);
      leaf([[0, 0, 0], [dx * .45, .34, dz * .45], [dx * 1.1, .20, dz * 1.1], [dx * 1.57, -.34, dz * 1.57]], .23, i % 2 ? 'leaf' : 'leafLight', crown);
      curve([[0, 0, 0], [dx * .6, .32, dz * .6], [dx * 1.5, -.25, dz * 1.5]], .013, 'leaf', crown);
    }
    for (let i = 0; i < 3; i++) egg([Math.cos(i * 2.1) * .18, -.12, Math.sin(i * 2.1) * .18], [.13, .16, .13], 'wood', crown, 'Coconut');
    mergeStatic(crown); palms.push(palm);
    motion.push(time => {crown.rotation.z = Math.sin(time * .62 + x) * .035; crown.rotation.x = Math.cos(time * .48 + x) * .025;});
  }
  const scenery = root('Bikini Bottom scenery', [0, 0, 0]);
  for(const [x,z,sx,sy,color] of [[-23,-24,8,1.5,'distantBlue'],[-11,-26,7,1.1,'seaweed'],[4,-25,9,1.4,'distantBlue'],[19,-26,8,1.7,'distantPurple']]) {
    egg([x,-.1,z],[sx,sy,2.7],color,scenery,'Distant reef silhouette');
  }
  // A second street and smaller background homes establish depth without
  // magnifying the main cast. Everything stays procedural and material-batched.
  const background = root('Background neighborhood', [0, .08, 0]);
  const backRoad=box([0,-.025,-13],[48,.035,1.6],.01,'road',background,'Distant cross street');backRoad.castShadow=false;
  for(const [x,z,height,color] of [[-16,-8,3.6,'distantPurple'],[-11,-12,4.4,'distantOchre'],[-6,-15,3.8,'distantBlue'],[0,-16,4.7,'distantPurple'],[5,-18,3.5,'distantOchre'],[18,-15,4.2,'distantBlue'],[22,-9,3.5,'distantPurple']]) {
    const home=new THREE.Group();home.position.set(x,0,z);background.add(home);
    mesh(new THREE.CylinderGeometry(.92,1.15,height,12),color,[0,height/2,0],home,'Tubular Bikini Bottom home');
    for(const y of [height*.25,height*.78]){
      const ring=mesh(new THREE.CylinderGeometry(1.09,1.09,.12,12),'chimney',[0,y,0],home);ring.scale.z=1.04;
      mesh(new THREE.CircleGeometry(.23,14),'glass',[0,y+.15,1.11],home);
      torus([0,y+.15,1.12],.23,.035,'metal',home);
    }
    box([0,.47,1.12],[.48,.91,.12],.16,'door',home);
    const roof=mesh(new THREE.SphereGeometry(1,12,6,0,Math.PI*2,0,Math.PI/2),color,[0,height-.02,0],home);roof.scale.set(1.08,.40,1.08);
    curve([[.4,height,0],[.4,height+.6,0],[.75,height+.6,0]],.08,'chimney',home);
  }
  // Roadside furniture gives characters places to stand and paths to follow.
  for(const [x,z] of [[-11.3,2.5],[4.7,2.1],[13,-3.5]]) {
    link([x,.05,z],[x,2.2,z],.04,'chimney',background,'Street lantern post');
    egg([x,2.30,z],[.15,.22,.15],'white',background,'Pearl street lantern');
    const cap=mesh(new THREE.ConeGeometry(.22,.17,12),'chimney',[x,2.52,z],background);cap.castShadow=false;
  }
  for(const [x,z] of [[-10.9,1.8],[4.4,1.25]]) {
    for(const dx of [-.42,.42]) link([x+dx,.08,z],[x+dx,.52,z],.035,'metal',background);
    for(const dz of [-.14,0,.14]) box([x,.55,z+dz],[1.15,.055,.11],.012,'wood',background,'Street bench');
    box([x,.83,z-.17],[1.15,.22,.055],.018,'wood',background);
  }
  const bus=root('Shell bus stop',[6.3,.08,.85]);
  link([0,0,0],[0,2.25,0],.045,'metal',bus);
  const busSign=mesh(new THREE.CylinderGeometry(.27,.27,.07,20),'white',[0,2.25,0],bus);busSign.rotation.x=Math.PI/2;
  box([0,2.25,.046],[.28,.15,.02],.025,'distantBlue',bus);
  mergeStatic(bus);mergeStatic(background);

  const foreground=root('Foreground coral garden',[0,.08,0]);
  for(const [x,z,h] of [[-13,6.9,1.5],[-9,8.8,1.0],[7.8,7.8,1.2],[13,4.4,1.6],[-15,-4,.9],[5.4,-.8,.8]]) {
    for(let j=0;j<3;j++) {
      const a=(j-1)*.5;
      curve([[x+j*.13,0,z],[x+a*.3,h*.5,z],[x+a*.65,h*.8,z],[x+a*.75,h,z]],.055,j%2?'coral':'seaweed',foreground,'Branching reef coral');
      curve([[x+a*.3,h*.5,z],[x+a*.3+.25,h*.65,z+.13],[x+a*.3+.35,h*.85,z+.12]],.035,'coral',foreground);
    }
    for(let j=0;j<3;j++) egg([x-.3+j*.30,.10,z+.3],[.24,.11,.17],'rock',foreground);
  }
  mergeStatic(foreground);
  for (let i = 0; i < 17; i++) {
    const x = -15 + i * 1.9, z = -9.8 - (i % 3) * .7;
    for (let j = 0; j < 3; j++) curve([[x + j * .09, .1, z], [x + .08 + j * .09, .45, z], [x - .12 + j * .09, .8 - (j % 2) * .20, z]], .035, i % 2 ? 'coral' : 'seaweed', scenery);
  }
  // Distant flower silhouettes are part of the underwater cartoon skyline.
  for (const [x, y, z, r] of [[-18, 9.3, -29, 2.2], [-5, 12.9, -34, 2.8], [10, 10.1, -31, 2.1], [24, 13.1, -38, 2.6]]) {
    const points = [];
    for (let i = 0; i <= 100; i++) {
      const a = i / 100 * Math.PI * 2, radius = r * (.75 + .25 * Math.cos(a * 5));
      points.push([x + Math.sin(a) * radius, y + Math.cos(a) * radius, z]);
    }
    curve(points, .026, 'glass', scenery); torus([x, y, z], r * .16, .024, 'glass', scenery);
  }
  mergeStatic(scenery);
  return {landmarks, palms, update(time) {motion.forEach(fn => fn(time));}};
}
