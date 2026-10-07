"""GUI の表示言語辞書（gui/i18n.js）のカバレッジ検査。Mitsuba 不要。

Run with: python -m unittest discover -s tests -p 'test_gui_i18n.py'（要 node, PyYAML）

検査内容:
  - gui_server.build_verb_schema() の label / help / title / desc の日本語が全て辞書にある
  - gui/index.html の静的テキストノード・title / placeholder / aria-label / alt 属性の
    日本語が全て辞書にある（I18N.translateDom が置換する対象）
  - index.html / object_viewer.js で tr('…') / I18N.t('…') に渡している原文が全て辞書にある
  - gui_server.py が _send_error_json で返す日本語メッセージが辞書にある
  - 辞書の英訳側に日本語が残っていない / プレースホルダ {name} が原文と英訳で一致する
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JA = re.compile('[ぁ-んァ-ン一-龥]')
NORM = re.compile(r'\s+')


def _norm(s: str) -> str:
    return NORM.sub(' ', s).strip()


def _load_dict() -> dict:
    node = shutil.which('node')
    if not node:
        raise unittest.SkipTest('node が無い')
    out = subprocess.run(
        [node, '-e', "process.stdout.write(JSON.stringify(require(process.argv[1]).dict))",
         str(ROOT / 'gui' / 'i18n.js')],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class _StaticText(HTMLParser):
    """静的 HTML のテキストノードと翻訳対象属性を集める（script/style/pre/textarea は除外）。"""
    SKIP = {'script', 'style', 'pre', 'textarea', 'code'}
    ATTRS = ('title', 'placeholder', 'aria-label', 'alt')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.texts: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        no_translate = dict(attrs).get('translate') == 'no'
        if tag not in ('br', 'img', 'input', 'meta', 'link'):
            # translate="no" の要素は SKIP 扱いのタグ名で積む（I18N.translateDom と同じ規則）
            self.stack.append('style' if no_translate else tag)
        if no_translate:
            return
        for k, v in attrs:
            if k in self.ATTRS and v and JA.search(v):
                self.attrs.append(v)

    def handle_endtag(self, tag):
        # translate="no" 要素は 'style' として積んでいるので、tag 名一致でなく深さで戻す
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] in self.SKIP:
            return
        key = _norm(data)
        if key and JA.search(key):
            self.texts.append(key)


class GuiI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dict = _load_dict()
        cls.index_html = (ROOT / 'gui' / 'index.html').read_text(encoding='utf-8')
        cls.viewer_js = (ROOT / 'gui' / 'object_viewer.js').read_text(encoding='utf-8')

    def _missing(self, strings):
        return sorted({s for s in strings if JA.search(s) and s not in self.dict and _norm(s) not in self.dict})

    def test_schema_strings_covered(self):
        try:
            import gui_server
        except ImportError as e:  # PyYAML 等
            raise unittest.SkipTest(f'gui_server を import できない: {e}')
        found: list[str] = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ('label', 'help', 'title', 'desc', 'placeholder') and isinstance(v, str):
                        found.append(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(gui_server.build_verb_schema())
        self.assertEqual(self._missing(found), [], 'フォームスキーマの未翻訳文言')

    def test_static_html_covered(self):
        p = _StaticText()
        p.feed(self.index_html)
        self.assertEqual(self._missing(p.texts), [], '静的 HTML テキストの未翻訳文言')
        self.assertEqual(self._missing(p.attrs), [], '静的 HTML 属性の未翻訳文言')

    def test_tr_calls_covered(self):
        pat = re.compile(r"(?:\btr|I18N\.t)\(\s*(['\"])((?:\\.|(?!\1).)*)\1")
        args = [m.group(2).replace("\\'", "'").replace('\\"', '"')
                for src in (self.index_html, self.viewer_js) for m in pat.finditer(src)]
        self.assertGreater(len(args), 50)
        self.assertEqual(self._missing(args), [], 'tr() 原文の未翻訳文言')

    def test_conditional_tr_literals_covered(self):
        # tr(cond ? 'A' : 'B') の形: 引数式の中の文字列リテラルも検査する
        pat = re.compile(r"\btr\(([^;]*?)\)\s*[;,)]")
        lits: list[str] = []
        for m in pat.finditer(self.index_html):
            lits += re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1))
        self.assertEqual(self._missing([s for s in lits if JA.search(s)]), [])

    def test_server_error_messages_covered(self):
        src = (ROOT / 'gui_server.py').read_text(encoding='utf-8')
        msgs = re.findall(r"_send_error_json\('([^']*)'", src)
        self.assertEqual(self._missing(msgs), [], 'gui_server のエラー文言')

    def test_dictionary_sanity(self):
        ph = re.compile(r'\{(\w+)\}')
        for ja, en in self.dict.items():
            self.assertFalse(JA.search(en), f'英訳に日本語が残っている: {ja!r} -> {en!r}')
            self.assertEqual(sorted(set(ph.findall(ja))), sorted(set(ph.findall(en))),
                             f'プレースホルダ不一致: {ja!r} -> {en!r}')
            self.assertTrue(en.strip(), f'空の英訳: {ja!r}')

    def test_runtime_translate_and_placeholders(self):
        node = shutil.which('node')
        if not node:
            raise unittest.SkipTest('node が無い')
        js = (
            "const I = require(process.argv[1]);"
            "const out = {lang: I.lang,"
            " a: I.t('{n} フレーム', {n: 12}),"
            " b: I.t('存在しない文言', {}),"
            " c: I.t(null),"
            " d: I.translateSchema({verbs: {x: {label: 'フレーム数', sections: [{title: '地球', fields: [{key: 'k', label: '幅 [px]', help: 'ガンマ'}]}]}}, files: {model: ['名前']}})};"
            "process.stdout.write(JSON.stringify(out));"
        )
        env = {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'}
        res = json.loads(subprocess.run([node, '-e', js, str(ROOT / 'gui' / 'i18n.js')],
                                        capture_output=True, text=True, check=True, env=env).stdout)
        # node には navigator.language='en-US' があるので英語モードになる（無ければ ja で a は原文）
        if res['lang'] == 'en':
            self.assertEqual(res['a'], '12 frames')
            self.assertEqual(res['d']['verbs']['x']['label'], 'Frames')
            self.assertEqual(res['d']['verbs']['x']['sections'][0]['title'], 'Earth')
            self.assertEqual(res['d']['verbs']['x']['sections'][0]['fields'][0]['help'], 'Gamma')
        else:
            self.assertEqual(res['a'], '12 フレーム')
        self.assertEqual(res['b'], '存在しない文言')
        self.assertIsNone(res['c'])
        # label 以外のキー（ファイル名等）は翻訳しない
        self.assertEqual(res['d']['files']['model'], ['名前'])


if __name__ == '__main__':
    unittest.main()
