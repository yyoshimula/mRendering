# 人工衛星軌道レンダリング

Mitsuba 3を使用して、地球周りを運動する人工衛星のレンダリングを行うプロジェクトです。

## セットアップ

### 1. 仮想環境の作成とアクティベート

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

## 使用方法

### 基本的な使い方

```bash
source venv/bin/activate
python satellite_orbit.py
```

デフォルトでは、60フレームの画像シーケンスが `output/` ディレクトリに生成されます。

### オプション

```bash
python satellite_orbit.py --frames 120 --orbit-radius 12.0 --orbit-inclination 30
```

#### 利用可能なオプション：

- `--frames`: レンダリングするフレーム数（デフォルト: 60）
- `--orbit-radius`: 軌道半径（地球半径の倍数、デフォルト: 10.0）
- `--orbit-inclination`: 軌道傾斜角（度、デフォルト: 23.5）
- `--output-dir`: 出力ディレクトリ（デフォルト: output）
- `--start-frame`: 開始フレーム（デフォルト: 0）
- `--end-frame`: 終了フレーム（デフォルト: None = 最後まで）
- `--earth-texture`: 地球のテクスチャ画像パス（オプション）
- `--width`: 画像の幅（デフォルト: 1920）
- `--height`: 画像の高さ（デフォルト: 1080）

### 例

#### 短いテストレンダリング（10フレーム）

```bash
python satellite_orbit.py --frames 10
```

#### 地球にテクスチャを貼り付ける

```bash
python satellite_orbit.py --earth-texture blue_marble.jpg
```

#### 高解像度（4K）でレンダリング

```bash
python satellite_orbit.py --width 3840 --height 2160
```

#### 高軌道の衛星（地球半径の15倍）

```bash
python satellite_orbit.py --orbit-radius 15.0 --frames 60
```

#### 極軌道（90度傾斜）

```bash
python satellite_orbit.py --orbit-inclination 90 --frames 60
```

#### 特定のフレーム範囲のみレンダリング

```bash
python satellite_orbit.py --frames 100 --start-frame 20 --end-frame 40
```

## 出力

レンダリングされた画像は `output/` ディレクトリに以下の形式で保存されます：

```
output/
  ├── frame_0000.png
  ├── frame_0001.png
  ├── frame_0002.png
  └── ...
```

## シーンの構成

- **地球**: 青い球体（半径: 1.0単位）、またはテクスチャマッピング
- **人工衛星**: 金属製の箱型本体とソーラーパネル
- **太陽光**: 指向性光源
- **環境**: 暗い宇宙背景

## カスタマイズ

`satellite_orbit.py` を編集することで、以下の要素をカスタマイズできます：

- 地球の色やテクスチャ
- 人工衛星の形状やサイズ
- カメラの位置と視野角
- ライティング設定
- レンダリング品質（サンプル数など）

## アニメーション動画の作成

画像シーケンスからMP4動画を作成する場合（ffmpegが必要）：

```bash
ffmpeg -framerate 30 -i output/frame_%04d.png -c:v libx264 -pix_fmt yuv420p satellite_orbit.mp4
```

## トラブルシューティング

### レンダリングが遅い場合

`satellite_orbit.py` の `sample_count` を減らしてください（現在: 64）：

```python
'sampler': {
    'type': 'independent',
    'sample_count': 32,  # 64から32に変更
},
```

### メモリ不足エラー

一度にレンダリングするフレーム数を減らして、複数回に分けて実行してください：

```bash
python satellite_orbit.py --frames 100 --start-frame 0 --end-frame 25
python satellite_orbit.py --frames 100 --start-frame 25 --end-frame 50
python satellite_orbit.py --frames 100 --start-frame 50 --end-frame 75
python satellite_orbit.py --frames 100 --start-frame 75 --end-frame 100
```

## 技術詳細

- **レンダリングエンジン**: Mitsuba 3
- **バリアント**: llvm_ad_rgb（CPUベースのパストレーシング）
- **レンダリング手法**: パストレーシング
- **画像解像度**: デフォルト 1920x1080（設定可能）
- **サンプル数**: 64 spp（samples per pixel）
