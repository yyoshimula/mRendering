"""Shared preset material lookup for browser and Mitsuba model viewers."""
from functools import lru_cache
from pathlib import Path
import yaml
import json

# Material assignments live in presets, not in these OBJ files' embedded MTLs.
_MATERIAL_PRESETS = {
    'akatsuki.obj': ('akatsuki.yaml', 'model', 'model'),
    'hubble.obj': ('oos_hubble.yaml', 'deputy', 'deputy'),
    'ykwn_satellite.obj': ('relative_ykwn_inspection.yaml', 'deputy', 'deputy'),
}


@lru_cache(maxsize=16)
def _read_material_preset(path, mtime):
    return yaml.safe_load(Path(path).read_text())


def library_metadata(path):
    model = Path(path).resolve()
    library = Path(__file__).resolve().parent / 'models/library'
    if not model.is_relative_to(library.resolve()):
        return None
    sidecar = model.parent / 'materials.json'
    if not sidecar.is_file():
        return None
    data = json.loads(sidecar.read_text())
    return data if data.get('model') == model.name else None


def registered_materials(path):
    imported = library_metadata(path)
    if imported is not None:
        return imported.get('parts', {}), imported.get('default', {})
    entry = _MATERIAL_PRESETS.get(Path(path).name)
    root = Path(__file__).resolve().parent
    # Do not assign spacecraft materials to an unrelated file with the same name.
    if not entry or Path(path).resolve() != (root / 'models' / Path(path).name).resolve():
        return None
    preset, section, prefix = entry
    source = root / 'presets' / preset
    config = _read_material_preset(str(source), source.stat().st_mtime_ns)[section]
    return config.get(prefix + '_parts', {}), config.get(prefix + '_bsdf', {})


