# Imported spacecraft models

Source: `3Dmodels` (separate source repository).

Imported: ACS3, BlueWalker-3, EKRAN, H-IIA upper stage (ADRAS-J target).

Non-public models from collaborations live under `internal/` (git-ignored, lab-only)
and are imported by their own scripts, e.g. `venv/bin/python internal/import_horn_models.py`.
Models are exported in metres, preserving the source Blender XYZ axes (Z up).
Use scale `0.001` in the relative renderer, whose scene units are kilometres.
The origin is the original model origin, not an inferred centre of mass.

Rebuild with installed Blender:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tools/import_blender_models.py -- /path/to/3Dmodels
```

OBJ files and extracted textures are generated local assets and ignored by Git.
`materials.json` records provenance, bounds, and part-material assignments.
Visible mesh/curve objects are evaluated and triangulated, then grouped by material
for fewer draw calls. Cameras, lights, previews, references and archive bundles
are excluded. Direct base-color image textures are copied; procedural shader
nodes are approximated, not baked. Original .blend files remain untouched.
