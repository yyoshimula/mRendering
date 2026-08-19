"""ラン単位の出力ディレクトリと manifest 管理。

各 verb（render / rotation / relative …）は実行ごとに 1 個の「ラン」を作る。
本モジュールはその物理レイアウト（ディレクトリ作成・命名・メタデータ書き出し）を
集約する。verb 側はコア処理に集中し、出力先や記録の体裁はここに任せる。

各実行は `runs/<YYYYMMDD_HHMMSS>_<verb>_<name>/` を作り、
- `config.resolved.yaml` … 解決済み引数のスナップショット（CLI > YAML > 既定 を反映後）
- `manifest.json`         … verb, 引数, git rev, ホスト, 開始/終了時刻, ステータス等
- `frames/`               … レンダリング結果（PNG 連番）
を配置する。再現性確保と、後で「あのランは何だったか」を追える状態を作るのが目的。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# 既定のラン置き場。プロジェクト直下に `runs/` を作る。
DEFAULT_RUNS_ROOT = Path('runs')
# ファイル/ディレクトリ名に使えない文字を `_` に潰すための正規表現。
_SLUG_RE = re.compile(r'[^A-Za-z0-9._-]+')


def _slugify(name: str) -> str:
    """任意文字列を、ディレクトリ名として安全な短い識別子へ変換する。

    日本語や空白などは `_` に置換し、両端の `_` を除去する。空文字列になった場合は
    フォールバックとして `'run'` を返す。
    """
    cleaned = _SLUG_RE.sub('_', name.strip())
    return cleaned.strip('_') or 'run'


def _git_revision(cwd: Path) -> Optional[str]:
    """現在の git HEAD の短縮 SHA を取得する。dirty なら `-dirty` を付ける。

    git が無い／非リポジトリの場合は静かに `None` を返す（manifest に書かないだけ）。
    """
    try:
        rev = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=cwd, stderr=subprocess.DEVNULL,
        ).decode().strip()
        # `git diff --quiet` は変更があると 1 を返すので dirty 判定に使う。
        dirty = subprocess.call(
            ['git', 'diff', '--quiet'],
            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return rev + ('-dirty' if dirty else '')
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _serialise(value: Any) -> Any:
    """argparse Namespace に紛れ込む任意値を YAML/JSON 安全な型へ落とす。

    Path → str、dict/list は再帰、プリミティブはそのまま、その他は `repr()` で
    フォールバック。これで `yaml.safe_dump` / `json.dump` が確実に通る。
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


@dataclass
class RunDir:
    """1 回の実行に紐づく出力ディレクトリ。

    Attributes:
        path: ラン本体のディレクトリ（例 `runs/20260428_120000_render_iss/`）。
        verb: 実行された動詞名（'render', 'rotation' など）。
        name: ラン識別用の slug 化済み名前（プリセット名や物体名から生成）。
        started_at: ラン開始時刻。manifest の `duration_s` 算出に使う。
        manifest_extra: 各 verb が任意で書き加えるメタ情報（例: video パス）。
    """

    path: Path
    verb: str
    name: str
    started_at: datetime
    manifest_extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def frames_dir(self) -> Path:
        """`<run>/frames/` を返す（無ければ作成）。PNG 連番の置き場。"""
        d = self.path / 'frames'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_resolved_config(self, args: argparse.Namespace) -> Path:
        """CLI/YAML/既定値をマージ後の `args` を `config.resolved.yaml` として保存。

        この YAML をそのまま `--config` に渡せば同じランを再現できる。
        """
        out = self.path / 'config.resolved.yaml'
        data = {k: _serialise(v) for k, v in vars(args).items()}
        with out.open('w') as fh:
            yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)
        return out

    def write_manifest(
        self,
        args: argparse.Namespace,
        finished_at: Optional[datetime] = None,
        status: str = 'ok',
        extra: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """ラン終了時に `manifest.json` を書き出す。

        Args:
            args: 解決済み引数。`args` キーに丸ごとシリアライズして残す。
            finished_at: 終了時刻（省略時は now）。`duration_s` 算出に使用。
            status: `'ok'` または `'error: <repr>'`。例外捕捉側からセットされる。
            extra: verb 固有の追加情報（frame_count, video パスなど）。
                   既に `manifest_extra` に積まれた内容と合わせて書き込む。
        """
        finished_at = finished_at or datetime.now()
        manifest: Dict[str, Any] = {
            'verb': self.verb,
            'name': self.name,
            'started_at': self.started_at.isoformat(timespec='seconds'),
            'finished_at': finished_at.isoformat(timespec='seconds'),
            'duration_s': (finished_at - self.started_at).total_seconds(),
            'status': status,
            'argv': sys.argv,
            'args': {k: _serialise(v) for k, v in vars(args).items()},
            'git_rev': _git_revision(Path.cwd()),
            'host': socket.gethostname(),
            'cwd': str(Path.cwd()),
            'pid': os.getpid(),
        }
        # verb 側が積んでおいた追加情報を合流させる（後から渡した extra が優先）。
        manifest.update(self.manifest_extra)
        if extra:
            manifest.update(extra)
        out = self.path / 'manifest.json'
        with out.open('w') as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False, default=str)
        return out


def _explicit_output_dir(args: argparse.Namespace) -> Optional[str]:
    """ユーザが `--output-dir` を明示指定したかを判定する。

    `_ARG_DEFAULTS['output_dir'] == 'output'`（argparse 側の既定値）と一致する場合は
    「未指定」とみなし、`runs/<ts>_...` の自動命名に倒す。後方互換のため、明示的に
    `output` 以外を渡せばその場所をそのまま使う。
    """
    value = getattr(args, 'output_dir', None)
    if value in (None, '', 'output'):
        return None
    return str(value)


def create_run_dir(
    verb: str,
    args: argparse.Namespace,
    *,
    name_hint: Optional[str] = None,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    manifest_extra: Optional[Dict[str, Any]] = None,
) -> RunDir:
    """新規ランディレクトリを作成し、`config.resolved.yaml` を即座に書き出す。

    output_dir が明示されていればそこを使い、未指定なら
    `runs/<YYYYMMDD_HHMMSS>_<verb>_<name>/` を作る。`name_hint` には主物体名や
    プリセット名を渡すと、後で一覧で見分けやすい名前になる。

    Args:
        verb: 動詞名。ディレクトリ名と manifest に記録される。
        args: 解決済み引数。`config.resolved.yaml` に保存される。
        name_hint: ディレクトリ名末尾に使う識別子（slug 化される）。
        runs_root: ルート（既定 `runs/`）。テスト時は差し替え可能。
        manifest_extra: manifest にあらかじめ載せたい情報。
    """
    started = datetime.now()
    # ヒント → primary_name → verb の順でフォールバック。
    name = _slugify(name_hint or getattr(args, 'primary_name', None) or verb)

    explicit = _explicit_output_dir(args)
    if explicit:
        # 明示指定されたパスはそのまま使用（自動 ts 命名なし）。
        path = Path(explicit)
    else:
        ts = started.strftime('%Y%m%d_%H%M%S')
        path = runs_root / f'{ts}_{verb}_{name}'

    path.mkdir(parents=True, exist_ok=True)

    run = RunDir(
        path=path,
        verb=verb,
        name=name,
        started_at=started,
        manifest_extra=dict(manifest_extra or {}),
    )
    # 解決済み設定はランの開始時点で必ず保存しておく（途中失敗しても残す）。
    run.write_resolved_config(args)
    return run
