"""CLI ヘルプの表示言語（cli_i18n.py）のカバレッジ検査。Mitsuba 不要。

Run with: python -m unittest discover -s tests -p 'test_cli_i18n.py'（要 PyYAML）

検査内容:
  - 各 verb のパーサ（relative / rotation / groundobs / render 系 / gui）の
    description / epilog / help / metavar の日本語が全て辞書にある
  - 英語モードでは format_help() に日本語が一切残らない
  - 日本語モードでは原文のまま（置換されない）
  - 辞書の英訳側に日本語が無く、%(…)s / {name} の書式が原文と一致する
  - 言語判定（MRENDER_LANG / LANG）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cli_i18n  # noqa: E402

JA = re.compile('[ぁ-んァ-ン一-龥]')


def _parser_strings(p: argparse.ArgumentParser) -> list[str]:
    out = [p.description, p.epilog]
    for g in p._action_groups:  # noqa: SLF001
        out += [g.title, g.description]
    for a in p._actions:  # noqa: SLF001
        out.append(a.help)
        if isinstance(a.metavar, str):
            out.append(a.metavar)
        if isinstance(getattr(a, 'choices', None), dict):
            for sub in a.choices.values():
                if isinstance(sub, argparse.ArgumentParser):
                    out += _parser_strings(sub)
    return [s for s in out if isinstance(s, str) and s is not argparse.SUPPRESS]


def _builders():
    try:
        import verb_parsers
        import config_loader
    except ImportError as e:  # PyYAML 等
        raise unittest.SkipTest(f'parser builders を import できない: {e}')
    return {
        'relative': verb_parsers.build_relative_parser,
        'rotation': verb_parsers.build_rotation_parser,
        'groundobs': verb_parsers.build_groundobs_parser,
        'render': config_loader.build_parser,
    }


class CliI18nTests(unittest.TestCase):
    def setUp(self):
        cli_i18n.set_lang(None)

    def tearDown(self):
        cli_i18n.set_lang(None)

    def test_all_help_strings_in_dict(self):
        cli_i18n.set_lang('ja')  # 置換前の原文を集める
        missing = set()
        for name, build in _builders().items():
            for s in _parser_strings(build()):
                if JA.search(s) and s not in cli_i18n.DICT:
                    missing.add((name, s[:60]))
        self.assertEqual(sorted(missing), [], '辞書に無い CLI ヘルプ文言')

    def test_english_help_has_no_japanese(self):
        cli_i18n.set_lang('en')
        for name, build in _builders().items():
            with self.subTest(verb=name):
                text = build().format_help()
                self.assertIsNone(JA.search(text), f'{name}: 英語ヘルプに日本語が残っている:\n{text}')

    def test_japanese_help_untouched(self):
        cli_i18n.set_lang('ja')
        for name, build in _builders().items():
            with self.subTest(verb=name):
                self.assertIsNotNone(JA.search(build().format_help()))

    def test_gui_server_parser(self):
        try:
            import gui_server  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f'gui_server を import できない: {e}')
        src = (ROOT / 'gui_server.py').read_text(encoding='utf-8')
        for s in re.findall(r"(?:help|description)='([^']*)'", src.split('def main(argv', 1)[1]):
            if JA.search(s):
                self.assertIn(s, cli_i18n.DICT, f'gui parser: {s}')

    def test_dictionary_sanity(self):
        pct = re.compile(r'%\((\w+)\)[sdfr]')
        ph = re.compile(r'\{(\w+)\}')
        for ja, en in cli_i18n.DICT.items():
            self.assertIsNone(JA.search(en), f'英訳に日本語: {ja!r} -> {en!r}')
            self.assertEqual(sorted(pct.findall(ja)), sorted(pct.findall(en)), f'%(…)s 不一致: {ja!r}')
            self.assertEqual(sorted(ph.findall(ja)), sorted(ph.findall(en)), f'{{name}} 不一致: {ja!r}')
            self.assertTrue(en.strip(), f'空の英訳: {ja!r}')

    def test_lang_detection(self):
        cases = [
            ({'MRENDER_LANG': 'en', 'LANG': 'ja_JP.UTF-8'}, 'en'),
            ({'MRENDER_LANG': 'ja', 'LANG': 'en_US.UTF-8'}, 'ja'),
            ({'LC_ALL': 'ja_JP.UTF-8', 'LANG': 'en_US.UTF-8'}, 'ja'),
            ({'LANG': 'en_US.UTF-8'}, 'en'),
            ({'LANG': 'ja_JP.UTF-8'}, 'ja'),
            ({'LANG': 'de_DE.UTF-8'}, 'en'),
        ]
        for env, want in cases:
            with self.subTest(env=env):
                clean = {k: v for k, v in os.environ.items()
                         if k not in ('MRENDER_LANG', 'LC_ALL', 'LC_MESSAGES', 'LANG')}
                with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
                    self.assertEqual(cli_i18n.cli_lang(), want)

    def test_tr_passthrough(self):
        cli_i18n.set_lang('en')
        self.assertIsNone(cli_i18n.tr(None))
        self.assertEqual(cli_i18n.tr('辞書に無い文言'), '辞書に無い文言')
        self.assertEqual(cli_i18n.tr('フレーム数'), 'number of frames')
        cli_i18n.set_lang('ja')
        self.assertEqual(cli_i18n.tr('フレーム数'), 'フレーム数')


if __name__ == '__main__':
    unittest.main()
