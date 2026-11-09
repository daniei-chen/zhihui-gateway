# Design QA - Selected Seaside Cycling Review (2026-10-31)

final result: blocked

The user selected Jacory Ou's Pelican / Velo, with a small, smooth-scene
requirement. The current candidate uses reduced MIT-derived sea/sky, bicycle
and cycling rig, a short-bill partridge, flower basket and one boardwalk.
The rejected ring and retired town are not active assets. The duplicate lower
workspace is removed; hero console and three-platform interactions remain.

Actual geometry/shaders were inspected with standalone OpenGL at coast/side/
front angles, NOT browser captures or timing. Source/DOM tests pass 63/63,
including historical fixtures; imported closure/HTTP/CSP tests pass 7/7.
Preserved production login remains byte-identical. Scripts measure 160693 gzip
bytes including Three.js; poster is 70480 bytes. Geometry has 23696 triangles
including sky/instances, 84 drawables (not measured render-pass draw calls).
Desktop caps automatic motion at 30fps/420000 pixels; mobile at 24fps/220000
pixels and no shadows. These are budgets, not measured device FPS.

Supported browser inventory still returns Local browser use is disabled in
Work settings. Browser visual comparison, real WebGL, opaque-origin sandbox,
mobile gestures and device FPS remain unverified. No workaround was used.
After user visual approval, the static candidate was deployed on 2026-10-31;
62 public static hashes and 33 console assets verified. Browser FPS remains
unmeasured. See seaside-release-and-server-cleanup-20261031.md.
HTTP preview is on port 4188 with synthetic
console on 4189; HTTP success does not establish visual acceptance.

## Historical Authoring QA (Not Current Publication Scope)

## Scope and target

All work is local. The server, gateway, production config and business data are unchanged. The target is the original light Zhihui homepage plus explicit corrections: spacious centered hero, authentic console screenshots, original-color task animation as screen two, bicycle as screen three, four entries (Codex, WorkBuddy, Qoder, Trae), rewritten copy, blue bicycle/scarf/water, and a detailed white Thinker lower on the page.

Current URL: http://127.0.0.1:4173/

## Browser evidence

Current-page screenshots: unavailable. Rechecked the supported browser runtime on 2026-10-29; it returns `Local browser use is disabled in Work settings`. No alternative browser path was used. Desktop/mobile layout, WebGL lighting, shader compilation, CSP behavior, touch handling, console errors, performance and final motion appearance are not browser-verified.

Source visual references: evidence/original-review/, original public synthetic preview images, user-supplied 971x712 task illustration, and read-only console structure observations. Real private screenshots are excluded from public output.

## Implemented changes

- Hero retains original spacing direction, logo and actual console preview. First three sections are top, task-scene, ride-preview.
- Task illustration keeps original white/gray/purple/dark colors and displays explicit demonstration numbers. Its actual local script is tested for value progression and playback/reduced-motion lifecycle.
- Bicycle retains original warm environment; scarf, bicycle and water are blue. Actual scene script changes wheel/feet/scarf/scenery across controlled frames, pauses and resumes.
- Four-entry selectors synchronize summary, paths and three code formats. Codex URL remains a placeholder because the real endpoint was not verified.
- Eight console previews use unchanged same-build public synthetic screenshots, not private screenshots. Models and usage detail cards open the corresponding workspace tab.
- Thinker uses a real Scan the World mesh, 837,482 faces and 418,714 vertices, white material and local Three.js/Draco. It loads near the viewport, supports full/detail, drag, keyboard, pause and reset, and keeps a poster on failure. Model/derivative attribution is included under CC BY-SA 4.0.
- Sculptural copy connects judgment and focus to the product: tools move work forward, users keep decisions. Neutral warm gallery is intentionally not blue.

## Model evidence and limitations

Source SHA-1 matches Wikimedia's file page. Source checks: one connected closed mesh, consistent winding, finite positions and zero degenerate faces. Axis rotation/translation/uniform scale do not alter the pose. Sequential Draco preserves every triangle index, face and vertex count; decoded coordinate error is measured in public/models/thinker-manifest.json. These checks do not prove absence of every intersection or scan imperfection.

Independent OpenGL renders were generated and visually inspected at 0/90/180/270 degrees and at an upper-body detail view. Paths: evidence/sculpture/geometry-contact-sheet.jpg and geometry-detail.jpg. They show connected anatomy and visible facial, hand, muscle, foot and rock features in these views. They are standalone geometry renders with different lighting from Three.js, not webpage screenshots or proof of final browser appearance.

## Completed nonvisual checks

npm run check: 14 test groups, script syntax, 34 top-level assets, 81 unique IDs, eight preview panel pairs, 29 immutable snapshot hashes and 16 synthetic preview hashes. Child scene assets, local viewer, model, decoder and attribution are checked. Coverage includes platform/code synchronization, workspace/menu/FAQ/copy logic, route/ambient playback, both embedded scenes, compressed model fidelity, sculpture loading/playback and failure poster behavior. Renderer tests use a test double; geometry tests decode the actual GLB.

## Outstanding acceptance

1. In the supported browser, inspect 1280x720, narrow desktop and 390x844. Capture matching source/current views and compare together.
2. Check hero scale, task window legibility, third-screen framing and mobile section rhythm/overflow.
3. Watch all motion in a real browser, exercise global/local pause and reduced motion, inspect network and console.
4. Verify WebGL model loading, warm white lighting/shadows, detailed views, all-angle anatomy, touch scrolling/rotation and keyboard controls.
5. Fix observed issues and re-capture before visual approval. Goal remains incomplete while browser evidence is missing.

## Packaging and HTTP verification

Latest local build succeeded. All 50 public/dist file hashes match exactly. Fourteen representative homepage/scene/viewer/model/decoder/license URLs returned HTTP 200 with bytes identical to public files and expected MIME types, including model/gltf-binary, image/webp and application/wasm. Private reference, raw source-model and archive URLs return 404; POST returns 405. Details: evidence/sculpture/http-verification.json.

The task illustration was made self-contained with inline CSS/JS, matching the scripts-only opaque-origin sandbox contract and avoiding self-origin resource ambiguity. CSP/render behavior still requires browser validation. The compressed model has zero degenerate faces after decoding.

## Responsive task scene audit

Source review found a breakpoint collision: reference CSS hid sidebar/environment below 760px, while the local three-column layout was intended above 600px. Explicit 601px-and-up visibility now matches the local column rule and prevents empty side columns at narrow desktop illustration widths. Local color variables are defined on the isolated document root. The self-contained document has no external script/style/image dependencies. Rebuilt and passed the same 14 check groups; representative HTTP bytes and all 50 output hashes rechecked. These source checks do not change the blocked visual-QA result.

## User screenshot correction - 2026-10-29

User supplied codex-clipboard-b4cbb6f9-b57f-42fd-9101-1fa6c1a031c9.png (1194x713). It visibly shows the bicycle/bird cropped at the right edge of a 634x490 scene. Source evidence identified inherited translateX(50%) combined with a local right:12% override. Replaced the competing breakpoint layout with a centered SVG viewport using left:50%, translateX(-50%), width minus 40px, height 88% and xMidYMax meet. Added 20-unit viewBox safety padding. This fits both dimensions without cropping; calculations checked 634x490, 800x490, 430x430, 354x360 and 300x360. Calculations are not browser screenshots.

Thinker is now the immediate sibling after ride-preview, ahead of product/platform sections. Numbering follows the fourth-screen placement. Ride section, iframe body and sky use matching pale blue (#edf5ff); sculpture section outer background is white, with its neutral gallery and white sculpture retained. The remaining page keeps the established light theme.

Rebuilt; all 14 checks pass, including immediate section adjacency and SVG fit mode. Four changed resources returned HTTP 200 with exact local bytes; all 50 public/dist hashes match. Browser runtime still returns Local browser use is disabled in Work settings, so current screenshot verification remains blocked.

## Login page implementation - 2026-10-30

User authorized the previously described porcelain gateway plus right-hand login form. Local /login and /login/ routes serve public/login/index.html. Desktop uses pale blue left story/model and white right form; mobile uses a compact top scene. Three.js geometry is code-native with beveled frame, separate blue inner lining, Z, pedestal and four paths. A geometric overlap hiding the initial lining was corrected before generating the current poster. Standalone geometry render was inspected, not a webpage screenshot.

The actual public /login source was fetched using read-only GET and its checksum saved under reference-login. Current endpoints are /api/me and /api/login, same origin; success requires data.ok === true. Local implementation defaults to preview and sends neither session probes nor credentials. Production activation is explicit and documented in LOGIN-HANDOFF.md. No real authentication was attempted.

20 test groups pass, including six login-specific groups covering local no-network behavior, DOM autofill, password visibility/caps lock, empty errors, same-origin request contract, duplicate prevention, error recovery, four model paths, renderer size and reduced/background/offscreen handling. Geometry and authentication tests use mock rendering/server boundaries, not real browser or production sessions. Nine local URLs, including /login and /login/, returned exact current bytes. All 56 public/dist file hashes match. Local auth POST returns 405 and session GET returns 404 as intended. Details in evidence/login/http-verification.json.

Supported browser setup still returns Local browser use is disabled in Work settings. Final result remains blocked for real desktop/mobile visual QA, WebGL shader/material behavior, native autofill, touch and authenticated integration. Login source and CSS are editable; no server deployment occurred.
