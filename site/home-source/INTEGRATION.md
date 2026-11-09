# Editable Homepage Handoff

The selected product release is three platforms: WorkBuddy, Qoder and Trae.
Codex is deferred to a separate product. Production login stays byte-identical
to the serving original; the source export's concept login is not published.
Original task and cycling scenes live in `scenes/`; run `build_scenes.py` to
copy them into the working export. Reference-site artwork is not distributed.

The selected local review now uses the seaside sunset direction from
[Jacory Ou's Pelican / Velo](https://github.com/JacoryOu/pelican-velo), MIT.
Original selected files and hashes are retained in `vendor/pelican-velo/`;
bounded derivatives are in `src/velo-lite/`, with a short-beaked partridge,
vintage bicycle, flower basket, one boardwalk and sea/sky shaders. No island,
sound, textures, downloaded character models, bloom or composer is included.
Run `python build_scenes.py` and `node build-velo.mjs` in this directory.
The boot script only downloads the renderer after valid parent playback and
intersection. Its classic-script loader works with the existing scripts-only
sandbox without granting same-origin access. Mobile shadows are disabled.
The old town and rejected shader ring remain source-level historical fixtures;
neither is included in the active publication closure.
`scenes/connected-paths.js` and `scenes/color-review.css` are deliberately loaded
by the review server (`node site/tools/color-review-preview.mjs` from the
repository root) and copied into each new static candidate. The task scene has
a deterministic context/build/test/receipt sequence rather than fake throughput.
No production publication is authorized before the user's visual review.

This directory retains the supplied authoring scripts, Three.js sources, tests,
snapshot provenance and lockfile. `source-import.json` identifies those input
files; it is not a claim that production has been updated.

## Recreate The Working Export

From the repository root:

```text
python site/tools/materialize-home-source.py
cd site/home-source
npm ci --ignore-scripts
# Point ZHIHUI_THINKER_REFERENCE to the supplied reference-models/thinker-normalized.glb.
npm run check
node server.mjs --port 4183
```

The first command verifies `site/home-import.json` and creates an ignored
`public/` directory. It refuses to overwrite an existing working directory.
It does not materialize or replace the separate preserved login. The supplied
historical login design is not the selected product login; do not publish it.
Edit the HTML, styles and controllers inside this reconstructed `public/`;
edit Three.js geometry/rendering in `src/`, then run `npm run build:viewer`.

To test production-like asset paths and narrow CSP, run
`node site/tools/preview-server.mjs --port 4184` from the repository root.
It does not proxy APIs or authenticate users. Import a reviewed working export
with `python site/tools/import-home-export.py <absolute-public-directory>`;
each distinct input tree receives its own immutable asset namespace.
The importer always preserves `site/login/index.html`; its exact approved hash
must match `site/login/preservation.json` before any homepage import.
The reconstructed `site/home-source/public` is also an accepted import source;
other sources inside `site/` are refused to prevent importing the output itself.

## Preserved Boundaries

- `prepare.py` and scene extraction scripts are historical authoring steps.
  Running them can replace hand-edited page markup; review the diff, do not
  assume a full build regenerates every later editorial change.
- The 61 MB original STL/intermediate model and private reference files are not
  duplicated into Git. The compressed model, poster, attribution and manifest
  are retained in the versioned public assets. Rebuilding raw geometry needs
  the original user-supplied package or its attributed public source.
- The full geometry regression deliberately requires the uncompressed reference:
  set `ZHIHUI_THINKER_REFERENCE` to its absolute local path. An absent reference
  fails that test; it is not silently skipped. All other interaction, login,
  asset-integrity and viewer-source checks run on the retained files.
- `source-import.json` records original input hashes. Later editorial changes
  (including the visible-first-paint animation) are normal
  Git diffs, not claims that those edited files still match the supplied ZIP.
- The Thinker model/poster retain CC BY-SA 4.0; Three.js and Draco retain their
  own notices. The selected bicycle, character rig and sea/sky are reduced
  MIT derivatives, not wholly original local geometry. Keep Jacory Ou's notice
  and the provenance file in the publication closure. The retired SpongeBob
  town is historical fan-art source, not the selected publication content.
- Supplied historical documents describe their own test dates. New browser
  checks, release evidence and production status belong to the integration
  report, not to those historical claims.

## Historical Bikini Bottom Review (Not The Selected Scene)

The local riding scene now has a forward-facing articulated rider, detailed
bicycle drivetrain and a procedural Bikini Bottom neighborhood. See
`references/bikini-modeling.md` for source/reference provenance, geometry
budgets, camera presets and the explicit browser/rights acceptance limits.
Rebuild with `python build_scenes.py` and `node build-coastal.mjs` from this
directory. These commands update only the ignored local working export, not
the approved immutable homepage or preserved login.
