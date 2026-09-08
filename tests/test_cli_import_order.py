"""CLI 起動経路（Mitsuba バリアント未設定の新規インタプリタ）で各 verb の import 連鎖が
通ることを確認する。2026-09-08 に scene_earth の戻り値注釈が import 時評価されて
render/preview/lightcurve/onboard が CLI・GUI から起動不能になった回帰の再発防止。
テストプロセス自身は scene_common を先に import しているため、必ずサブプロセスで検証する。
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliImportOrderTests(unittest.TestCase):
    def _fresh_import(self, module: str) -> None:
        proc = subprocess.run([sys.executable, '-c', f'import {module}'], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, f'import {module} failed:\n{proc.stderr[-2000:]}')

    def test_render_family_modules_import_without_preset_variant(self):
        for module in ['satellite_orbit', 'scene_earth', 'scene_builder']:
            with self.subTest(module=module):
                self._fresh_import(module)

    def test_every_verb_entry_module_imports(self):
        for verb in ['render', 'preview', 'lightcurve', 'onboard', 'relative', 'rotation', 'groundobs']:
            with self.subTest(verb=verb):
                self._fresh_import(f'verbs.{verb}')

    def test_mrender_help_for_each_verb(self):
        for verb in ['render', 'preview', 'lightcurve', 'onboard']:
            with self.subTest(verb=verb):
                proc = subprocess.run([sys.executable, 'mrender.py', verb, '--help'], cwd=str(ROOT),
                                      capture_output=True, text=True, timeout=300)
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                self.assertNotIn('before setting a variant', proc.stderr)


if __name__ == '__main__':
    unittest.main()
