# DGX Spark でレンダリング、Mac から GUI 操作

計算を DGX Spark（Grace ARM64 + Blackwell GPU）で行うには 2 方式ある。

## 方式①: GUI 内マシン選択（推奨）

GUI は Mac で普通に起動し（`python mrender.py gui`）、ヘッダーの
「実行マシン」セレクタで local / dgx をジョブ・ライブプレビューごとに
切り替える。リモートマシンはリポジトリルートの `gui_hosts.json` で定義する
（ssh ホスト名・リポジトリ dir・MRENDER_VARIANT。GUI 自身が動くホストと
同名のエントリは自動除外されるので、DGX 側に同期しても無害）。

- バッチジョブ: YAML を scp → `ssh -tt` でリモート実行。runs/<run> は rsync で
  Mac へ自動ミラーされ、進捗・最新フレーム・成果物リンクはローカル実行と
  同じ UI。プロセス終了後の最終同期中は status=syncing になる。停止ボタンは
  リモートプロセスも pkill する。
- ライブプレビュー: worker を `ssh -L` トンネル付きでリモート常駐させる
  （リモート側ポートは GUI ポート+1+100。方式②の worker と衝突しないため）。
- 前提: `tools/dgx/sync_to_dgx.sh` でコード・アセットを同期済みであること。
  **リモートのシーンはリモート側のファイルで組まれる**ので、プリセットや
  モデルを変えたら再同期する。

## 方式②: GUI ごと DGX で動かす

GUI サーバー自体（+ live_worker 常駐）を DGX で動かし、Mac のブラウザから
SSH トンネル越しに操作する。トンネルは GUI ポート（既定 8600）の
**1 本だけ** でよい — live_worker（ポート+1）は gui_server が
`/api/live/*` をプロキシするため。Mac を閉じてもレンダリングが続くのが利点。

```
Mac ブラウザ ──http://127.0.0.1:8600──▶ SSH トンネル ──▶ DGX gui_server:8600
                                                            ├─ live_worker:8601（プロキシ経由）
                                                            └─ mrender.py <verb> サブプロセス（GPU レンダ）
```

## 前提

`~/.ssh/config`（Mac 側）に DGX のエントリを作っておく。全スクリプトは
既定でホスト名 `dgx` を使う（`DGX_HOST` 環境変数で変更可）:

```
Host dgx
    HostName <DGXのIPアドレス>
    User <ユーザー名>
```

## 使い方

```bash
# 1. コード同期（Mac 側、コード変更のたびに実行。方式①②共通）
tools/dgx/sync_to_dgx.sh

# 2. 初回セットアップ（DGX 側で一度だけ。方式①②共通）
ssh dgx 'cd mRendering && tools/dgx/setup_dgx.sh'

# 3a. 方式①: Mac で GUI を起動してヘッダーの「実行マシン」で dgx を選ぶ
python mrender.py gui

# 3b. 方式②: GUI ごと DGX — GUI 自動起動 + トンネル + ブラウザを開くまで全部やる
tools/dgx/connect_dgx.sh

# トンネルを閉じる
tools/dgx/connect_dgx.sh stop
```

GUI は DGX 側の tmux セッション `mrender-gui` で動き続けるので、Mac を閉じても
レンダリングは継続する。再接続は `connect_dgx.sh` を再実行するだけ。
GUI 本体を止めるには `ssh dgx 'tmux kill-session -t mrender-gui'`。

## 学生・メンバーが自分のアカウントで使う

dgx は 1 人 1 アカウントで使う（共用アカウントは使わない）。GUI サーバーは
人ごとに別プロセス・別ポートで動くので、同時に使っても互いの設定やジョブは
混ざらない。共有されるのは GPU だけ（誰かの長いバッチ中は他の人のプレビューも遅くなる）。

1. **管理者が用意するもの**: dgx のアカウント（`ssh root@dgx adduser --disabled-password <name>`）と、
   tailnet ACL の `ssh` ルール 1 行（`src` = 本人の Tailscale ログイン、`dst` = `tag:dgx`、
   `users` = 本人のアカウント名）。**ルールがあれば SSH 公開鍵の登録は不要**（Tailscale SSH が認証する）。
2. **本人**: tailnet に参加した端末から `ssh <name>@<DGX の Tailscale IP>` で入れることを確認。
3. **本人（dgx 上、初回のみ）**:
   ```bash
   git clone https://github.com/yyoshimula/mRendering.git && cd mRendering
   tools/dgx/setup_dgx.sh          # venv 作成 + Mitsuba (cuda_ad_rgb) 確認
   ```
4. **起動（dgx 上）**: 割り当てられたポートで
   ```bash
   GUI_PORT=<自分のポート> tools/dgx/start_gui_dgx.sh
   ```
   tmux 内で動かせば端末を閉じても続く（`tmux new -s mrender-gui` してから上を実行）。
5. **手元の Mac から開く**: このリポジトリを Mac にも clone してあれば
   ```bash
   DGX_HOST=<name>@<DGX の Tailscale IP> GUI_PORT=<自分のポート> tools/dgx/connect_dgx.sh
   ```
   （起動確認 → トンネル → ブラウザまで自動。無ければ `ssh -L <port>:127.0.0.1:<port> <name>@<DGX の Tailscale IP>` を張って `http://127.0.0.1:<port>` を開く）

### ポート割り当て

GUI ポートに加えて +1（ライブワーカー）と +101（方式①のリモートワーカー）を使うので、10 刻みで割り当てる。

| アカウント | GUI ポート |
|---|---|
| 管理者 | 8600 |
| メンバー 1 人目 | 8610 |
| メンバー 2 人目 | 8620 |

`internal/`（非公開データ）は GitHub に無い。必要な人には Dropbox から別途渡す。

## Mitsuba バリアント（GPU / CPU）

バリアントは環境変数 `MRENDER_VARIANT`（カンマ区切り、先勝ち）で選ぶ
（実装は [mi_variant.py](../../mi_variant.py)）。`start_gui_dgx.sh` の既定は
`cuda_ad_rgb,llvm_ad_rgb` — CUDA ビルドがあれば GPU、無ければ CPU に自動
フォールバック。GUI から起動される verb サブプロセスと live_worker は環境
変数を継承するので、常に同じバリアントで動く。

## Mitsuba を aarch64 + CUDA でソースビルドする（通常は不要）

**pip の mitsuba wheel（3.9.1 時点）は aarch64 Linux 向けに存在し、
`cuda_ad_rgb` 込み**なので通常は `setup_dgx.sh` だけで GPU レンダリングまで
動く（DGX Spark 実機で 2026-08-19 確認済み: cornell box 64spp 0.18s、
oos_hubble 2 フレーム@32spp 全体 3.9s）。将来 wheel が壊れた場合や独自
バリアントが要る場合のみ、以下でソースビルドする:

```bash
sudo apt install -y clang ninja-build cmake libpng-dev libjpeg-dev python3-dev
git clone --recursive https://github.com/mitsuba-renderer/mitsuba3
cd mitsuba3
mkdir build && cd build
cmake -GNinja -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ ..
# → 生成された build/mitsuba.conf の "enabled" に variants を列挙して再 cmake:
#   "enabled": ["scalar_rgb", "llvm_ad_rgb", "cuda_ad_rgb"],
cmake -GNinja .. && ninja
```

ビルド後、venv から見えるようにする（どちらか）:

```bash
# a) setpath.sh を start_gui_dgx.sh 実行前に source する
source ~/mitsuba3/build/setpath.sh
# b) venv に pip install する
pip install ~/mitsuba3
```

確認: `python -c "import mitsuba as mi; print(mi.variants())"` に
`cuda_ad_rgb` が出れば OK。

## 注意

- **CUDA と LLVM のレンダ結果はビット一致しない**。ライブ vs バッチの
  ピクセル一致検証はバリアント単位（同一バリアント内では従来どおり成立）。
- AI drawer を DGX 経由で使うには DGX 側に `claude` CLI が必要。トンネル
  越しのリクエストは DGX 上では 127.0.0.1 から来るので localhost 制限は
  そのまま通る。
- 成果物（runs/）は DGX 側に溜まる。回収は
  `rsync -avz dgx:mRendering/runs/<RUN>/ runs/<RUN>/`。
- `runs/` は `sync_to_dgx.sh` の同期対象外なので、Mac→DGX の再同期で
  消えることはない。
