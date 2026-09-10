"""Shared preset material lookup for browser and Mitsuba model viewers."""
from functools import lru_cache
from pathlib import Path
import yaml
import json

# Material assignments live in presets, not in these OBJ files' embedded MTLs.
# 値は (リポジトリ相対のプリセット path, セクション, キー接頭辞)。
_MATERIAL_PRESETS = {
    'hubble.obj': ('presets/oos_hubble.yaml', 'deputy', 'deputy'),
}
# 配布に含めないモデル（internal/models/）の対応表は internal/material_presets.json
# に同じ形式で置く（無ければ何もしない）。
_MODEL_DIRS = ('models', 'internal/models')


def _material_presets():
    table = dict(_MATERIAL_PRESETS)
    extra = Path(__file__).resolve().parent / 'internal' / 'material_presets.json'
    if extra.is_file():
        for name, spec in json.loads(extra.read_text()).items():
            table[name] = tuple(spec)
    return table


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
    root = Path(__file__).resolve().parent
    model = Path(path).resolve()
    entry = _material_presets().get(model.name)
    # Do not assign spacecraft materials to an unrelated file with the same name.
    if not entry or not any(model == (root / d / model.name).resolve() for d in _MODEL_DIRS):
        return None
    preset, section, prefix = entry
    source = root / preset
    config = _read_material_preset(str(source), source.stat().st_mtime_ns)[section]
    return config.get(prefix + '_parts', {}), config.get(prefix + '_bsdf', {})


