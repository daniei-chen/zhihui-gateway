# Local Bikini Bottom Modeling Review

2026-10-30. Source/reference inspection only; no fetched script was executed,
no character model or texture was imported, and no remote dependency was added.

## References Inspected

- https://github.com/winchxyz/bikini-bottom : its raw `main/README.md` describes
  procedural Conch Street, landmark placement and geometry batching. The API
  was rate-limited; the root LICENSE request returned 404. This does not prove
  there is no license elsewhere. No implementation or asset is copied.
- https://github.com/wormholeportal/Makone/tree/main/worlds/bicycle : read the
  parameters, wheel, drivetrain and cockpit source to study crossed spokes,
  hub flanges, gear ratios, closed chains and level pedals. Its fetched root
  LICENSE says MIT, Copyright (c) 2026 Makone contributors. The new scene uses
  independently written geometry, not the fetched source or its runtime.
- The exact downloaded reference files, URLs, hashes and failures are retained
  outside the public source under the local review evidence/bikini-references.
  These `main` URLs are mutable; the receipts identify the actual inspected bytes.

## Scene Implementation

`src/sponge-cyclist.js` uses bicycle +X as forward. The rider's entire torso,
face and pants share the same rotated coordinate basis, while shoulders, hands,
hip joints and pedals use bicycle coordinates. Two equal-length articulated
legs reach opposed cranks; feet sit on the level pedal platforms. A 42/18 gear
ratio drives wheels/cassette and a 76-instance closed chain. Fork blades,
dropouts, brake tracks/pads/levers/cables, crossed spokes, nipples, hub flanges,
valves, tire tread, saddle rails, headlamp and reflectors are actual geometry.

`src/bikini-neighborhood.js` contains a continuous Conch Street, pineapple
house with crossed surface lattice and crown, Moai house, Patrick's rock,
barrel-roof Krusty Krab with windows, flags and a geometry-lettered shell sign,
plus Patrick, Squidward, Mr. Krabs, Plankton and coconut palms. Animated crowns
keep separate pivots; immutable detail is merged by material. At review r3 the
scene has 230 meshes, 505458 stored vertices and about 192k rendered triangles
including chain instances. These budgets are not measured device FPS.

Five presets expose the town, cyclist, front view, homes and restaurant.
Portrait first-load framing starts on the cyclist so the street does not become
unreadable miniatures; the town remains selectable. Drag/keyboard/pause,
reduced-motion/offscreen handling and teardown are preserved.

## Verification And Rights

35 homepage/preview tests pass, including direction, hand contact, constant
leg lengths, pedal contact, chain movement, landmarks, portrait framing and
resource disposal. Static asset verification passes. Standalone OpenGL views
compile the actual sea/sky GLSL and inspect the actual transformed geometry.
They do not reproduce the browser's final Three.js lighting/shadow pipeline.
Browser access is disabled in Work settings: no browser/mobile pixel or FPS
acceptance is claimed, and no alternate browser automation was used.

This is user-requested fan art of existing characters/settings, not an original
character IP or a claim that the characters are MIT-licensed. Public publication
needs separate rights review. No deployment, push or original-login change is
part of this local review.


## Scale And Street-Depth Revision (Local Review r9)

Additional references inspected, not imported into the scene:

- https://github.com/winchxyz/bikini-bottom : viewed the Conch Street screenshot
  and read src/gen/city.js, buildings.js and landmarks.js. Reference ideas are
  a road as the town spine, plots facing the street, layered nautical silhouettes
  and batching. No source or assets copied.
- https://github.com/ziadgafar/Sponge-Bob-Game : read the README on the two-level
  environment. Raw C++ returned zero bytes and the GitHub page yielded no lines;
  this is not claimed as a successful character-code review.
- https://github.com/Abdalrahman-Alhamod/SpongeBob : read the camera/lighting
  README. It renders a word, so it is not a character-proportion reference.
  README claims MIT but root LICENSE returned 404; asset permission unverified.
- https://www.buzzfeed.com/lyapalater/spongebob-squarepants-now-and-then : viewed
  animation stills reproduced in the article, notably the three homes and the
  SpongeBob/Plankton frame. These are secondary-source stills, not a claim of
  watching a full episode or consulting official model sheets. The attempted
  Paramount page yielded no useful visual material. An unverified YouTube
  thumbnail is not relied on as an official reference.

URLs, hashes and failures remain in private review evidence. No remote script
was executed and no character textures/meshes were imported.

src/bikini-layout.js defines shared scene units. The whole bicycle/rider rig
scales uniformly to 0.48, preserving local-space pedal/hand/leg constraints.
Its height is about 1.53 units; pineapple 7.47, Patrick 1.42, Squidward 1.96,
Mr. Krabs 1.54. Plankton stays intentionally tiny (0.21). These are art-directed
relative units, not official dimensions. Actual door bounds are 1.82 / 1.98 /
2.45 units. Feet and tires meet sand/raised roadway within tested tolerances.

Residents' Row follows rock / Moai / pineapple order. A curved road reaches the
restaurant; cross street, seven tubular homes, distant reefs, lanterns, benches,
shell bus stop and foreground coral establish depth. Squidward has a full-depth
cranium, jaw, cheeks, neck, nose and four rounded walking tentacles. Moai has a
tapered lathed body instead of a box/cap. All geometry is procedural fan art.

The original ceilings (260 meshes / 550k vertices / 210k stored triangles) are
unchanged. Curbs, tiny planks and oversubdivided curves were optimized instead
of raising limits. Exact final data is in bikini-scale-review-r9.json.

37/37 homepage/preview tests pass, no skips: actual transformed door bounds,
adult/bike/house ratios, ground contact, head depth, hand/pedal constraints and
projected landmark corners in five presets at four portrait/wide aspects. r7's
new door test initially used a stale anchor world matrix; r8/r9 update it
explicitly. Failed evidence is preserved. The diagnostic Python renderer now
reads exported camera presets instead of keeping a duplicate camera table.

Final standalone views: bikini-{town,homes,front,restaurant}-r9.jpg. They inspect
actual geometry and GLSL, not browser screenshots, in-page Three.js shadows,
responsive pixels or FPS. Local 4188 serves rebuilt scene and AI/joy copy.
Approved homepage, original login and import manifest remain unchanged.
No upload, commit, push or production change is part of this revision.


## Profile, Basket And Expanded Workspace Revision (2026-10-30)

Local review only, following the user's supplied side-view screenshots. Snapshot:
baseline/character-profile-20261030-194608. No commit, push, upload or deployment.
Original published home/login/import manifest SHA256 values remain unchanged.

- character-surface.js constructs curved eye/iris/pupil patches on ellipsoid
  surfaces, not stacked tangent-plane disks. Sponge body thickness and smile
  transitions updated; the head remains facing bicycle +X.
- Patrick uses one continuous rounded torso/head profile and fitted swimwear.
  Flower patches follow the actual shorts surface; no raised floating plates.
- Squidward has a more integrated nose/bridge, lower face, cheeks and neck,
  a lathed polo, closed shoulder sleeves and rounded tentacle hands.
- An actual inward-winding defect in tapered limb geometry was found when the
  diagnostic renderer started respecting material-side backface culling.
  Triangle order corrected; regression checks front/back outward arm normals.
  Earlier independent renders without culling were insufficient to catch it.
- Open tapered wicker basket, visible rails/uprights, stem bracket/fork stays,
  five fresh flowers, stems and leaves. Floor clears tire; bouquet below face.
- Desktop and phone now default/reset to the side three-quarter close view,
  keeping neighborhood background. Existing optional town/detail views remain.
- Workspace iframe reports its own intrinsic content height, through an
  owner-frame-source-checked finite integer message (240..1600). Parent expands
  its container; code no longer clips at a fixed max-height. Height calculation
  avoids min-height:100vh feedback and deduplicates unchanged measurements.
- Console and homepage console-section carry mint/apricot edge gradients.
  Central cards and chart semantic colors unchanged; dark theme not recolored.

Final evidence: character-profile-review-final2.json. 42/42 home/preview tests,
59/59 console tests, TypeScript + 26-route export passed. Node emitted existing
module-type warnings; not a clean-warning claim. Static integrity and diff check
passed. Scene: 258 meshes, 520480 stored vertices, 194631 stored triangles,
195531 rendered triangles including instances; original ceilings not raised.
The final console build is Kl-I5q8vdhnNvIMv8tZDf. HTTP 200 and served compiled
side gradients verified at 4188/4189. No business/provider operation performed.

The r1 new surface test caught duplicated degenerate center vertices/normals;
fixed with a single shared center fan and regression. r5 test expected CSS zero
as '0' instead of computed '0px'; corrected fixture expectation. Failed logs
retained. Final independent renders inspect actual side/front geometry, with
backface culling enabled, not browser lighting/shadow/layout/FPS screenshots.
The supported browser inventory still reports local browser use disabled in
Work settings; no alternate browser automation was used. In-page no-scroll,
responsive pixels and final visual acceptance remain for user review.

## Squidward Silhouette And Opening Composition

The latest user side-profile screenshot exposed a spherical rear skull and
separate jaw/neck transitions. Replaced that stack with one closed head/neck
surface whose width and depth vary independently, plus a curved closed nose.
Eyes, lids, mouth and forehead spots now fit the narrower head. Wrapped rings
and shared poles avoid seams and collapsed triangles under backface culling.

Moved Squidward to the left rear of the cyclist and shifted the camera target
slightly left without changing the close-view radius. The foreground remains
the bicycle and SpongeBob, not a distant town panorama. Twenty-eight actual
projection checks (seven animation times, four aspect ratios) keep both body
silhouettes in frame with a minimum horizontal NDC gap of 0.07668. This is a
geometry regression, not a claim that every arbitrary orbit is occlusion-free.

44/44 home/preview tests pass, no skips; static integrity passes. Closed edge
manifolds, nondegenerate faces, positive signed volumes, outward radial normals
and head/neck/profile proportions have dedicated checks. The old spherical-head
and bridge assertions were replaced by these stronger construction checks;
the first failed run is retained as squid-silhouette-tests-r1.txt.

Final scene: 258 meshes, 529840 stored vertices, 197751 stored triangles,
198651 rendered triangles including instances. Original ceilings unchanged.
Evidence: squid-silhouette-review-final.json and five squid-*-final.jpg renders.
The diagnostic renderer now supports portrait dimensions and aspect-correct
sky rays; exported subject positions track the actual layout. These images use
actual scene geometry/GLSL and culling, not browser shadows or screenshots.

Only local 4188 was refreshed; served bundle matches the rebuilt file exactly.
Original home/login/import hashes unchanged. No console edit, provider action,
upload, push or deployment. Browser acceptance remains unavailable under the
previously reported Work browser setting; no alternate browser was used.
